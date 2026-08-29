#!/usr/bin/env python3
"""NIFTY options intraday decision engine - UPSTOX data source.

Captures the NIFTY option chain + market context from the authenticated
Upstox Developer API (no NSE scraping / no Akamai 403) once per minute and
writes:

1. One option-chain row per strike with CE and PE fields in the same row.
2. One market-context row per capture.
3. One calculated snapshot-summary row.
4. Sectoral context, spot ticks, an LLM-size chain export, the decision
   packet, and (event-driven) a Gemini audit.

Generated files
---------------
All data is written under the hardcoded output folder
(C:/Users/Administrator/Desktop/OI-AI-Strategy/nifty_data) in ONE
SUBFOLDER PER TRADING DAY named by the date (YYYY-MM-DD), e.g.
nifty_data/2026-08-28/ . Files for a session:

* nifty-option-chain-v2-YYYY-MM-DD.csv
    The core derivative snapshot. One row per strike per capture: CE+PE OI,
    exchange OI change, interval OI change, volume, LTP, IV, bid/ask/depth,
    spread, intrinsic/extrinsic. AUTHORITATIVE source for option quotes/OI.
* nifty-option-chain-llm-YYYY-MM-DD.csv
    Size-optimised SUBSET of the above for pasting into an LLM: last
    NIFTY_LLM_CHAIN_SNAPSHOTS snapshots at ATM +/- NIFTY_LLM_CHAIN_STRIKES
    strikes, floats rounded (typically 50-150 KB vs several MB).
* nifty-market-context-v2-YYYY-MM-DD.csv
    One row per capture: chain freshness (source_age_seconds, chain_frozen,
    chain_data_age_seconds), 1-minute sampled spot high/low, day OHLC,
    India VIX day OHLC, minutes_to_close, optional futures/VWAP context,
    scheduled-event note.
* nifty-sector-context-v2-YYYY-MM-DD.csv
    One row per capture: sectoral indices (NIFTY BANK, IT, FIN SERVICE, FMCG,
    AUTO, METAL, PHARMA, ENERGY, PSU BANK, REALTY, MIDCAP SELECT, INFRA) last
    + %change, from the SAME allIndices payload the collector already fetches
    (zero extra network cost). Feeds the anticipation layer's breadth /
    sector-lead context - advisory only, never a trade trigger.
* nifty-snapshot-summary-v2-YYYY-MM-DD.csv
    One row per capture: session-range aggregates (total CE/PE OI, OI PCR,
    exchange-change PCR, interval OI/volume, max-OI strikes, ATM quotes/IV,
    ATM straddle, executability + trade_ready flags, quality notes).
* nifty-spot-ticks-v2-YYYY-MM-DD.csv
    Intra-minute spot/VIX ticks (polled at :20 and :40 each minute). Feeds
    tick-based breakout confirmation and the sampled 1-min high/low.
* nifty-llm-analysis-v2-YYYY-MM-DD.json
    The full decision packet (OVERWRITTEN each capture): decision, price
    structure, swing levels, option flow, tail stats, breakout state,
    risk/geometry, option candidate, blocking/advisory reasons, decision
    grade. This is what an LLM consumes.
* nifty-trader-report-v2-YYYY-MM-DD.json
    Condensed human-readable subset of the decision packet (OVERWRITTEN).
* signals_ledger-YYYY-MM-DD.csv
    APPEND-ONLY audit trail of every signal/detection (mode, decision,
    reasons). Source of truth for daily signal caps.
* pending_llm_review-YYYY-MM-DD.jsonl
    ESCALATION QUEUE for the LLM decision layer (edge-triggered). One row per
    escalation transition: "execute" (BUY at execution grade), "review"
    (journal-grade BUY), "armed" (level crossed/unconfirmed or flow building).
    A scheduler polls this file instead of running the LLM every minute.
* gemini_decision-latest.json
    Latest Gemini audit result (OVERWRITTEN each call): timestamp, escalation,
    snapshot id, decision, concise summary + full response. Written only when
    an escalation level warrants a call (execute/review/armed).
* gemini_decision-log-YYYY-MM-DD.jsonl
    Append-only history of every Gemini audit call (and any error), keyed by
    timestamp — the audit trail of AI decisions for the session.
* paper_trades-YYYY-MM-DD.csv
    SIMULATED trading ledger. One row per CLOSED paper trade: entry/exit
    underlying + premium, stop/targets, exit reason (STOP/T2/EOD), underlying
    points, premium P&L per lot (entry at ask, exit at bid - no costs). Use it
    to judge whether the strategy is profitable over time.
* paper_state-YYYY-MM-DD.json
    Currently OPEN paper trades (trades move here on signal, then to
    paper_trades CSV when closed).
* paper_summary-YYYY-MM-DD.csv
    Rolling session summary: trades, wins/losses, win rate, total P&L (₹ and
    points), avg win/loss, profit factor. Overwritten after each close.
* last_signal_state-YYYY-MM-DD.json
    Last signal + timestamp (per session) for cooldown enforcement.
* collector_state.json
    Persisted collector state across restarts (session anchor ATM, last
    source timestamps/hashes, per-contract OI/volume for interval deltas,
    recent ticks).
* collector.log, collector.lock
    Runtime log and single-instance lock.

Running
-------
* Collect:                 python nifty_collector_v8.py
    Credentials are hardcoded in the UPSTOX section near the top. The
    access token expires daily - regenerate it in the Upstox console and
    paste the new value there.
* Verify connectivity:     python nifty_collector_v8.py --check-upstox
* Offline decision run:    python nifty_collector_v8.py --analyze 2026-08-28
* Paper-trading results:   python nifty_collector_v8.py --paper-report 2026-08-28
* Proxy (if your IP is blocked): set UPSTOX_HTTP_PROXY=http://user:pass@host:port

Important limitations
---------------------
* Upstox OI/volume arrive in QUANTITY (shares); the adapter normalises them to
  CONTRACTS (divided by lot size) so they match the NSE schema's unit.
* IV and Greeks are broker-computed and may differ slightly from NSE's official
  values - always use ONE consistent source for a coherent series.
* A once-per-minute spot observation is not a true one-minute OHLC candle.

Use Upstox data in accordance with the Upstox Developer API terms.
This collector is for research/data preparation; it does not place trades.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


# =============================================================================
# env.txt loader
# -----------------------------------------------------------------------------
# The operator wants tokens (GEMINI_API_KEY, UPSTOX_API_KEY, UPSTOX_API_SECRET,
# UPSTOX_ACCESS_TOKEN, and any other GEMINI_/UPSTOX_/NIFTY_/PAPER_ tunable) to be
# readable from a plain-text `env.txt` file instead of only env vars / hardcoded
# defaults. Precedence: an already-set real environment variable wins, then a
# value from the env file, then the hardcoded default in the script.
#   * env file discovery: path given by --env-file (CLI), else $NIFTY_ENV_FILE,
#     else ./env.txt, else <script-dir>/env.txt.
#   * format: one KEY=VALUE per line; blank lines and #-comments ignored;
#     values may optionally be quoted with matching ' or ".
#   * This MUST run before the GEMINI_/UPSTOX_ token definitions below.
# =============================================================================

def _read_env_file(path: Optional[str]) -> Dict[str, str]:
    """Parse a simple KEY=VALUE env file into a dict. Returns {} if absent."""
    out: Dict[str, str] = {}
    if not path:
        return out
    p = Path(path)
    if not p.is_file():
        return out
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # strip a single surrounding pair of matching quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _load_env_file(env_file: Optional[str] = None) -> Dict[str, str]:
    """Load env.txt values into os.environ WITHOUT overriding already-set vars."""
    candidates = []
    if env_file:
        candidates.append(env_file)
    if os.getenv("NIFTY_ENV_FILE"):
        candidates.append(os.getenv("NIFTY_ENV_FILE"))
    candidates.append(str(Path("env.txt")))
    candidates.append(str(Path(__file__).resolve().parent / "env.txt"))
    loaded: Dict[str, str] = {}
    seen: set = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        for k, v in _read_env_file(cand).items():
            loaded[k] = v
            if k not in os.environ:
                os.environ[k] = v
    return loaded


ENV_FILE_VALUES = _load_env_file()


# =============================================================================

SCHEMA_VERSION = "2.1.0"
SYMBOL = "NIFTY"
INDEX_NAME = "NIFTY 50"
# Sectoral indices captured from the SAME allIndices response the collector
# already fetches each minute (zero extra network cost). Used only by the
# anticipation layer as breadth / sector-lead context - NEVER a trade trigger
# and NEVER a blocking gate. (nse_name, column_key) pairs.
SECTOR_INDICES = [
    ("NIFTY BANK", "bank"),
    ("NIFTY IT", "it"),
    ("NIFTY FIN SERVICE", "fin_service"),
    ("NIFTY FMCG", "fmcg"),
    ("NIFTY AUTO", "auto"),
    ("NIFTY METAL", "metal"),
    ("NIFTY PHARMA", "pharma"),
    ("NIFTY ENERGY", "energy"),
    ("NIFTY PSU BANK", "psu_bank"),
    ("NIFTY REALTY", "realty"),
    ("NIFTY MIDCAP SELECT", "midcap_select"),
    ("NIFTY INFRA", "infra"),
]
SECTOR_COLUMNS = [
    "schema_version", "snapshot_id", "capture_timestamp", "source_timestamp",
    "timezone", "source", "nifty_last", "nifty_pchange",
]
for _s_name, _s_key in SECTOR_INDICES:
    SECTOR_COLUMNS.append(f"{_s_key}_last")
    SECTOR_COLUMNS.append(f"{_s_key}_pchange")
STRIKE_GAP = 50
STRIKES_ON_EACH_SIDE = int(os.getenv("NIFTY_STRIKES_EACH_SIDE", "12"))
MIN_CURRENT_ATM_COVERAGE = 10  # extend fixed range if current ATM moves far
LOT_SIZE: Optional[int] = None  # set only from a verified current source

# --- V2 fixes ---------------------------------------------------------------
# FIX-1: freshness gates and structural minima used by the decision packet.
MAX_DECISION_CHAIN_AGE_SECONDS = 90   # blocking: chain older than this -> no BUY decision
MIN_STRUCTURAL_STOP_POINTS = 10.0     # blocking: stop closer than this is noise, no trade
SWING_PIVOT_WINDOW = 3                # bars either side required to confirm a swing point
TRIGGER_OBSERVATIONS = 2              # consecutive prints beyond a level required
# FIX-2: extra allIndices polls per minute -> true 1-min spot high/low instead
# of a single observation per minute (futures OHLCV remains broker-fed).
SPOT_TICK_SECONDS = (20, 40)
MAX_TICKS_KEPT = 6
# V2.2: MODE-B capture profile. MODE-A stays the 50-pt / <=25-pt-risk engine.
# MODE-B is a SEPARATE, clearly-labelled capture profile sized for ~20-pt
# moves (the 2026-08-14 flush class): smaller target, tighter stop, earlier
# trigger. It never shares MODE-A's stop/target budget and is still blocked by
# the chain-freshness and executable-quote gates.
MODE_B_ENABLED = True
# V16 (friction): widen MODE-B geometry. Micro-scalping ~10-20-pt targets on an
# index contract is death by friction: a ₹60/lot round-trip cost against a 15-pt
# win grossing ₹375 = 16% drag. Widen to 30-pt target / 15-pt T1 so cost drag is
# a small % and the win rate required to stay net-positive drops materially.
MODE_B_TARGET = 30.0
MODE_B_T1 = 15.0
MODE_B_MAX_RISK = 10.0
# MODE-B risk floor. NOTE (v10.1 audit): this was briefly raised to 10.0 in an
# earlier pass, but that BLOCKED the 2026-08-26 10:09 winner (7.85-pt stop,
# +35/lot). The 6-pt whipsaw loss that motivated the change (2026-08-25 10:51)
# is already prevented by the 0-DTE block. Keeping MODE-B's 5-10 pt profile so
# its quicker-capture budget stays intact.
MODE_B_MIN_RISK = 5.0
MODE_B_MIN_RR = 2.0
# V2.2: tick-based trigger confirmation spacing (two tick observations less
# than this far apart do NOT confirm a break).
TICK_CONFIRM_MIN_SPACING_SECONDS = 45.0
# V2.3 (day-audit driven): the 2026-08-14 replay showed 9 MODE-A BUY signals,
# all 9 stopped out, all in the 09:35-10:08 opening chop. Three principled
# anti-chop filters:
#  1. opening minutes: interval-OI "flow" is de-weighted (overnight CE decay
#     is mechanically misread as bullish accumulation),
#  2. EMA alignment: MODE-A entries must be with the EMA5/EMA13 orientation,
#  3. re-entry cooldown: same-direction MODE-A signals suppressed for a
#     cooldown window after the previous same-direction signal.
OPENING_FLOW_NEUTRAL_MINUTES = 45.0  # V9: raised from 15 -> observed morning chop (09:35-10:08)
                                       # persists well past 15 min; de-weight flow confirmation
                                       # during the whole opening chop window to avoid false longs.
# V18: intraday ENTRY WINDOW (data-supported across 2026-08-21/24/25/26). Momentum
# entries have positive expectancy only in the mid-morning window; late-session
# entries (after ~168 min) lost on every validation day, and opening-chop entries
# lost. Lower bound = opening whipsaw window (configurable; default 45 min, aligns
# with OPENING_FLOW_NEUTRAL_MINUTES). Upper bound = leave enough time for the
# ~40-50pt T2 capture (default 150 min ≈ 11:45 IST). Set to None to disable either.
ENTRY_WINDOW_MIN_MINUTES = float(os.getenv("ENTRY_WINDOW_MIN_MINUTES", "45"))
ENTRY_WINDOW_MAX_MINUTES = float(os.getenv("ENTRY_WINDOW_MAX_MINUTES", "150"))
# V14 (dynamic chop filter): the rigid 45-min morning block is overfitted. Replace it
# with a dynamic "is the tape actually trending or chopping?" measure based on the
# localized True Range (recent N-bar range) relative to the tick-level noise floor.
#   - If the recent range is LARGE relative to the noise floor, the market is making
#     genuine structural progress -> flow can be trusted regardless of time of day
#     (trade the open confidently on real momentum).
#   - If the recent range is small (chop) -> fall back to the time gate.
# CHOP_RANGE_BARS: bars over which to measure localized range / ATR.
# CHOP_TREND_RATIO: how many multiples of the tick-noise floor count as "trending".
OPENING_FLOW_RANGE_BARS = int(os.getenv("OPENING_FLOW_RANGE_BARS", "15"))
OPENING_FLOW_TREND_RATIO = float(os.getenv("OPENING_FLOW_TREND_RATIO", "2.5"))
# V14 (feasibility look-ahead bias): the 50-pt p95 check used trailing quiet windows to
# hard-block late-session breakouts. It now only blocks when the tape is NOT already
# expanding; a genuine fresh range expansion (recent range > prior range) proves the
# move is real and must not be blocked on stale historical p95.
RANGE_EXPANSION_BARS = int(os.getenv("RANGE_EXPANSION_BARS", "10"))
RANGE_EXPANSION_RATIO = float(os.getenv("RANGE_EXPANSION_RATIO", "1.4"))
MODE_A_COOLDOWN_MINUTES = 15.0
# V2.9 (independent audit S-1/S-7/M-2): fresh-30-extreme entries often have no
# structural stop within the 25-pt cap (the previous 10-bar extreme is far
# away after a real impulse) -> MODE-A self-blocked in exactly the trends it
# targets. ATR-based stop fallback + global cooldown + daily signal cap.
MODE_A_ATR_STOP_MULT = 3.0      # stop = clamp(3x ATR proxy, 10, 25) pts
ATR_STOP_FALLBACK_ENABLED = True
GLOBAL_SIGNAL_COOLDOWN_MINUTES = 5.0   # any BUY blocks any new BUY for 5 min
MODE_A_MAX_SIGNALS_PER_DAY = 4
MODE_B_MAX_SIGNALS_PER_DAY = 6
# V2.10 (third independent audit): execution realism and contract selection.
EXECUTION_AGE_CEILING_SECONDS = 120.0  # H8: HARD ceiling for execution-grade
MAX_CROSS_AGE_SECONDS = 600.0          # C6: a breakout older than this is stale
MIN_OPTION_OI = 1000                   # M4: minimum OI (shares) for the ATM leg
MIN_QUOTE_QTY = 75                     # M4: minimum bid/ask depth (1 NIFTY lot)
MAX_OPTION_SPREAD_ABS = 2.0            # M5: absolute spread cap (pts), plus the 0.5% rule
MAX_ATM_IV_SPREAD = 8.0                # data-quality gate: ATM CE/PE IVs further apart
                                       # than this have not repriced yet (pre-open/stale)
# V13 fix (unbounded elasticity): a minimum spot move per interval before it can
# estimate premium elasticity (delta proxy), and an upper cap on that estimate so
# a tiny spot drift with a premium bounce cannot produce an impossible 500% "delta"
# that blocks every subsequent valid flow confirmation.
MIN_ELASTICITY_SPOT_MOVE = 1.5         # pts of spot move required for an elasticity sample
MAX_ELASTICITY = 4.0                   # cap on the observed premium/spot elasticity
LLM_CHAIN_STRIKES = int(os.getenv("NIFTY_LLM_CHAIN_STRIKES", "5"))     # ATM +/- strikes for the LLM export
LLM_CHAIN_SNAPSHOTS = int(os.getenv("NIFTY_LLM_CHAIN_SNAPSHOTS", "20"))  # trailing snapshots for the LLM export

# --- Gemini LLM review (event-driven; fires only on escalation) --------------
# SECURITY NOTE: prefer the environment variable. The fallback key below is
# embedded per operator instruction — do NOT share this file publicly and
# rotate the key if it ever leaks.
GEMINI_ENABLED = os.getenv("GEMINI_ENABLED", "1") in ("1", "true", "True", "yes")
# V19: token may come from env.txt (loaded above via _load_env_file), the real
# environment, or the hardcoded fallback. os.getenv already sees the env-file
# value because _load_env_file() injected it into os.environ.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyBsI2fQduE9GEMnbrucUn3OJAn5p9zQXFo")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096"))
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.2"))
# Which escalation levels trigger a (billable) API call.
GEMINI_CALL_REVIEW = os.getenv("GEMINI_CALL_REVIEW", "1") in ("1", "true", "True", "yes")
GEMINI_CALL_ARMED = os.getenv("GEMINI_CALL_ARMED", "0") in ("1", "true", "True", "yes")
# "execute" always calls (unless GEMINI_ENABLED=0).
GEMINI_SUSPENDED = False  # set True during offline replay/--analyze (no network)
GEMINI_PROMPT_PATH = os.getenv("GEMINI_PROMPT_PATH", "")  # optional external prompt file

GEMINI_PROMPT_TEMPLATE = """You are the final decision auditor for a NIFTY intraday options trading engine.

The Python engine has already computed everything in the data below. Your job is to
INDEPENDENTLY verify its decision and output ONE final decision. Do not force a trade:
WAIT / NO TRADE is a valid and often correct answer.

DATA (attached inline below)
---------------------------
{data}

DECISION GATES - verify each one against the data:
1. FRESHNESS: decision_source_age_seconds must be within the engine's gate
   (default 90s; the packet states which gate applied). Stale -> WAIT / NO TRADE.
2. TRIGGER: a BUY needs breakout_state.confirmed == true (TWO consecutive prints
   >=45s apart beyond the level, confirmation_mode minute_prints or ticks) with
   matching premium-corroborated OI flow (atm_flow), at a FRESH 30-observation
   extreme, and EMA5/EMA13 aligned. If any is missing -> WAIT / NO TRADE.
3. GEOMETRY (MODE-A): stop <= 25 pts AND >= max(2*ATR, 10 pts); Target-2 >= 50 pts
   from entry; reward/risk >= 2.0. (MODE-B: ~20 pt target, 5-10 pt stop, R:R >= 2.)
4. FEASIBILITY: 50 pts must not exceed the p95 directional net move (tail_stats).
5. QUOTES: option_candidate must be executable (spread <= 0.5% AND <= 2 pts).
6. Any BLOCKING reason in "reasons" -> WAIT / NO TRADE.
7. ANTICIPATION (implied_forward divergence, iv_bid/straddle expansion, skew
   velocity, gamma regime) is CONTEXT ONLY: it pre-loads the "armed" state and
   adjusts confidence/exit discipline. It NEVER justifies a BUY by itself.

OUTPUT EXACTLY THIS (plain text, concise, no markdown tables):
DECISION: <BUY CALL NOW | BUY PUT NOW | WAIT / NO TRADE>
DIRECTION: <CALL | PUT | NONE>
ENTRY: <underlying> | STOP: <level> | T1: <level> | T2: <level>
OPTION: <strike CE/PE @ entry ask>
R:R: <value>
REASONS: <1-3 short lines>
BLOCKING: <none, or the blocking reasons>
CONFIDENCE: <low | medium | high - evidence quality, NOT win probability>
AGREEMENT: <AGREE or DISAGREE with the engine + one line why>

BE TERSEC: the entire response must be under 150 words. Do not elaborate.
"""
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

# ---- NSE market timings (equities segment) -------------------------------
PREOPEN_OPEN = dt.time(9, 0)       # pre-open order entry open
PREOPEN_CLOSE = dt.time(9, 8)      # pre-open order entry close (random closure last 1 min)
MARKET_OPEN = dt.time(9, 15)       # regular trading session open
MARKET_CLOSE = dt.time(15, 30)     # regular trading session close
CLOSING_AUCTION_OPEN = dt.time(15, 20)   # closing auction order entry open
CLOSING_AUCTION_CLOSE = dt.time(15, 30)  # closing auction order entry close
CLOSING_SESSION_OPEN = dt.time(15, 50)
CLOSING_SESSION_CLOSE = dt.time(16, 0)
CAPTURE_SECOND = 3  # fetch shortly after the minute changes

# ---- NSE trading holidays 2026 (Equities segment, exchange-declared) -----
# Weekend-only holidays are covered by the weekday test (15-Feb Sun, 21-Mar
# Sat, 15-Aug Sat, 08-Nov Sun). 08-Nov-2026 (Diwali Laxmi Pujan) is a Sunday
# with a special MUHURAT TRADING session - timings notified later by circular.
NIFTY_TRADING_HOLIDAYS_2026 = {
    dt.date(2026, 1, 15),   # Municipal Corporation Election - Maharashtra
    dt.date(2026, 1, 26),   # Republic Day
    dt.date(2026, 3, 3),    # Holi
    dt.date(2026, 3, 26),   # Shri Ram Navami
    dt.date(2026, 3, 31),   # Shri Mahavir Jayanti
    dt.date(2026, 4, 3),    # Good Friday
    dt.date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    dt.date(2026, 5, 1),    # Maharashtra Day
    dt.date(2026, 5, 28),   # Bakri Id
    dt.date(2026, 6, 26),   # Muharram
    dt.date(2026, 9, 14),   # Ganesh Chaturthi
    dt.date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    dt.date(2026, 10, 20),  # Dussehra
    dt.date(2026, 11, 10),  # Diwali-Balipratipada
    dt.date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    dt.date(2026, 12, 25),  # Christmas
}
# Special sessions: not normal trading days (timings notified separately).
NIFTY_MUHURAT_TRADING_DATES_2026 = {
    dt.date(2026, 11, 8),   # Diwali Laxmi Pujan - Muhurat Trading (timings TBA)
}
CSV_FLOAT_DP = 4  # R1-FIX: decimal places kept for floats written to CSV
# H3-FIX (layering, documented): three DISTINCT age thresholds.
#   MAX_DECISION_CHAIN_AGE_SECONDS = 90  -> prompt-mandated BLOCKING gate for
#       BUY decisions (section A / V2 blocking rule).
#   EXECUTION_AGE_CEILING_SECONDS = 120  -> hard ceiling for the "NOW" label
#       (V2.10 H8); signals older than this are relabelled journal-grade.
#   MAX_SOURCE_AGE_SECONDS = 150         -> collector hygiene flag only: marks
#       a snapshot source_is_stale for data-quality visibility. It is NOT the
#       execution gate.
MAX_SOURCE_AGE_SECONDS = 150
# ---- Output directory (HARDCODED per operator) ---------------------------
# All data lives under this folder, organised in ONE SUBFOLDER PER DAY named
# by the trading date (YYYY-MM-DD), e.g. nifty_data/2026-08-28/.
# NIFTY_OUTPUT_DIR env var can override it (used for tests); otherwise the
# hardcoded path below is used.
OUTPUT_DIR = Path(r"C:\Users\Administrator\Desktop\OI-AI-Strategy\nifty_data")
if os.getenv("NIFTY_OUTPUT_DIR"):
    OUTPUT_DIR = Path(os.getenv("NIFTY_OUTPUT_DIR"))
EXTERNAL_CONTEXT_JSON = os.getenv("NIFTY_EXTERNAL_CONTEXT_JSON", "")
SCHEDULED_EVENT = os.getenv("NIFTY_SCHEDULED_EVENT", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

CHAIN_COLUMNS = [
    "schema_version",
    "snapshot_id",
    "capture_timestamp",
    "source_timestamp",
    "timezone",
    "source",
    "underlying",
    "expiry",
    "lot_size",
    "chain_hash",
    "source_age_seconds",
    "source_is_stale",
    "spot",
    "nifty_index_last",
    "nifty_day_open",
    "nifty_day_high",
    "nifty_day_low",
    "nifty_previous_close",
    "india_vix",
    "current_atm",
    "session_anchor_atm",
    "strike",
    "strike_distance_from_spot",
    "moneyness",
    "interval_seconds",
    "ce_interval_seconds",
    "pe_interval_seconds",
    # CE source fields
    "ce_identifier",
    "ce_oi",
    "ce_exchange_change_oi",
    "ce_exchange_pchange_oi",
    "ce_interval_oi_change",
    "ce_volume_cumulative",
    "ce_interval_volume",
    "ce_volume_counter_reset",
    "ce_ltp",
    "ce_price_change",
    "ce_price_pchange",
    "ce_iv",
    "ce_iv_valid",
    "ce_bid",
    "ce_bid_qty",
    "ce_ask",
    "ce_ask_qty",
    "ce_total_buy_qty",
    "ce_total_sell_qty",
    "ce_mid",
    "ce_spread",
    "ce_spread_pct",
    "ce_intrinsic",
    "ce_extrinsic_ltp",
    # PE source fields
    "pe_identifier",
    "pe_oi",
    "pe_exchange_change_oi",
    "pe_exchange_pchange_oi",
    "pe_interval_oi_change",
    "pe_volume_cumulative",
    "pe_interval_volume",
    "pe_volume_counter_reset",
    "pe_ltp",
    "pe_price_change",
    "pe_price_pchange",
    "pe_iv",
    "pe_iv_valid",
    "pe_bid",
    "pe_bid_qty",
    "pe_ask",
    "pe_ask_qty",
    "pe_total_buy_qty",
    "pe_total_sell_qty",
    "pe_mid",
    "pe_spread",
    "pe_spread_pct",
    "pe_intrinsic",
    "pe_extrinsic_ltp",
    # Derived strike metrics
    "strike_oi_pcr",
    "exchange_change_oi_ce_minus_pe",
]

CONTEXT_COLUMNS = [
    "schema_version",
    "snapshot_id",
    "capture_timestamp",
    "source_timestamp",
    "timezone",
    "source",
    "expiry",
    "chain_hash",
    "source_age_seconds",
    "source_is_stale",
    "spot_chain",
    # FIX-3: chain freshness divergence, session clock and true 1-min spot OHLC
    # from intra-minute ticks. These let the decision layer detect exactly the
    # failure mode seen on 2026-08-14 (context updating while chain froze).
    "chain_data_age_seconds",
    "chain_frozen",
    "minutes_to_close",
    "spot_high_1m",
    "spot_low_1m",
    "spot_samples_1m",
    "nifty_index_last",
    "nifty_day_open",
    "nifty_day_high",
    "nifty_day_low",
    "nifty_previous_close",
    "nifty_year_high",
    "nifty_year_low",
    "india_vix",
    "vix_day_open",
    "vix_day_high",
    "vix_day_low",
    "vix_previous_close",
    "current_atm",
    "session_anchor_atm",
    "scheduled_event_note",
    # Optional broker/data-feed fields
    "external_source",
    "external_source_timestamp",
    "external_age_seconds",
    "external_is_stale",
    "nifty_futures",
    "futures_open_1m",
    "futures_high_1m",
    "futures_low_1m",
    "futures_close_1m",
    "futures_volume_1m",
    "futures_vwap",
    "true_futures_context_available",
]

SUMMARY_COLUMNS = [
    "schema_version",
    "snapshot_id",
    "capture_timestamp",
    "source_timestamp",
    "expiry",
    "spot",
    "current_atm",
    "session_anchor_atm",
    "range_low",
    "range_high",
    "rows_captured",
    "source_is_stale",
    "nifty_futures",
    "futures_vwap",
    "india_vix",
    "range_ce_oi",
    "range_pe_oi",
    "range_oi_pcr",
    "range_ce_exchange_change_oi",
    "range_pe_exchange_change_oi",
    "range_exchange_change_oi_pcr",
    "range_ce_interval_oi_change",
    "range_pe_interval_oi_change",
    "range_ce_interval_volume",
    "range_pe_interval_volume",
    "max_ce_oi_strike",
    "max_ce_oi",
    "max_pe_oi_strike",
    "max_pe_oi",
    "max_ce_interval_add_strike",
    "max_ce_interval_add",
    "max_pe_interval_add_strike",
    "max_pe_interval_add",
    "atm_ce_ltp",
    "atm_pe_ltp",
    "atm_straddle_ltp",
    "atm_ce_bid",
    "atm_ce_ask",
    "atm_pe_bid",
    "atm_pe_ask",
    "atm_straddle_bid",
    "atm_straddle_ask",
    "atm_straddle_mid",
    "atm_ce_iv",
    "atm_pe_iv",
    "atm_quotes_executable",
    "true_futures_context_available",
    "trade_ready_data",
    "quality_notes",
]


# =============================================================================
# GENERIC HELPERS
# =============================================================================


def now_ist() -> dt.datetime:
    return dt.datetime.now(tz=IST)


def iso_or_none(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def number(value: Any, integer: bool = False) -> Optional[float | int]:
    """Parse a numeric source value without changing missing values to zero."""
    if value is None:
        return None
    if isinstance(value, bool):
        # L5-FIX: never coerce a Python bool into 1.0/0.0; NSE numeric fields
        # are not booleans.
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value in {"", "-", "--", "NA", "N/A", "null", "None"}:
            return None
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return int(parsed) if integer else parsed
    except (TypeError, ValueError):
        return None


def safe_add(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a + b


def safe_sub(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a - b


def safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def round_to_strike(price: float) -> int:
    # Avoid Python's banker's rounding at exact half-strike values.
    return int(math.floor(price / STRIKE_GAP + 0.5) * STRIKE_GAP)


def parse_nse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y %H:%M",
    ):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    try:
        parsed = dt.datetime.fromisoformat(text)
        return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)
    except ValueError:
        return None


def parse_expiry(value: str) -> Optional[dt.date]:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def seconds_age(reference: dt.datetime, capture: dt.datetime) -> Optional[float]:
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=IST)
    return round((capture - reference).total_seconds(), 3)


def hash_payload(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def nifty_is_trading_day(d: dt.date) -> bool:
    """True if `d` is a normal NSE equities trading day: a weekday AND not a
    declared trading holiday. Weekend holidays fall out of the weekday test;
    Muhurat-trading days are NOT normal trading days."""
    if d.weekday() >= 5:
        return False
    return d not in NIFTY_TRADING_HOLIDAYS_2026


def is_nominal_market_hours(moment: dt.datetime) -> bool:
    if not nifty_is_trading_day(moment.date()):
        return False
    return MARKET_OPEN <= moment.timetz().replace(tzinfo=None) <= MARKET_CLOSE


def _session_dir(output_dir: Path, date_label: str) -> Path:
    """Per-day data folder: <output_dir>/<date_label>/ ."""
    return output_dir / date_label


def atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _fmt_value(value: Any) -> Any:
    """R1-FIX: round floats to CSV_FLOAT_DP decimals before writing. NSE
    returns floats like 0.2335732042391732; storing 12+ digits of noise
    bloats the option-chain CSV to tens of MB with no information gain.
    4 dp keeps prices (2 dp), IV (2 dp) and ratios (4 dp) lossless for this
    engine, while roughly halving the file size."""
    if isinstance(value, float) and math.isfinite(value):
        return round(value, CSV_FLOAT_DP)
    return value


def append_csv(path: Path, rows: Iterable[Dict[str, Any]], columns: List[str]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({column: _fmt_value(row.get(column)) for column in columns})
        handle.flush()
        os.fsync(handle.fileno())
    return len(rows)





def one_index(all_indices: Dict[str, Any], name: str) -> Dict[str, Any]:
    for item in all_indices.get("data", []) if isinstance(all_indices, dict) else []:
        if item.get("index") == name or item.get("indexSymbol") == name:
            return item
    return {}


def one_index_ci(all_indices: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Case/whitespace-insensitive variant of one_index(). NSE's allIndices
    names are not guaranteed to match our exact casing/spacing across API
    versions; this keeps the sector capture robust to that drift."""
    key = "".join(str(name).split()).lower()
    for item in all_indices.get("data", []) if isinstance(all_indices, dict) else []:
        for field in ("index", "indexSymbol"):
            value = item.get(field)
            if value is not None and "".join(str(value).split()).lower() == key:
                return item
    return {}


def _index_pchange(item: Dict[str, Any]) -> Optional[float]:
    """Percent change vs previous close, computed from authoritative fields so
    we do not depend on the exact pchange field name in the NSE payload."""
    last = number(item.get("last"))
    prev = number(item.get("previousClose"))
    if last is not None and prev and prev > 0:
        return round((last - prev) / prev * 100.0, 2)
    return None


def _build_sector_row(indices: Dict[str, Any], snapshot_id: str,
                      capture: dt.datetime, source_timestamp: Optional[str]) -> Optional[Dict[str, Any]]:
    nifty = one_index_ci(indices, INDEX_NAME)
    if not number(nifty.get("last")):
        return None  # no usable allIndices payload (offline/blocked): skip
    row = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "capture_timestamp": iso_or_none(capture),
        "source_timestamp": source_timestamp,
        "timezone": "Asia/Kolkata",
        "source": "NSE",
        "nifty_last": number(nifty.get("last")),
        "nifty_pchange": _index_pchange(nifty),
    }
    for name, key in SECTOR_INDICES:
        it = one_index_ci(indices, name)
        row[f"{key}_last"] = number(it.get("last"))
        row[f"{key}_pchange"] = _index_pchange(it)
    return row


def option_mid_spread(bid: Optional[float], ask: Optional[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
        return None, None, None
    midpoint = (bid + ask) / 2.0
    spread = ask - bid
    spread_pct = spread / midpoint if midpoint > 0 else None
    return midpoint, spread, spread_pct


def infer_moneyness(strike: float, spot: float) -> str:
    if abs(strike - spot) < STRIKE_GAP / 2:
        return "ATM"
    return "BELOW_SPOT" if strike < spot else "ABOVE_SPOT"


# =============================================================================



def _build_summary(
    rows: List[Dict[str, Any]],
    context: Dict[str, Any],
    range_low: int,
    range_high: int,
    source_is_stale: bool,
    true_futures_context: bool,
    test_mode: bool = False,
) -> Dict[str, Any]:
    def total(field: str) -> Optional[float]:
        values = [float(row[field]) for row in rows if row.get(field) is not None]
        return sum(values) if values else None

    def maximum(
        field: str, *, positive_only: bool = False
    ) -> Tuple[Optional[int], Optional[float]]:
        valid = [row for row in rows if row.get(field) is not None]
        if positive_only:
            valid = [row for row in valid if float(row[field]) > 0]
        if not valid:
            return None, None
        winner = max(valid, key=lambda row: float(row[field]))
        return int(winner["strike"]), float(winner[field])

    atm = int(context["current_atm"])
    atm_row = next((row for row in rows if int(row["strike"]) == atm), {})

    ce_oi = total("ce_oi")
    pe_oi = total("pe_oi")
    ce_chg = total("ce_exchange_change_oi")
    pe_chg = total("pe_exchange_change_oi")
    ce_int_oi = total("ce_interval_oi_change")
    pe_int_oi = total("pe_interval_oi_change")
    ce_int_vol = total("ce_interval_volume")
    pe_int_vol = total("pe_interval_volume")
    max_ce_strike, max_ce = maximum("ce_oi")
    max_pe_strike, max_pe = maximum("pe_oi")
    max_ce_add_strike, max_ce_add = maximum("ce_interval_oi_change", positive_only=True)
    max_pe_add_strike, max_pe_add = maximum("pe_interval_oi_change", positive_only=True)

    atm_ce_ltp = atm_row.get("ce_ltp")
    atm_pe_ltp = atm_row.get("pe_ltp")
    atm_ce_bid = atm_row.get("ce_bid")
    atm_ce_ask = atm_row.get("ce_ask")
    atm_pe_bid = atm_row.get("pe_bid")
    atm_pe_ask = atm_row.get("pe_ask")
    straddle_bid = safe_add(atm_ce_bid, atm_pe_bid)
    straddle_ask = safe_add(atm_ce_ask, atm_pe_ask)
    straddle_mid = (
        (straddle_bid + straddle_ask) / 2
        if straddle_bid is not None and straddle_ask is not None
        else None
    )
    executable = all(
        value is not None and value > 0
        for value in (atm_ce_bid, atm_ce_ask, atm_pe_bid, atm_pe_ask)
    ) and atm_ce_ask >= atm_ce_bid and atm_pe_ask >= atm_pe_bid

    quality_notes: List[str] = []
    if test_mode:
        quality_notes.append("TEST-MODE capture (off-hours testing)")
    if source_is_stale:
        quality_notes.append("NSE source timestamp stale/invalid for live-entry use")
    if not executable:
        quality_notes.append("ATM bid/ask incomplete or non-executable")
    if not true_futures_context:
        quality_notes.append("true one-minute NIFTY futures OHLCV/VWAP unavailable")
    if atm_row.get("ce_iv_valid") is not True or atm_row.get("pe_iv_valid") is not True:
        quality_notes.append("ATM IV missing or zero")
    if not SCHEDULED_EVENT:
        quality_notes.append("scheduled-event status not supplied")

    # M7-FIX: readiness for THIS engine's needs = fresh chain + executable
    # quotes + valid IV. Futures OHLCV/VWAP and the scheduled-event feed
    # are OPTIONAL context (the prompt says their absence reduces
    # confidence, it does not auto-reject). They are flagged separately in
    # quality_notes, so trade_ready_data is no longer permanently False.
    trade_ready = (
        not source_is_stale
        and executable
        and atm_row.get("ce_iv_valid") is True
        and atm_row.get("pe_iv_valid") is True
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": context["snapshot_id"],
        "capture_timestamp": context["capture_timestamp"],
        "source_timestamp": context["source_timestamp"],
        "expiry": context["expiry"],
        "spot": context["spot_chain"],
        "current_atm": context["current_atm"],
        "session_anchor_atm": context["session_anchor_atm"],
        "range_low": range_low,
        "range_high": range_high,
        "rows_captured": len(rows),
        "source_is_stale": source_is_stale,
        "nifty_futures": context.get("nifty_futures"),
        "futures_vwap": context.get("futures_vwap"),
        "india_vix": context.get("india_vix"),
        "range_ce_oi": ce_oi,
        "range_pe_oi": pe_oi,
        "range_oi_pcr": safe_ratio(pe_oi, ce_oi),
        "range_ce_exchange_change_oi": ce_chg,
        "range_pe_exchange_change_oi": pe_chg,
        "range_exchange_change_oi_pcr": safe_ratio(pe_chg, ce_chg),
        "range_ce_interval_oi_change": ce_int_oi,
        "range_pe_interval_oi_change": pe_int_oi,
        "range_ce_interval_volume": ce_int_vol,
        "range_pe_interval_volume": pe_int_vol,
        "max_ce_oi_strike": max_ce_strike,
        "max_ce_oi": max_ce,
        "max_pe_oi_strike": max_pe_strike,
        "max_pe_oi": max_pe,
        "max_ce_interval_add_strike": max_ce_add_strike,
        "max_ce_interval_add": max_ce_add,
        "max_pe_interval_add_strike": max_pe_add_strike,
        "max_pe_interval_add": max_pe_add,
        "atm_ce_ltp": atm_ce_ltp,
        "atm_pe_ltp": atm_pe_ltp,
        "atm_straddle_ltp": safe_add(atm_ce_ltp, atm_pe_ltp),
        "atm_ce_bid": atm_ce_bid,
        "atm_ce_ask": atm_ce_ask,
        "atm_pe_bid": atm_pe_bid,
        "atm_pe_ask": atm_pe_ask,
        "atm_straddle_bid": straddle_bid,
        "atm_straddle_ask": straddle_ask,
        "atm_straddle_mid": straddle_mid,
        "atm_ce_iv": atm_row.get("ce_iv"),
        "atm_pe_iv": atm_row.get("pe_iv"),
        "atm_quotes_executable": executable,
        "true_futures_context_available": true_futures_context,
        "trade_ready_data": trade_ready,
        "quality_notes": "; ".join(quality_notes) if quality_notes else "OK",
    }




# =============================================================================
# LLM ANALYTICS / DECISION PACKET
# =============================================================================

ANALYTICS_SCHEMA = "3.0.0"
MAX_OPTION_SPREAD_PCT = 0.005
MAX_UNDERLYING_RISK = 25.0
MIN_TARGET_2 = 50.0
MIN_REWARD_RISK = 2.0
# V9: MODE-A now has a target-1 so the paper engine can scale out (bank a
# partial) instead of giving a favourable run all the way back to the stop.
# V16 (friction): raised from 15 -> 25. A 15-pt T1 with a ₹60/lot cost grosses
# ₹375 and loses 16% to drag. A 25-pt T1 (₹625 gross) cuts drag to <10%, and the
# 45-60-pt T2 keeps it near-negligible.
MODE_A_T1_POINTS = 25.0
# V10.2: volatility-adaptive MODE-A target floor. When the session's realized
# p95 net move (20-obs window) is below 50 pts, the MODE-A T2 is capped to that
# p95 (floored at MODE_A_T2_MIN) so the target stays reachable on low-vol
# sessions instead of being a >p90 tail event.
# V16 (friction): floor raised 30 -> 40 so a T2 is never <40 pts, keeping the
# ₹60/lot round-trip cost below ~6% of the winning trade value and making the
# strategy robust to sub-40% win rates at the 2:1+ R:R.
MODE_A_T2_MIN = 40.0


def _read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(v: Any) -> Optional[float]:
    return number(v)


def _write_llm_compact_chain(output_dir: Path, date_label: str) -> None:
    """R2-FIX: size-optimised export for sharing with an LLM.

    The full nifty-option-chain-v2 CSV is the authoritative record (every
    strike, every capture, full precision) but grows to several MB/day, which
    is impractical to paste into an LLM. This file keeps only the last
    LLM_CHAIN_SNAPSHOTS synchronized snapshots at ATM +/- LLM_CHAIN_STRIKES
    strikes with floats rounded — typically 50-150 KB — enough for the
    decision engine's ATM +/- 3..5 strike and interval-flow analysis.
    """
    chain = _read_csv(output_dir / f"nifty-option-chain-v2-{date_label}.csv")
    context = _read_csv(output_dir / f"nifty-market-context-v2-{date_label}.csv")
    summary = _read_csv(output_dir / f"nifty-snapshot-summary-v2-{date_label}.csv")
    common = ({r["snapshot_id"] for r in summary if r.get("snapshot_id")}
              & {r["snapshot_id"] for r in context if r.get("snapshot_id")}
              & {r["snapshot_id"] for r in chain if r.get("snapshot_id")})
    if not common:
        return
    sids = set(sorted(common)[-LLM_CHAIN_SNAPSHOTS:])
    latest_sid = max(common)
    atm = None
    for r in summary:
        if r.get("snapshot_id") == latest_sid:
            atm = _f(r.get("current_atm"))
            break
    if atm is None:
        return
    lo = int(atm) - 50 * LLM_CHAIN_STRIKES
    hi = int(atm) + 50 * LLM_CHAIN_STRIKES
    path = output_dir / f"nifty-option-chain-llm-{date_label}.csv"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHAIN_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in chain:
            strike = _f(r.get("strike"))
            if r.get("snapshot_id") in sids and strike is not None and lo <= strike <= hi:
                writer.writerow({c: _fmt_value(r.get(c)) for c in CHAIN_COLUMNS})
    os.replace(tmp, path)


def _common_snapshot_validation(chain_rows, context_rows, summary_rows):
    maps = []
    for rows in (chain_rows, context_rows, summary_rows):
        maps.append({r.get("snapshot_id"): r for r in rows if r.get("snapshot_id")})
    ids = set(maps[0]) & set(maps[1]) & set(maps[2])
    issues = []
    for sid in ids:
        ts = [m[sid].get("source_timestamp") for m in maps]
        if len(set(ts)) != 1:
            issues.append(f"source_timestamp mismatch for {sid}")
        ex = [m[sid].get("expiry") for m in maps]
        if len(set(ex)) != 1:
            issues.append(f"expiry mismatch for {sid}")
    ordered = sorted((m for m in maps[0].values() if m.get("source_timestamp")),
                     key=lambda x: x.get("source_timestamp"))
    timestamps = [x.get("source_timestamp") for x in ordered]
    if timestamps != sorted(set(timestamps)):
        issues.append("duplicate or out-of-order source timestamps")
    return {"common_snapshot_count": len(ids), "latest_common_snapshot_id": max(ids) if ids else None,
            "latest_common_source_timestamp": max((maps[0][i].get("source_timestamp") for i in ids), default=None),
            "files_in_sync": not issues, "validation_issues": issues}


def _rolling_price_features(summary_rows):
    rows = []
    for r in summary_rows:
        if r.get("source_timestamp") and _f(r.get("spot")) is not None:
            t = parse_nse_timestamp(r["source_timestamp"])
            if t is None:  # AUDIT-F3: unparseable timestamp must not crash the cutoff math
                continue
            x = dict(r); x["_t"] = t; x["_spot"] = _f(r["spot"]); rows.append(x)
    rows.sort(key=lambda x: x["_t"] or dt.datetime.min.replace(tzinfo=IST))
    if not rows: return {}
    last = rows[-1]; out = {"latest_spot": last["_spot"]}
    for minutes in (3, 5, 10, 15, 30):
        cutoff = last["_t"] - dt.timedelta(minutes=minutes)
        q = [x for x in rows if x["_t"] >= cutoff]
        vals = [x["_spot"] for x in q]
        out[f"{minutes}m"] = {"high": max(vals), "low": min(vals),
                              "range": max(vals)-min(vals),
                              "net_change": last["_spot"]-q[0]["_spot"], "observations": len(q)}
    r5, r30 = out.get("5m", {}).get("range"), out.get("30m", {}).get("range")
    out["price_state"] = "compression" if r5 is not None and r30 and r5 <= max(5.0, 0.35*r30) else "expansion_or_trend"
    return out


def _swing_levels(summary_rows, spot):
    vals = []
    for r in summary_rows:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None: vals.append((t,v))
    vals.sort()
    highs=[]; lows=[]
    # FIX-7: 1-neighbour local extrema were producing noise "swing" levels 2-3
    # points from price (observed 2026-08-14 15:17: ceiling 24357.2 -> 2.35-pt
    # stop). Require SWING_PIVOT_WINDOW bars on BOTH sides before a point is
    # accepted as a swing, and only return levels at a structurally usable
    # distance (>= MIN_STRUCTURAL_STOP_POINTS) for stop placement.
    w = SWING_PIVOT_WINDOW
    for i in range(w, len(vals)-w):
        left = [v for _, v in vals[i-w:i]]
        right = [v for _, v in vals[i+1:i+w+1]]
        v = vals[i][1]
        # V13 fix (pivot noise): STRICT comparisons. A flat tape of equal prints
        # previously registered EVERY print as both a swing high and a swing low,
        # putting stops directly on top of the entry. Strict > / < means a bar must
        # be STRICTLY above all neighbours to be a swing high (and strictly below
        # for a swing low), so consolidation produces no pivots -> no noise stops.
        if v > max(left) and v > max(right): highs.append(vals[i])
        if v < min(left) and v < min(right): lows.append(vals[i])
    floor = max((v for t,v in lows if v <= spot), default=None)
    ceiling = min((v for t,v in highs if v >= spot), default=None)
    # Nearest high BELOW spot = last broken resistance (breakout-confirmation
    # trigger); nearest low ABOVE spot = last broken support (breakdown
    # confirmation). Without these, price at fresh highs/lows has no trigger.
    high_below = max((v for t,v in highs if v < spot), default=None)
    low_above = min((v for t,v in lows if v > spot), default=None)
    floor_dist = spot-floor if floor is not None else None
    ceil_dist = ceiling-spot if ceiling is not None else None
    # V2.6-1: TREND-MODE RESCUE. Windowed pivots need 3 bars of recovery on
    # each side, so in a MONOTONIC trend almost no pivots confirm and the
    # trigger/stop system goes blind precisely when the trend is strongest
    # (2026-08-12 hour 9: -98 pts in 45 min, only 2 swing highs confirmed).
    # Provisional rolling levels (10 prior bars) supplement the pivots:
    #   provisional ceiling = max(previous 10 bars), floor = min(previous 10).
    if len(vals) >= 11:
        prev10 = [v for _, v in vals[-11:-1]]
        prov_ceil = max(prev10)
        prov_floor = min(prev10)
    else:
        prov_ceil = prov_floor = None
    return {"nearest_confirmed_floor": floor, "nearest_confirmed_ceiling": ceiling,
            "nearest_high_below": high_below, "nearest_low_above": low_above,
            "floor_distance": floor_dist, "ceiling_distance": ceil_dist,
            "floor_stop_usable": floor_dist is not None and floor_dist >= MIN_STRUCTURAL_STOP_POINTS,
            "ceiling_stop_usable": ceil_dist is not None and ceil_dist >= MIN_STRUCTURAL_STOP_POINTS,
            "provisional_floor_10": prov_floor, "provisional_ceiling_10": prov_ceil,
            "swing_high_count": len(highs), "swing_low_count": len(lows)}


def _atm_flow(chain_rows, latest_sid, atm):
    # AUDIT-M3: each snapshot's ATM band is derived from THAT snapshot's
    # current_atm, not today's ATM applied to the whole history (a moving
    # spot otherwise makes "the ATM basket" a different set of contracts).
    by_sid={}
    for r in chain_rows:
        sid = r.get("snapshot_id")
        if not sid:
            continue
        snap_atm = _f(r.get("current_atm"))
        if snap_atm is None:
            snap_atm = atm
        if abs((_f(r.get("strike")) or 0) - snap_atm) <= 150:
            by_sid.setdefault(sid, []).append(r)
    ids=sorted(by_sid)
    records=[]
    for sid in ids:
        q=by_sid[sid]
        def total(k):
            z=[_f(x.get(k)) for x in q if _f(x.get(k)) is not None]; return sum(z) if z else None
        records.append({"snapshot_id":sid,"ce_interval_oi":total("ce_interval_oi_change"),"pe_interval_oi":total("pe_interval_oi_change"),
                        "ce_volume":total("ce_interval_volume"),"pe_volume":total("pe_interval_volume"),
                        "ce_ltp_sum":total("ce_ltp"),"pe_ltp_sum":total("pe_ltp"),
                        "spot": _f(q[0].get("spot")) if q else None})
    for i in range(1, len(records)):
        if records[i]["ce_ltp_sum"] is not None and records[i-1]["ce_ltp_sum"] is not None:
            records[i]["ce_prem_d"] = round(records[i]["ce_ltp_sum"] - records[i-1]["ce_ltp_sum"], 2)
        if records[i]["pe_ltp_sum"] is not None and records[i-1]["pe_ltp_sum"] is not None:
            records[i]["pe_prem_d"] = round(records[i]["pe_ltp_sum"] - records[i-1]["pe_ltp_sum"], 2)
        if records[i]["spot"] is not None and records[i-1]["spot"] is not None:
            records[i]["spot_d"] = round(records[i]["spot"] - records[i-1]["spot"], 2)
    # AUDIT-S4: observed premium elasticity (sum of ATM±150 premiums vs spot).
    # A directional leg's premium move must EXCEED its mechanical
    # intrinsic-consistent amount (elasticity x |spot move|); otherwise the
    # premium condition is tautological with "spot went up/down". No Greeks
    # are invented — the elasticity is measured from synchronized snapshots.
    # V13 fix (unbounded elasticity): require a MINIMUM spot move per sample
    # before it can estimate elasticity. A 0.5-pt drift with a 2.5-pt premium
    # bounce used to produce elasticity ~5.0 (a 500% "delta"), which then forced
    # all future flow to outpace an impossible threshold and permanently blocked
    # valid intervals. Only samples with |spot_d| >= MIN_ELASTICITY_SPOT_MOVE count,
    # and the estimate is capped at MAX_ELASTICITY so noise can never dominate.
    elasticity_samples = [r for r in records
                          if r.get("spot_d") is not None and r.get("ce_prem_d") is not None
                          and abs(r.get("spot_d")) >= MIN_ELASTICITY_SPOT_MOVE]
    sum_up_spot = sum(x["spot_d"] for x in elasticity_samples if x["spot_d"] > 0)
    sum_up_ce = sum(x["ce_prem_d"] for x in elasticity_samples if x["spot_d"] > 0)
    sum_dn_spot = sum(-x["spot_d"] for x in elasticity_samples if x["spot_d"] < 0)
    sum_dn_pe = sum(x["pe_prem_d"] for x in elasticity_samples if x["spot_d"] < 0 and x.get("pe_prem_d") is not None)
    ce_elast = (sum_up_ce / sum_up_spot) if sum_up_spot > 1e-9 else None
    pe_elast = (sum_dn_pe / sum_dn_spot) if sum_dn_spot > 1e-9 else None
    # V9 fix: NO fabricated fallback. Previously an unavailable measurement
    # defaulted to a hard-coded 2.5 "delta proxy" that silently fed the OI-flow
    # confirmation gate. A made-up number must never drive a trade gate: when
    # elasticity cannot be measured, the premium non-tautology test degrades to
    # a plain directional premium check instead of assuming an invented 2.5.
    ce_elast = ce_elast if (ce_elast and ce_elast > 0 and ce_elast <= MAX_ELASTICITY) else None
    pe_elast = pe_elast if (pe_elast and pe_elast > 0 and pe_elast <= MAX_ELASTICITY) else None
    recent=records[-5:]
    # FIX-8 + V2.3: confirmation requires a CONSECUTIVE RUN of
    # TRIGGER_OBSERVATIONS intervals with the same directional signature.
    # V2.3 (day-audit driven): an OI leg now counts ONLY with premium
    # corroboration (per the prompt's own OI-interpretation examples):
    #   bullish: CE OI down AND CE premium up, or PE OI up AND PE premium down
    #   bearish: PE OI down AND PE premium up, or CE OI up AND CE premium down
    # OI moves with flat/absent premium response = mass positioning, NEUTRAL.
    # (The 2026-08-14 replay showed raw PE-OI adds in the falling morning
    # being misread as bullish flow -> 6 false BUY CALLs, 6 stop-outs.)
    # V2.6-3 + AUDIT-S4 (v3): CUMULATIVE-WINDOW flow confirmation. The chain
    # feed's premium snapshots lag the spot prints (options repriced 1-2 min
    # later), so per-leg time-aligned tests systematically reject fast legs
    # (measured: the 2026-08-12 10:48 winner had a -11.1 pt spot leg with
    # almost no premium move yet). The test therefore works on the trailing
    # confirmation window:
    #   direction: net spot move over the window must be in the claimed
    #     direction;
    #   non-tautology: across the SAME window, at least one premium axis
    #     (CE up / PE down for bull; PE up / CE down for bear) must
    #     CUMULATIVELY outpace its mechanical need (elasticity x |net spot|).
    # Premium simply mirroring delta does not pass; genuine demand/IV bid or
    # hedging pressure does.
    def _window_ok(bullish: bool) -> bool:
        win = recent[-TRIGGER_OBSERVATIONS:]
        sd = sum(x.get("spot_d") or 0 for x in win)
        if bullish and sd <= 0:
            return False
        if not bullish and sd >= 0:
            return False
        ce_cum = sum(x.get("ce_prem_d") or 0 for x in win)
        pe_cum = sum(x.get("pe_prem_d") or 0 for x in win)
        if ce_elast is not None and pe_elast is not None:
            need_ce = ce_elast * abs(sd)
            need_pe = pe_elast * abs(sd)
            if bullish:
                return bool(max(ce_cum - need_ce, -pe_cum - need_pe) >= 0)
            return bool(max(pe_cum - need_pe, -ce_cum - need_ce) >= 0)
        # V9: elasticity unmeasured - no fabricated delta proxy. Require plain
        # directional premium corroboration instead (still not tautological with
        # the spot move being claimed).
        if bullish:
            return bool(ce_cum > 0 or pe_cum < 0)
        return bool(pe_cum > 0 or ce_cum < 0)
    bull_ok = _window_ok(True)
    bear_ok = _window_ok(False)
    bullish = 1 if bull_ok else 0
    bearish = 1 if bear_ok else 0
    return {"recent":recent, "bullish_compatible_count":bullish, "bearish_compatible_count":bearish,
            "two_observation_bullish":bull_ok,
            "two_observation_bearish":bear_ok,
            "ce_elasticity": round(ce_elast, 2) if ce_elast is not None else None,
            "pe_elasticity": round(pe_elast, 2) if pe_elast is not None else None,
            "elasticity_basis": "sum of premiums across the ATM +/- 150 basket (7 strikes); NOT an ATM delta and can exceed 1.0",
            "note": "cumulative-window premium confirmation: net premium must outpace mechanical need on one axis; no Greeks invented"}


def _quote(candidate, side):
    bid=_f(candidate.get(f"{side}_bid")); ask=_f(candidate.get(f"{side}_ask")); mid=_f(candidate.get(f"{side}_mid"))
    spread_pct=(ask-bid)/mid if bid is not None and ask is not None and mid else None
    spread_abs=(ask-bid) if bid is not None and ask is not None else None
    # AUDIT-M5: executable requires BOTH the 0.5% relative rule AND an
    # absolute spread cap (a cheap option passes % but a deep-ITM option with
    # a 5-pt spread is not executable).
    executable = (bid is not None and ask is not None and bid>0 and ask>=bid
                  and spread_pct is not None and spread_pct<=MAX_OPTION_SPREAD_PCT
                  and spread_abs is not None and spread_abs<=MAX_OPTION_SPREAD_ABS)
    return {"bid":bid,"ask":ask,"mid":mid,"spread_pct":spread_pct,"spread_abs":spread_abs,
            "executable":executable}



def _enhanced_features(summary_rows, chain_rows, latest_sid, spot, atm, source_age=None):
    """Add non-lookahead, execution-oriented features to the LLM packet."""
    ordered=[]
    for r in summary_rows:
        t=parse_nse_timestamp(r.get("source_timestamp")); v=_f(r.get("spot"))
        if t and v is not None: ordered.append((t,v,r))
    ordered.sort(key=lambda x:x[0])
    vals=[x[1] for x in ordered]
    diffs=[vals[i]-vals[i-1] for i in range(1,len(vals))]
    atr_proxy=sum(abs(x) for x in diffs[-14:])/max(1,len(diffs[-14:]))
    ema={}
    for period in (3,5,8,13,21):
        if vals:
            alpha=2/(period+1); e=vals[0]
            for v in vals[1:]: e=alpha*v+(1-alpha)*e
            ema[str(period)]=round(e,2)
    slope5=(ema.get("5")-ema.get("13")) if ema.get("5") is not None and ema.get("13") is not None else None
    q=[r for r in chain_rows if r.get("snapshot_id")==latest_sid]
    ranked=[]
    for r in q:
        strike=_f(r.get("strike"));
        if strike is None or abs(strike-atm)>100: continue
        for side, label in (("ce","CALL"),("pe","PUT")):
            bid=_f(r.get(f"{side}_bid")); ask=_f(r.get(f"{side}_ask")); mid=_f(r.get(f"{side}_mid")); oi=_f(r.get(f"{side}_oi")); vol=_f(r.get(f"{side}_interval_volume"))
            spread=((ask-bid)/mid) if bid is not None and ask is not None and mid else None
            if bid and ask and mid and spread is not None and spread<=MAX_OPTION_SPREAD_PCT:
                distance=abs(strike-spot)
                # V13 fix (candidate overweighting): the old distance term
                # `1000/(1+distance)` was ~1000 at ATM vs liquidity terms that top
                # out around 10-14, so the engine always forced the exact-ATM
                # strike even when it was illiquid during fast moves. The distance
                # bonus is now NORMALISED (out of 50-pt strike gaps) and capped, so
                # OI + interval volume genuinely influence selection while ATM still
                # keeps a modest, bounded preference.
                distance_bonus = 10.0 * (1.0 / (1.0 + distance / 50.0))
                liq = math.log1p(max(oi or 0, 0)) + 2.0 * math.log1p(max(vol or 0, 0))
                score = distance_bonus + liq
                ranked.append({"direction":label,"strike":int(strike),"entry_ask":ask,"exit_bid":bid,"mid":mid,"spread_pct":spread,"oi":oi,"interval_volume":vol,"selection_score":round(score,2)})
    ranked=sorted(ranked,key=lambda x:x["selection_score"],reverse=True)[:6]
    regime="trend_up" if slope5 is not None and slope5>1 else "trend_down" if slope5 is not None and slope5<-1 else "range_or_compression"
    latest=ordered[-1][2] if ordered else {}
    quality=[]
    # AUDIT-B5: source_age_seconds lives in the CONTEXT file, not summary;
    # the caller now passes the joined value. Only flag unavailability when
    # the caller could not resolve it either.
    age = source_age if source_age is not None else _f(latest.get("source_age_seconds"))
    if age is None: quality.append("source_age_unavailable")
    elif age>MAX_SOURCE_AGE_SECONDS or age < -60: quality.append("source_stale")
    if latest.get("true_futures_context_available") not in (True,"True"): quality.append("futures_ohlcv_vwap_missing")
    if not SCHEDULED_EVENT: quality.append("scheduled_event_status_unverified")
    if len(ordered)<10: quality.append("short_history")
    return {"observations_used":len(ordered),"atr_proxy_points":round(atr_proxy,2),"ema":ema,"ema_5_minus_13":round(slope5,2) if slope5 is not None else None,"regime":regime,"candidate_options":ranked,"quality_flags":quality,"non_predictive_note":"Features describe current evidence; they do not predict or guarantee future movement."}


def _directional_score(packet):
    p=packet.get("price_structure",{}); f=packet.get("atm_flow",{}); b=packet.get("breakout_state",{})
    e=packet.get("enhanced_features",{}); m=packet.get("microstructure",{}); t=packet.get("tail_stats",{})
    pf=packet.get("premium_flow",{}); c=packet.get("coil",{})
    bull=50.0; bear=50.0
    net=p.get("5m",{}).get("net_change")
    if net is not None:
        bull += min(15,max(-15,net)); bear -= min(15,max(-15,net))
    bull += 12 if f.get("two_observation_bullish") else 0; bear += 12 if f.get("two_observation_bearish") else 0
    if e.get("regime")=="trend_up": bull+=8
    if e.get("regime")=="trend_down": bear+=8
    if b.get("bullish",{}).get("confirmed"): bull+=10
    if b.get("bearish",{}).get("confirmed"): bear+=10
    # V2.2: absorption / rejection evidence
    if m.get("triple_ceiling_rejection"): bear+=6
    if m.get("triple_floor_rejection"): bull+=6
    # V2.2: premium paying up
    if pf.get("ce_premium_bid"): bull+=6
    if pf.get("pe_premium_bid"): bear+=6
    # V2.2: coil resolution lean
    if c.get("resolution_lean")=="upside_resolution": bull+=6
    if c.get("resolution_lean")=="downside_resolution": bear+=6
    # V2.2: tail asymmetry
    if t.get("tail_asymmetry")=="downside_heavy": bear+=5
    if t.get("tail_asymmetry")=="upside_heavy": bull+=5
    # V2.4: behavioural terms (small; never enough to flip a decision alone)
    obp=packet.get("options_behaviour",{})
    if obp.get("pcr_regime")=="put_crowding_contrarian_bullish": bull+=3
    if obp.get("pcr_regime")=="call_crowding_contrarian_bearish": bear+=3
    if obp.get("skew_reversal"): bull+=3
    if obp.get("skew_change_30m") is not None and obp["skew_change_30m"]>=1.5: bear+=3
    if obp.get("pinning_zone"):
        if obp.get("max_pain_distance",0)>0: bear+=2   # spot above pain: pull down
        elif obp.get("max_pain_distance",0)<0: bull+=2  # spot below pain: pull up
    bull=round(max(0,min(100,bull)),1); bear=round(max(0,min(100,bear)),1)
    return {"bullish":bull,"bearish":bear,
            "conflicting":bool(bull>=58 and bear>=58),
            "role":"advisory_display_only",
            "interpretation":"evidence score, not probability of profit; the decision is made by the breakout/flow/EMA/geometry gates, not by this score"}

def _timestamp_map(rows):
    out = {}
    for r in rows:
        sid = r.get("snapshot_id")
        if sid and r.get("source_timestamp"):
            out[sid] = parse_nse_timestamp(r.get("source_timestamp"))
    return out


def _signal_ledger_path(output_dir: Path, date_label: str) -> Path:
    return output_dir / f"signals_ledger-{date_label}.csv"


def _append_signal_ledger(output_dir: Path, date_label: str, mode: str, decision: str,
                          reasons: List[str], ts: str) -> None:
    """AUDIT-M2: persistent signal ledger — every BUY decision (and its
    blocking context) is appended, so fire rate and outcomes can be audited
    at scale instead of being reconstructed from packet JSONs.

    H1-FIX: no silent swallowing — a ledger write failure now propagates to
    the caller, which logs it loudly. The daily signal cap counts ledger
    rows, so a silent failure used to undercount signals and disable the cap.
    """
    append_csv(_signal_ledger_path(output_dir, date_label),
               [{"analysis_time_ist": ts, "mode": mode, "decision": decision,
                 "reasons": ("; ".join(reasons) or "")[:400]}],
               ["analysis_time_ist", "mode", "decision", "reasons"])


def _commit_signal_records(output_dir: Path, date_label: str, mode: str, decision: str,
                           reasons: List[str], is_execution: bool) -> None:
    """H1-FIX: ledger row and last-signal state are committed together.
    The ledger is written FIRST, so a crash between the two writes can never
    hide a signal from the daily cap (it can only lose the cooldown
    timestamp, which is the safe direction). Journal-grade detections are
    ledgered but do NOT update last-signal state, so they stay auditable
    without triggering execution cooldowns."""
    ts = iso_or_none(now_ist())
    try:
        _append_signal_ledger(output_dir, date_label, mode, decision, reasons, ts)
    except Exception:
        logging.exception("SIGNAL LEDGER APPEND FAILED for %s %s", mode, decision)
    if is_execution:
        try:
            atomic_json_write(_signal_state_path(output_dir, date_label),
                              {"decision": decision, "analysis_time_ist": ts})
        except Exception:
            logging.exception("SIGNAL STATE WRITE FAILED for %s", decision)


def _append_llm_escalation(output_dir: Path, date_label: str, packet: Dict[str, Any]) -> None:
    """ESCALATION QUEUE for the LLM decision layer.

    The LLM prompt is expensive to run every minute (~375 packets/day), and
    signals are rare (0-4/day). This file lets a scheduler consume LLM work
    ONLY when needed, while the Python script keeps watching every minute.

    Levels:
      execute - a BUY signal at execution grade: run the FULL audit NOW.
      review  - a BUY that missed the 120 s execution ceiling (journal-grade),
                or another flagged setup worth a full audit.
      armed   - no signal yet, but a level has been crossed (unconfirmed) or
                flow confirmation is building: optional cheap pre-check /
                context pre-load.

    Edge-triggered: a row is written only when the level CHANGES from the last
    queued row, so a persistent "armed" state (or the frozen-chain path, which
    rebuilds the same snapshot every minute) cannot spam the queue. The
    scheduler always reads the latest nifty-llm-analysis-*.json for the
    current full state.
    """
    level = packet.get("llm_escalation")
    if not level or level == "none":
        return
    path = output_dir / f"pending_llm_review-{date_label}.jsonl"
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            if lines and json.loads(lines[-1]).get("escalation") == level:
                return  # edge-trigger dedup
        except Exception:
            pass
    record = {
        "ts": packet.get("analysis_time_ist"),
        "snapshot_id": packet.get("latest_snapshot_id_used"),
        "escalation": level,
        "decision": packet.get("decision"),
        "decision_grade": packet.get("decision_grade"),
        "spot": (packet.get("latest") or {}).get("spot"),
        "minutes_to_close": packet.get("minutes_to_close"),
        "trigger_age_seconds": packet.get("trigger_age_seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _gemini_generate(prompt_text: str) -> Tuple[str, Optional[str]]:
    """Call the Gemini generateContent REST endpoint. Raises on any failure so
    the caller can record the error; never silently returns garbage.
    Returns (text, finish_reason)."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": GEMINI_TEMPERATURE,
            "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini network error: {exc}") from exc
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:400]}")
    first = candidates[0]
    parts = (first.get("content") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return text, first.get("finishReason")


def _build_gemini_prompt(output_dir: Path, date_label: str, packet: Dict[str, Any]) -> str:
    """Attach the data inline: compact chain + summary tail + context tail +
    the engine packet JSON. Text inlining is the most reliable attachment
    method across Gemini API versions (no Files API dependency)."""
    try:
        _write_llm_compact_chain(output_dir, date_label)
    except Exception:
        pass
    sections = []
    for fname, tail in (
        (f"nifty-option-chain-llm-{date_label}.csv", 0),
        (f"nifty-snapshot-summary-v2-{date_label}.csv", 25),
        (f"nifty-market-context-v2-{date_label}.csv", 25),
    ):
        p = output_dir / fname
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        if tail > 0:
            lines = text.splitlines()
            if len(lines) > tail + 1:
                text = "\n".join(lines[:1] + lines[-(tail):])
        sections.append(f"--- {fname} ---\n{text}")
    keep_keys = (
        "decision", "decision_grade", "llm_escalation", "latest",
        "latest_snapshot_id_used", "trigger_age_seconds", "cross_age_seconds",
        "decision_source_age_seconds", "chain_gap_seconds",
        "minutes_to_close", "minutes_since_open", "price_structure",
        "swing_levels", "atm_flow", "tail_stats", "microstructure",
        "premium_flow", "options_behaviour", "anticipation", "mode_b", "breakout_state",
        "option_candidate", "risk_geometry", "reasons",
        "directional_evidence_score",
    )
    packet_json = json.dumps({k: packet[k] for k in keep_keys if k in packet}, ensure_ascii=False)
    sections.append("--- nifty-llm-analysis engine packet (JSON) ---\n" + packet_json)
    data = "\n\n".join(sections)
    template = GEMINI_PROMPT_TEMPLATE
    if GEMINI_PROMPT_PATH:
        p = Path(GEMINI_PROMPT_PATH)
        if p.exists():
            template = p.read_text(encoding="utf-8")
    return template.replace("{data}", data)


def _gemini_already_called(output_dir: Path, date_label: str, snapshot_id: Optional[str], level: str) -> bool:
    log_path = output_dir / f"gemini_decision-log-{date_label}.jsonl"
    if not log_path.exists() or not snapshot_id:
        return False
    try:
        for line in log_path.read_text(encoding="utf-8").strip().splitlines():
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("snapshot_id") == snapshot_id and rec.get("escalation") == level:
                return True
    except Exception:
        pass
    return False


def _write_gemini_result(output_dir: Path, date_label: str, packet: Dict[str, Any],
                         level: str, response_text: str, finish_reason: Optional[str] = None) -> None:
    ts = iso_or_none(now_ist())
    decision = None
    match = re.search(r"DECISION\s*:\s*(BUY CALL NOW|BUY PUT NOW|WAIT / NO TRADE)", response_text)
    if match:
        decision = match.group(1)
    record = {
        "timestamp": ts,
        "escalation": level,
        "snapshot_id": packet.get("latest_snapshot_id_used"),
        "spot": (packet.get("latest") or {}).get("spot"),
        "model": GEMINI_MODEL,
        "finish_reason": finish_reason,
        "decision": decision,
        "summary": response_text[:400],
        "raw_response": response_text,
    }
    atomic_json_write(output_dir / "gemini_decision-latest.json", record)
    log_path = output_dir / f"gemini_decision-log-{date_label}.jsonl"
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_gemini_error(output_dir: Path, date_label: str, packet: Dict[str, Any],
                        level: str, error: str) -> None:
    record = {
        "timestamp": iso_or_none(now_ist()),
        "escalation": level,
        "snapshot_id": packet.get("latest_snapshot_id_used"),
        "spot": (packet.get("latest") or {}).get("spot"),
        "model": GEMINI_MODEL,
        "decision": None,
        "error": error[:400],
        "raw_response": None,
    }
    atomic_json_write(output_dir / "gemini_decision-latest.json", record)
    log_path = output_dir / f"gemini_decision-log-{date_label}.jsonl"
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _maybe_gemini_review(output_dir: Path, date_label: str, packet: Dict[str, Any],
                         level: Optional[str]) -> None:
    """Call Gemini ONLY when the escalation level requires it (previous Python
    logic): execute -> always; review -> per GEMINI_CALL_REVIEW; armed -> per
    GEMINI_CALL_ARMED. Deduplicated per (snapshot_id, level) so the frozen-chain
    path or a cooldown rebuild cannot spam the API. Never raises."""
    if not GEMINI_ENABLED or GEMINI_SUSPENDED or not GEMINI_API_KEY or not level or level == "none":
        return
    if level == "execute":
        should_call = True
    elif level == "review":
        should_call = GEMINI_CALL_REVIEW
    elif level == "armed":
        should_call = GEMINI_CALL_ARMED
    else:
        should_call = False
    if not should_call:
        return
    snapshot_id = packet.get("latest_snapshot_id_used")
    if _gemini_already_called(output_dir, date_label, snapshot_id, level):
        return
    try:
        prompt = _build_gemini_prompt(output_dir, date_label, packet)
        response_text, finish_reason = _gemini_generate(prompt)
        _write_gemini_result(output_dir, date_label, packet, level, response_text, finish_reason)
        logging.info("Gemini review (%s) written for snapshot %s", level, snapshot_id)
    except Exception as exc:
        logging.error("Gemini review failed for snapshot %s: %s", snapshot_id, exc)
        try:
            _write_gemini_error(output_dir, date_label, packet, level, str(exc))
        except Exception:
            pass


def _signals_today_direction(output_dir: Path, date_label: str, direction: str) -> int:
    """V10: count executable signals in the ledger by direction (CALL/PUT),
    across modes. Used by the put-fatigue guard so the cap is per-side, not
    per-mode."""
    try:
        p = _signal_ledger_path(output_dir, date_label)
        if not p.exists():
            return 0
        with open(p, newline="", encoding="utf-8") as f:
            return sum(1 for r in csv.DictReader(f)
                       if r.get("decision")
                       and r["decision"].startswith("BUY")
                       and direction in r["decision"].upper())
    except Exception:
        return 0


def _signals_today(output_dir: Path, date_label: str, mode: str) -> int:
    try:
        p = _signal_ledger_path(output_dir, date_label)
        if not p.exists():
            return 0
        with open(p, newline="", encoding="utf-8") as f:
            return sum(1 for r in csv.DictReader(f) if r.get("mode") == mode)
    except Exception:
        return 0


def _signal_state_path(output_dir: Path, date_label: str) -> Path:
    # M8-FIX: per-session signal state, so a previous session's BUY can never
    # leak into this session's cooldown/cap checks.
    return output_dir / f"last_signal_state-{date_label}.json"


def _last_signal_state(output_dir: Path, date_label: str) -> Dict[str, Any]:
    try:
        p = _signal_state_path(output_dir, date_label)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _capture_map(rows):
    out = {}
    for r in rows:
        sid = r.get("snapshot_id")
        if sid and r.get("capture_timestamp"):
            out[sid] = parse_nse_timestamp(r.get("capture_timestamp"))
    return out


def _ema_pair(usable):
    vals = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None: vals.append((t, v))
    vals.sort()
    x = [v for _, v in vals]
    if len(x) < 2:
        return None, None
    def ema(xx, n):
        a = 2 / (n + 1); e = xx[0]
        for v in xx[1:]:
            e = a * v + (1 - a) * e
        return e
    return ema(x, 5), ema(x, 13)


def _trend_regime(usable, day_open, spot):
    """V11: trend-regime gate (the missing calculation). Uses ONLY data up to
    the current snapshot (no lookahead) to classify the intraday regime from
    the EMA8/EMA21 structure plus the day-open bias. The 4-day audit showed the
    engine's biggest P&L killer was trading COUNTER to the dominant intraday
    trend (shorted into 08-25's +159 UP day, bought CALLs into 08-26's -134 DOWN
    day). A no-lookahead EMA8>EMA21 + price>day-open / EMA8<EMA21 + price<day-open
    rule made money on 3 of 4 days.

    Returns ("up"|"down"|"range") plus the raw EMA8/EMA21 values. "range" means
    no confirmed trend (EMAs crossed or price straddles day open) - entries there
    are de-prioritised."""
    vals = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None: vals.append((t, v))
    vals.sort()
    x = [v for _, v in vals]
    if len(x) < 12:
        return "range", None, None
    def ema(xx, n):
        a = 2 / (n + 1); e = xx[0]
        for v in xx[1:]:
            e = a * v + (1 - a) * e
        return e
    e8 = ema(x, 8); e21 = ema(x, 21)
    if e8 is None or e21 is None:
        return "range", None, None
    # require a minimum separation so a flat/choppy tape is NOT called a trend
    sep = abs(e8 - e21)
    if spot is None or day_open is None:
        return "range", e8, e21
    if e8 > e21 and spot > day_open and sep >= 2.0:
        return "up", e8, e21
    if e8 < e21 and spot < day_open and sep >= 2.0:
        return "down", e8, e21
    return "range", e8, e21


def _dynamic_flow_quality(usable):
    """V14 (dynamic chop filter): replace the rigid OPENING_FLOW_NEUTRAL_MINUTES
    time block with a dynamic measure of whether the tape is genuinely trending or
    chopping. Localized True Range (recent N-bar range) is compared to the tick-level
    noise floor (mean absolute 1-bar change):
      - if recent_range >= TREND_RATIO * noise_floor -> TRENDING: flow is trustworthy
        regardless of time of day (trade the open on real momentum).
      - otherwise -> CHOPPING: flow should be de-weighted (fall back to the time gate).
    Returns a dict with an "ok" flag + the raw measures."""
    vals = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None: vals.append((t, v))
    vals.sort()
    x = [v for _, v in vals]
    if len(x) < OPENING_FLOW_RANGE_BARS + 1:
        return {"ok": False, "trending": False, "recent_range": None, "noise": None, "reason": "insufficient_history"}
    recent = x[-OPENING_FLOW_RANGE_BARS:]
    recent_range = max(recent) - min(recent)
    diffs = [abs(x[i] - x[i-1]) for i in range(1, len(x))]
    noise = sum(diffs[-OPENING_FLOW_RANGE_BARS:]) / max(1, len(diffs[-OPENING_FLOW_RANGE_BARS:]))
    if noise <= 0:
        return {"ok": False, "trending": False, "recent_range": recent_range, "noise": noise, "reason": "zero_noise"}
    trending = recent_range >= OPENING_FLOW_TREND_RATIO * noise
    return {"ok": trending, "trending": trending,
            "recent_range": round(recent_range, 2), "noise": round(noise, 2),
            "ratio": round(recent_range / noise, 2),
            "reason": "trending" if trending else "chopping"}


def _tail_stats(usable):
    """FIX-9a: forward-risk statistics the decision layer must see.

    On 2026-08-14 the engine reported 'compression' but never quantified how
    often a >=21-pt flush occurs in a 10-minute window (~5.5% of windows ->
    roughly once an hour). These statistics make tail risk explicit.
    """
    rows = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None: rows.append((t, v))
    rows.sort()
    vals = [v for _, v in rows]
    diffs = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    if len(vals) < 15 or not diffs:
        return {"available": False}
    # V2.5-3: HOLE-AWARENESS. A 35.6-min source gap (2026-08-13 11:32->12:08,
    # +62.7 pts across it) makes hole-spanning windows look like giant moves
    # and inflates every p95. The series is split at gaps > 300s (V2.5-4:
    # 1-2 min jitter is normal capture noise) and window statistics are
    # computed only WITHIN segments.
    segs = []; seg = [rows[0]]
    for i in range(1, len(rows)):
        gap = (rows[i][0] - rows[i - 1][0]).total_seconds()
        if gap > 300:  # V2.5-4: 5+ min = real hole; 1-2 min jitter is normal capture noise
            segs.append(seg); seg = [rows[i]]
        else:
            seg.append(rows[i])
    segs.append(seg)
    gap_count = len(segs) - 1

    def _per_segment(fn, window):
        out = []
        for seg in segs:
            sv = [v for _, v in seg]
            out.extend(fn(sv, window))
        return out

    def drawdowns(window):
        return _per_segment(lambda sv, w: [sv[i] - min(sv[i:i + w + 1]) for i in range(len(sv) - w)], window)

    def ranges(window):
        return _per_segment(lambda sv, w: [max(sv[i:i + w + 1]) - min(sv[i:i + w + 1]) for i in range(len(sv) - w)], window)

    def _percentile(s, q):
        # M6-FIX: linear-interpolation quantile. Nearest-rank made p95 == max
        # for small windows (n<=20) and over-stated tail risk.
        if not s:
            return None
        if len(s) == 1:
            return s[0]
        pos = (len(s) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (pos - lo)

    def summ(xs):
        if not xs:
            return None  # too few observations for this window size
        s = sorted(xs)
        return {"mean": round(statistics.fmean(xs), 2),
                "max": round(max(xs), 2),
                "p95": round(_percentile(s, 0.95), 2)}

    # Window stats only where enough observations exist; otherwise None.
    dd10 = drawdowns(10)
    rng15 = ranges(15)
    rng20 = ranges(20)

    def runups(window):
        return _per_segment(lambda sv, w: [max(sv[i:i + w + 1]) - sv[i] for i in range(len(sv) - w)], window)

    ru10 = runups(10)

    def net_moves(window):
        return _per_segment(lambda sv, w: [abs(sv[i + w] - sv[i]) for i in range(len(sv) - w)], window)

    net10, net15, net20 = net_moves(10), net_moves(15), net_moves(20)
    net30, net45, net60 = net_moves(30), net_moves(45), net_moves(60)
    dd10_s = summ(dd10)
    return {
        "available": True,
        "abs_1m_change_mean": round(statistics.fmean(diffs), 2),
        "abs_1m_change_p90": round(_percentile(sorted(diffs), 0.9), 2) if diffs else None,
        "abs_1m_change_max": round(max(diffs), 2),
        "drawdown_10m": dd10_s,
        "drawdown_10m_ge21_count": int(sum(1 for x in dd10 if x >= 21.0)),
        "drawdown_10m_windows": len(dd10),
        "runup_10m": summ(ru10),
        "range_15m": summ(rng15),
        "range_15m_ge50_count": int(sum(1 for x in rng15 if x >= 50.0)),
        "range_20m": summ(rng20),
        "abs_net_move_10m": summ(net10),
        "abs_net_move_15m": summ(net15),
        "abs_net_move_20m": summ(net20),
        "abs_net_move_30m": summ(net30),
        "abs_net_move_45m": summ(net45),
        "abs_net_move_60m": summ(net60),
        "flush_risk_flag": bool(dd10_s and dd10_s.get("p95") is not None and dd10_s["p95"] >= 18.0),
        "data_holes_gaps_gt_300s": gap_count,
        "window_basis": "observation_counts",  # AUDIT-H7: windows are N observations, not N minutes
        "window_basis_note": "Window sizes (10/15/20/30/45/60) count OBSERVATIONS. With irregular capture cadence or sub-minute prints, an N-observation window spans more than N minutes; treat 'm' suffixes as approximate.",
        "fresh_extreme_basis_note": "new_high_30/new_low_30 compare against the previous 30 OBSERVATIONS, not 30 minutes.",
        "tail_asymmetry": (
            "downside_heavy" if dd10_s and summ(ru10) and dd10_s.get("p95") is not None and summ(ru10).get("p95") is not None and dd10_s["p95"] > 1.3 * summ(ru10)["p95"]
            else "upside_heavy" if summ(ru10) and dd10_s and summ(ru10).get("p95") is not None and dd10_s.get("p95") is not None and summ(ru10)["p95"] > 1.3 * dd10_s["p95"]
            else "symmetric"),
        "non_predictive_note": "Tail statistics describe historical frequency, not a prediction.",
    }


def _load_ticks(output_dir: Path, date_label: str) -> List[Dict[str, Any]]:
    path = output_dir / f"nifty-spot-ticks-v2-{date_label}.csv"
    if not path.exists():
        return []
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def _trigger_prints(usable, level, above, spot, ticks=None):
    """FIX-9b + V2.2: a breakout/breakdown is confirmed only by
    TRIGGER_OBSERVATIONS observations beyond the level with a minimum time
    spacing (two ticks 20 s apart do NOT confirm; minute prints do). When the
    intra-minute tick feed exists, confirmation can come ~45-90 s earlier than
    the minute-print rule — with the tradeoff made explicit via
    confirmation_mode."""
    if level is None or spot is None:
        return {"level": level, "crossed": False, "consecutive_prints": 0, "confirmed": False,
                "cross_age_obs": None, "cross_timestamp": None, "confirmation_timestamp": None,
                "fresh": False}
    # V13 fix (breakout hallucination): mutually-exclusive boundary. The old
    # `spot >= level` (bull) / `spot <= level` (bear) both evaluated TRUE when a
    # print sat EXACTLY at the level (e.g. 24207.75 for 11 straight obs), flagging
    # bullish AND bearish simultaneously and tripping the "conflicting evidence"
    # hard block on a flat tape. Making the bull side STRICT (>) and the bear side
    # inclusive (<=) makes the two conditions disjoint at the boundary: a print at
    # the level can never be both a bullish and a bearish cross, while entry timing
    # for genuine breaks is preserved.
    crossed = spot > level if above else spot <= level
    obs = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None:
            obs.append((t, v, "minute"))
    for t_row in (ticks or []):
        t = parse_nse_timestamp(t_row.get("capture_timestamp")); v = _f(t_row.get("spot"))
        if t and v is not None:
            obs.append((t, v, "tick"))
    obs.sort(key=lambda x: x[0])
    # AUDIT-B7: minute prints (chain underlyingValue) and ticks (allIndices)
    # are different spot sources that can disagree by +/-2 pts. A merged run
    # lets the lower-printing source reset the run built by the other when
    # price hugs a level. Runs are now computed PER SOURCE; confirmation
    # comes from whichever source holds a valid run.
    def run_for(kind_filter):
        run = []
        last_ok_t = None
        for t, v, kind in obs:
            if kind != kind_filter:
                continue
            ok = v > level if above else v <= level
            if ok:
                if last_ok_t is not None and (t - last_ok_t).total_seconds() > 300:
                    run = []  # V2.5-3: a data hole breaks the consecutive run
                run.append((t, v, kind))
                last_ok_t = t
            else:
                run = []
                last_ok_t = None
        return run

    def run_qualifies(run):
        if len(run) < TRIGGER_OBSERVATIONS:
            return False
        span = (run[-1][0] - run[0][0]).total_seconds()
        # V2.6-5: TIME-SPREAD confirmation — the run must span >= 45 s.
        return span >= TICK_CONFIRM_MIN_SPACING_SECONDS

    run_min, run_tick = run_for("minute"), run_for("tick")
    confirmed = False
    confirmation_mode = None
    run = None
    if run_qualifies(run_min):
        confirmed, confirmation_mode, run = True, "minute_prints", run_min
    elif run_qualifies(run_tick):
        confirmed, confirmation_mode, run = True, "ticks", run_tick
    # AUDIT-C5: freshness is computed WITHIN the qualifying source series, not
    # against the merged interleaved list (which inflated age via the other
    # source's observations).
    cross_age_obs = None
    cross_timestamp = None
    confirmation_timestamp = None
    if run:
        # C6: cross_timestamp = FIRST observation of the current run (the
        # original cross); confirmation_timestamp = the observation that
        # completed the TRIGGER_OBSERVATIONS requirement.
        cross_timestamp = run[0][0]
        if len(run) >= TRIGGER_OBSERVATIONS:
            confirmation_timestamp = run[TRIGGER_OBSERVATIONS - 1][0]
        source_series = [o for o in obs if o[2] == run[0][2]]
        first_idx = None
        for i, (t, v, kind) in enumerate(source_series):
            if t == cross_timestamp:
                first_idx = i
                break
        if first_idx is not None:
            cross_age_obs = len(source_series) - first_idx
    return {"level": level, "crossed": bool(crossed), "consecutive_prints": len(run or []),
            "confirmed": confirmed, "confirmation_mode": confirmation_mode,
            "cross_age_obs": cross_age_obs,
            "cross_timestamp": iso_or_none(cross_timestamp) if cross_timestamp else None,
            "confirmation_timestamp": iso_or_none(confirmation_timestamp) if confirmation_timestamp else None,
            "fresh": bool(cross_age_obs is not None and cross_age_obs <= TRIGGER_OBSERVATIONS),
            "prints": [{"timestamp": iso_or_none(t), "spot": v} for t, v, _ in (run or [])[-TRIGGER_OBSERVATIONS:]]}


def _level_microstructure(usable, ceiling, floor, spot):
    """V2.2: touch/rejection statistics for the nearest levels. On 2026-08-14
    the tell was three failed pushes at 24380-24382 (15:03-15:08) with no
    follow-through. Count touches and rejections of the last 30 minutes and
    flag triple rejection — evidence of absorption, not a trade trigger."""
    rows = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None: rows.append((t, v))
    if not rows or spot is None:
        return {"available": False}
    cutoff = rows[-1][0] - dt.timedelta(minutes=30)
    recent = [(t, v) for t, v in rows if t >= cutoff]
    # AUDIT-F6: a REJECTION is a touch followed by displacement AWAY from
    # the level (ceiling: next bar drops > 1 pt; floor: next bar rises > 1 pt).
    # The old code returned touches twice, so "triple rejection" actually
    # meant "touched three times" — including clean break-throughs.
    def level_stats(level, sign):
        if level is None:
            return 0, 0
        touches_n = 0
        rej_n = 0
        for i in range(len(recent) - 1):
            _, v_i = recent[i]
            _, v_j = recent[i + 1]
            if abs(v_i - level) <= 2.0:
                touches_n += 1
                if sign * (v_j - v_i) < -1.0:
                    rej_n += 1
        return touches_n, rej_n
    c_t, c_r = level_stats(ceiling, +1)
    f_t, f_r = level_stats(floor, -1)
    return {"available": True,
            "ceiling_touches": c_t, "ceiling_rejections": c_r,
            "floor_touches": f_t, "floor_rejections": f_r,
            "triple_ceiling_rejection": c_r >= 3,
            "triple_floor_rejection": f_r >= 3}


def _premium_flow(chain, latest_sid_used, atm):
    """V2.2: observed premium response across the last intervals at the ATM
    strike. Estimates observed elasticity (delta proxy) from synchronized
    premium/spot changes — NO Greeks are invented. Flags when premium moves
    more than the intrinsic-consistent amount (premium bid / paying up)."""
    by_sid = {}
    for r in chain:
        sid = r.get("snapshot_id")
        if sid and _f(r.get("strike")) == atm:
            by_sid[sid] = r
    sids = sorted(by_sid, key=lambda s: s)
    records = []
    for s in sids:
        r = by_sid[s]
        records.append({"sid": s, "spot": _f(r.get("spot")), "ce_ltp": _f(r.get("ce_ltp")),
                        "pe_ltp": _f(r.get("pe_ltp")), "ce_iv": _f(r.get("ce_iv")),
                        "pe_iv": _f(r.get("pe_iv"))})
    if len(records) < 2:
        return {"available": False}
    tail = records[-6:]
    # AUDIT-M2: build ONE per-interval record and compute deltas only when all
    # required fields are present for BOTH endpoints — independent filtering
    # plus zip() misaligned premium deltas with spot deltas.
    intervals = []
    for i in range(1, len(tail)):
        a, b = tail[i-1], tail[i]
        if (a["spot"] is not None and b["spot"] is not None
                and a["ce_ltp"] is not None and b["ce_ltp"] is not None
                and a["pe_ltp"] is not None and b["pe_ltp"] is not None):
            intervals.append({
                "d_spot": round(b["spot"] - a["spot"], 2),
                "d_ce": round(b["ce_ltp"] - a["ce_ltp"], 2),
                "d_pe": round(b["pe_ltp"] - a["pe_ltp"], 2),
            })
    up = [x for x in intervals if x["d_spot"] > 0]
    dn = [x for x in intervals if x["d_spot"] < 0]
    def elasticity(interv, prem_key):
        moves = [abs(x["d_spot"]) for x in interv]
        prems = [x[prem_key] for x in interv]
        if not moves or not prems: return None
        return round(sum(prems) / sum(moves), 3)
    ce_up = elasticity(up, "d_ce")
    pe_dn = elasticity(dn, "d_pe")
    last = records[-1]; first = tail[0]
    ce_iv_d = (last["ce_iv"] - first["ce_iv"]) if last["ce_iv"] is not None and first["ce_iv"] is not None else None
    pe_iv_d = (last["pe_iv"] - first["pe_iv"]) if last["pe_iv"] is not None and first["pe_iv"] is not None else None
    return {"available": True,
            "atm_ce_obs_delta_up": ce_up, "atm_pe_obs_delta_down": pe_dn,
            "ce_iv_change_last5": round(ce_iv_d, 2) if ce_iv_d is not None else None,
            "pe_iv_change_last5": round(pe_iv_d, 2) if pe_iv_d is not None else None,
            "ce_premium_bid": bool(ce_up is not None and ce_up >= 0.75),
            "pe_premium_bid": bool(pe_dn is not None and pe_dn >= 0.75),
            "note": "Observed premium elasticity from synchronized snapshots; not a Greek model."}


def _black76(spot, strike, t_years, iv, cp="c", r=0.065):
    """Black-76 analytic Greeks for a European option on a forward-like underlying
    (index options). Returns delta, gamma, vega, theta (per unit premium, in index
    points). Uses the captured IV directly — this is a LIVE GREEK ENGINE rather than
    the backward-looking premium-elasticity proxy, so it responds to the current IV
    surface (incl. IV crush on a breakout) instead of extrapolating 5 trailing bars.
    cp='c' call, 'p' put. t_years in years. iv in decimal (0.15 = 15%)."""
    import math as _m
    if spot is None or strike is None or t_years is None or iv is None or t_years <= 0 or iv <= 0:
        return None
    d1 = (_m.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * _m.sqrt(t_years))
    d2 = d1 - iv * _m.sqrt(t_years)
    def N(x): return 0.5 * (1.0 + _m.erf(x / _m.sqrt(2.0)))
    def n(x): return _m.exp(-0.5 * x * x) / _m.sqrt(2.0 * _m.pi)
    if cp == "c":
        delta = N(d1)
    else:
        delta = N(d1) - 1.0
    gamma = n(d1) / (spot * iv * _m.sqrt(t_years))
    vega = spot * _m.sqrt(t_years) * n(d1)          # per 1.0 vol change (i.e. 100% vol)
    # theta per year for a call/put on no-dividend underlying
    if cp == "c":
        theta = -(spot * n(d1) * iv) / (2.0 * _m.sqrt(t_years)) - r * strike * _m.exp(-r * t_years) * N(d2)
    else:
        theta = -(spot * n(d1) * iv) / (2.0 * _m.sqrt(t_years)) + r * strike * _m.exp(-r * t_years) * (1.0 - N(d2))
    return {"delta": round(delta, 4), "gamma": round(gamma, 6),
            "vega": round(vega, 4), "theta": round(theta, 4)}


def _greeks_for_candidates(chain, latest_sid, spot, atm):
    """V16: attach live Black-76 Greeks to the current snapshot's candidate legs
    (ATM band) using each leg's captured IV and time-to-expiry. Exposes real delta/
    gamma/vega/theta so position sizing and stop bounds can be based on live delta
    exposure instead of the trailing spot-premium ratio. Returns a dict keyed by
    (strike, side) with Greeks + iv + t_years."""
    expiry = None
    for r in chain:
        if r.get("snapshot_id") == latest_sid and r.get("expiry"):
            expiry = r.get("expiry"); break
    exp_date = parse_expiry(expiry) if expiry else None
    cap_ts = None
    for r in chain:
        if r.get("snapshot_id") == latest_sid:
            cap_ts = parse_nse_timestamp(r.get("capture_timestamp")); break
    if not (exp_date and cap_ts):
        return {}
    t_years = max((exp_date - cap_ts.date()).days, 0) / 365.0
    out = {}
    for r in chain:
        if r.get("snapshot_id") != latest_sid:
            continue
        strike = _f(r.get("strike"))
        if strike is None or abs(strike - atm) > 150:
            continue
        for side in ("ce", "pe"):
            iv = _f(r.get(f"{side}_iv"))
            if iv is None or iv <= 0:
                continue
            g = _black76(spot, strike, t_years, iv / 100.0, cp=("c" if side == "ce" else "p"))
            if g:
                out[(int(strike), side)] = {**g, "iv": round(iv, 2), "t_years": round(t_years, 4),
                                            "strike": int(strike), "side": side}
    return out


def _order_flow_imbalance(chain, latest_sid, atm):
    """V16 (true aggressor classification — Lee-Ready at the option level).

    V14's naive proxy assumed rising spot == aggressive call-buying. That
    misreads passive limit-order absorption (market makers selling into a rally,
    or short-covering) as bullish momentum and validates false breakouts.

    V16 classifies each option's OWN prints against ITS OWN prevailing order book,
    per the Lee-Ready algorithm:
        print (LTP) >= ask  -> trade lifted the ask   = aggressive BUYER
        print (LTP) <= bid  -> trade hit the bid      = aggressive SELLER
    A net delta is accumulated per option strike (buy volume - sell volume) and
    normalised over the ATM band to [-1, 1]. This uses genuine trade-level
    aggression (ask-lifts vs bid-hits) rather than a synthetic spot-direction
    assumption. Only strikes with a tradeable spread (bid < ask) are classifiable.

    Note: the collector stores interval VOLUME, not per-print tags, so the
    LTP-vs-quote sign is applied to the interval's traded volume (a Lee-Ready
    bulk classification, standard when tick prints are not available). Falls back
    to None (unknown) when quotes are too wide to be informative or there are not
    enough synchronized snapshots."""
    by_sid = {}
    for r in chain:
        sid = r.get("snapshot_id")
        if sid and _f(r.get("strike")) is not None:
            snap_atm = _f(r.get("current_atm")) or atm
            if abs((_f(r.get("strike")) or 0) - snap_atm) <= 150:
                by_sid.setdefault(sid, []).append(r)
    sids = sorted(by_sid)
    recs = []
    for sid in sids:
        q = by_sid[sid]
        # Lee-Ready bulk classification per strike within the band, mapped to
        # DIRECTIONAL pressure (a put ask-lift is put-buying = BEARISH, a put
        # bid-hit is put-selling = BULLISH):
        #   bull_vol = call ask-lifts (call buying) + put bid-hits (put selling)
        #   bear_vol = call bid-hits (call selling) + put ask-lifts (put buying)
        bull_vol = bear_vol = 0.0
        classified = 0
        for x in q:
            strike = _f(x.get("strike"))
            for side in ("ce", "pe"):
                ltp = _f(x.get(f"{side}_ltp")); bid = _f(x.get(f"{side}_bid")); ask = _f(x.get(f"{side}_ask"))
                vol = _f(x.get(f"{side}_interval_volume")) or 0.0
                if ltp is None or bid is None or ask is None or vol <= 0:
                    continue
                if not (bid < ask and bid > 0):   # wide/illiquid book not classifiable
                    continue
                if ltp >= ask:        # lifted the ask -> aggressive buyer of that leg
                    if side == "ce":
                        bull_vol += vol     # call buying = bullish
                    else:
                        bear_vol += vol     # put buying = bearish
                    classified += 1
                elif ltp <= bid:      # hit the bid -> aggressive seller of that leg
                    if side == "ce":
                        bear_vol += vol     # call selling = bearish
                    else:
                        bull_vol += vol     # put selling = bullish
                    classified += 1
                # ltp strictly inside (bid, ask) -> not classifiable (inside spread)
        recs.append({"sid": sid, "buy_vol": bull_vol, "sell_vol": bear_vol, "classified": classified})
    if len(recs) < 2:
        return {"available": False}
    tail = recs[-6:]
    total_buy = sum(x["buy_vol"] for x in tail)
    total_sell = sum(x["sell_vol"] for x in tail)
    total = total_buy + total_sell
    if total <= 0:
        return {"available": False}
    imbalance = round((total_buy - total_sell) / total, 3)
    # bull = net buy (positive), bear = net sell (negative)
    return {"available": True,
            "imbalance": imbalance,
            "buy_vol": round(total_buy, 0), "sell_vol": round(total_sell, 0),
            "classified_strikes": max((x["classified"] for x in tail)),
            "method": "Lee-Ready bulk (LTP vs own bid/ask)",
            "note": "true aggressor: ask-lifts (+) minus bid-hits (-), / total traded vol"}


def _coil_asymmetry(usable, spot):
    """V2.2: where price sits inside the recent range vs how flow is leaning.
    The 2026-08-14 15:10 error was labelling a top-of-range coil with put-side
    flow as 'symmetric compression'. Position + cumulative flow lean now
    produce an explicit resolution-lean advisory (not a trade trigger)."""
    rows = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None: rows.append((t, v))
    if len(rows) < 10 or spot is None:
        return {"available": False}
    cutoff = rows[-1][0] - dt.timedelta(minutes=60)
    win = [v for t, v in rows if t >= cutoff]
    if not win: win = [v for _, v in rows[-60:]]
    hi, lo = max(win), min(win)
    if hi - lo <= 0:
        return {"available": False}
    pos = (spot - lo) / (hi - lo)
    pos_zone = "top" if pos >= 0.62 else "bottom" if pos <= 0.38 else "middle"
    # session PCR drift: summary rows carry range_oi_pcr
    pcr_vals = [_f(r.get("range_oi_pcr")) for r in usable if _f(r.get("range_oi_pcr")) is not None]
    pcr_drift = None
    if len(pcr_vals) >= 5:
        pcr_drift = round(pcr_vals[-1] - pcr_vals[max(0, len(pcr_vals) - 6)], 3)
    flow_lean = None
    if pcr_drift is not None:
        flow_lean = "put_side" if pcr_drift > 0.02 else "call_side" if pcr_drift < -0.02 else "neutral"
    lean = "neutral"
    if pos_zone == "top" and flow_lean == "put_side": lean = "downside_resolution"
    elif pos_zone == "bottom" and flow_lean == "call_side": lean = "upside_resolution"
    return {"available": True, "coil_position": round(pos, 2), "coil_zone": pos_zone,
            "pcr_drift_last5": pcr_drift, "flow_lean": flow_lean, "resolution_lean": lean}


def _options_behaviour(chain, latest_sid_used, spot, atm, usable):
    """V2.4: options behavioural layer, research-backed patterns with honest
    evidence grades. None of these is a trade trigger; they adjust evidence
    weight only (prompt hierarchy: Level 3-4).

    Sources of the patterns:
      - PCR contrarian-at-extremes: practitioner consensus (0.7-1.2 noise,
        >1.3 put-crowding, <0.5-0.7 call-crowding). NOT peer-reviewed.
      - Max pain pinning: mixed academic record; modest pull on liquid index
        expiries, strongest final days into expiry, breaks in news regimes.
        NIFTY-specific pinning near expiry day = practitioner observation.
      - IV skew: peer-reviewed for NIFTY (Shaikh & Padhi, JIBR 2014; JAMR
        2016): persistent put skew (put IV > call IV), smile stronger in
        near-month. Low-skew days -> higher subsequent returns (2016 study,
        Xing-Zhang-Zhao methodology).
    """
    out = {"available": False}
    if spot is None or atm is None:
        return out
    q = [r for r in chain if r.get("snapshot_id") == latest_sid_used]
    if not q:
        return out
    strikes = sorted({int(_f(r["strike"])) for r in q if _f(r.get("strike")) is not None})
    if not strikes:
        return out

    # ---- max pain: strike minimising writer payout = sum of intrinsic x OI ----
    def pain(s):
        total = 0.0
        for r in q:
            st = int(_f(r["strike"]))
            ce_oi = _f(r.get("ce_oi")) or 0.0
            pe_oi = _f(r.get("pe_oi")) or 0.0
            total += max(s - st, 0.0) * ce_oi + max(st - s, 0.0) * pe_oi
        return total
    max_pain = min(strikes, key=pain)
    mp_dist = round(spot - max_pain, 1)

    # max pain stability across the session (shifting pain = contested expiry)
    # M4-FIX: a true trailing 30-minute window (was: last 6 snapshots, which
    # is ~6 minutes at 1-min cadence but 90 minutes at 15-min cadence).
    sid_ts: Dict[str, dt.datetime] = {}
    for r in chain:
        sid = r.get("snapshot_id")
        t = parse_nse_timestamp(r.get("source_timestamp"))
        if not sid or sid in sid_ts or t is None:
            continue
        # Only snapshots where the ATM leg actually has a market (bid>0 and
        # ask>0) AND its ATM CE/PE IVs have repriced, qualify for
        # IV/skew/max-pain history. The pre-open capture has zero quotes and
        # stale placeholder IVs (e.g. ~45% ATM, CE IV 26 vs PE IV 12), which
        # would otherwise poison skew_change_30m / max-pain history.
        if _f(r.get("strike")) == atm:
            bid = _f(r.get("ce_bid")); ask = _f(r.get("ce_ask"))
            ce_iv = _f(r.get("ce_iv")); pe_iv = _f(r.get("pe_iv"))
            iv_repriced = (ce_iv is not None and pe_iv is not None
                           and ce_iv > 0 and pe_iv > 0
                           and abs(ce_iv - pe_iv) <= MAX_ATM_IV_SPREAD)
            if (bid or 0) > 0 and (ask or 0) > 0 and iv_repriced:
                sid_ts[sid] = t
    sids = sorted(sid_ts, key=lambda s: sid_ts[s])
    sids_30m = sids
    if sids:
        latest_ts = sid_ts[sids[-1]]
        sids_30m = [s for s in sids if (latest_ts - sid_ts[s]).total_seconds() <= 1800.0] or sids[-6:]
    pain_hist = []
    for sid in sids_30m:
        qs = [r for r in chain if r.get("snapshot_id") == sid and _f(r.get("strike")) is not None]
        if qs:
            pain_hist.append(min({int(_f(r["strike"])) for r in qs}, key=lambda s: sum(
                max(s - int(_f(r["strike"])), 0.0) * (_f(r.get("ce_oi")) or 0.0) +
                max(int(_f(r["strike"])) - s, 0.0) * (_f(r.get("pe_oi")) or 0.0)
                for r in qs)))
    pain_shift_30m = round(pain_hist[-1] - pain_hist[0], 0) if len(pain_hist) >= 2 else None

    # ---- IV skew: OTM put IV - OTM call IV at equidistant strikes ----
    def iv_at(strike, side):
        best = None
        for r in q:
            if int(_f(r["strike"])) == strike:
                v = _f(r.get(f"{side}_iv"))
                if v and v > 0:
                    return v
        return best

    def skew_for_sid(sid, spot_s, atm_s):
        qs = [r for r in chain if r.get("snapshot_id") == sid and _f(r.get("strike")) is not None]
        if not qs or spot_s is None or atm_s is None:
            return None
        put_iv = call_iv = None
        for d in (100, 150, 200):
            if put_iv is None:
                put_iv = next((_f(r.get("pe_iv")) for r in qs if int(_f(r["strike"])) == atm_s - d and _f(r.get("pe_iv")) and _f(r["pe_iv"]) > 0), None)
            if call_iv is None:
                call_iv = next((_f(r.get("ce_iv")) for r in qs if int(_f(r["strike"])) == atm_s + d and _f(r.get("ce_iv")) and _f(r["ce_iv"]) > 0), None)
        if put_iv is not None and call_iv is not None:
            return round(put_iv - call_iv, 2)
        return None

    skew = skew_for_sid(latest_sid_used, spot, atm)
    skew_hist = []
    for sid in sids_30m:
        qs = [r for r in chain if r.get("snapshot_id") == sid and _f(r.get("spot")) is not None]
        if qs:
            s_spot = _f(qs[0].get("spot"))
            s_atm = round_to_strike(s_spot) if s_spot is not None else None
            sk = skew_for_sid(sid, s_spot, s_atm)
            if sk is not None:
                skew_hist.append(sk)
    skew_change_30m = round(skew_hist[-1] - skew_hist[0], 2) if len(skew_hist) >= 2 else None
    skew_reversal = bool(skew_change_30m is not None and skew_hist[0] >= 2.0 and skew_hist[-1] <= 0.8)
    skew_inverted = bool(skew is not None and skew < -0.5)  # calls richer than puts: crowded-long regime

    # ---- PCR regime: z-score of OI PCR vs the session distribution ----
    pcr_vals = [_f(r.get("range_oi_pcr")) for r in usable if _f(r.get("range_oi_pcr")) is not None]
    pcr_z = None
    if len(pcr_vals) >= 10:
        mu = statistics.fmean(pcr_vals); sd = statistics.pstdev(pcr_vals)
        if sd > 1e-9:
            pcr_z = round((pcr_vals[-1] - mu) / sd, 2)
    pcr_regime = None
    if pcr_z is not None:
        if pcr_z >= 1.5: pcr_regime = "put_crowding_contrarian_bullish"
        elif pcr_z <= -1.5: pcr_regime = "call_crowding_contrarian_bearish"
        else: pcr_regime = "neutral"

    # ---- volume concentration: ATM +/- 2 strikes share of cumulative volume ----
    total_vol = 0.0; near_vol = 0.0
    for r in q:
        st = int(_f(r["strike"]))
        v = (_f(r.get("ce_volume_cumulative")) or 0.0) + (_f(r.get("pe_volume_cumulative")) or 0.0)
        total_vol += v
        if abs(st - atm) <= 100:
            near_vol += v
    vol_conc = round(near_vol / total_vol, 3) if total_vol > 0 else None

    # ---- expiry proximity (TRADING days; weekend gap would distort) ----
    expiry_date = parse_expiry((q[0].get("expiry") or ""))
    cap_dt = parse_nse_timestamp(q[0].get("capture_timestamp"))
    days_to_expiry = None
    if expiry_date and cap_dt:
        if cap_dt.date() == expiry_date:
            days_to_expiry = 0
        else:
            d = cap_dt.date() + dt.timedelta(days=1)
            days_to_expiry = 0
            while d <= expiry_date:
                if d.weekday() < 5:
                    days_to_expiry += 1
                d += dt.timedelta(days=1)

    out = {
        "available": True,
        "windowed": True,  # AUDIT-H6: computed from the captured strike window, not the full chain
        "windowed_max_pain": max_pain,
        "windowed_max_pain_distance": mp_dist,
        "windowed_max_pain_shift_30m": pain_shift_30m,
        "max_pain": max_pain,
        "max_pain_distance": mp_dist,
        "max_pain_shift_30m": pain_shift_30m,
        "pinning_zone": bool(abs(mp_dist) <= 30.0 and days_to_expiry is not None and days_to_expiry <= 2),
        "skew_otm_put_minus_call": skew,
        "skew_change_30m": skew_change_30m,
        "skew_reversal": skew_reversal,
        "skew_inverted": skew_inverted,
        "pcr_z": pcr_z,
        "pcr_regime": pcr_regime,
        "volume_concentration_atm2": vol_conc,
        "windowed_volume_concentration_atm2": vol_conc,
        "windowed_pcr_z": pcr_z,
        "window_note": "max pain, PCR and volume concentration are computed from the captured strike window (ATM +/- ~1000), not the full NIFTY chain",
        "days_to_expiry": days_to_expiry,
        "expiry_week": bool(days_to_expiry is not None and days_to_expiry <= 3),
        "evidence_grades": {
            "pcr_extremes": "practitioner consensus, not peer-reviewed",
            "max_pain": "mixed academic record; modest expiry-week pull",
            "skew": "peer-reviewed for NIFTY (Shaikh & Padhi 2014, 2016)",
        },
        "non_predictive_note": "Behavioural patterns weight evidence; they never trigger a trade.",
    }
    return out


def _read_sector_rows(output_dir: Path, date_label: str) -> List[Dict[str, Any]]:
    path = output_dir / f"nifty-sector-context-v2-{date_label}.csv"
    if not path.exists():
        return []
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def _anticipation(output_dir, date_label, chain, latest_sid_used, spot, atm, usable,
                  minutes_to_close=None):
    """ANTICIPATION LAYER — options-native leading indicators, context only.

    These are the only options-chain signals that can lean BEFORE a move
    (as opposed to OI/price, which are coincident/lagging):

      1. implied_forward divergence  - put-call parity F = K + (C_mid - P_mid)
         across ATM +/- 1..2 strikes. The options market's own forecast of
         spot. Divergence vs spot = the market's "pull" direction.
      2. straddle expansion while the spot range compresses ("iv_bid") -
         options paying up for a move BEFORE spot breaks.
      3. skew velocity + z-score     - hedging demand shifting (peer-reviewed
         NIFTY put-skew literature).
      4. interval volume PCR         - put volume / call volume (leads OI PCR,
         which is dominated by structural put hedging).
      5. gamma regime proxy          - ATM+/-2 OI share of ATM+/-5: high gamma
         => market-makers CONTAIN moves; low gamma => AMPLIFY moves.

    NONE of these is a trade trigger. They set the "armed" escalation level,
    add advisories, and are passed to the LLM as context. The breakout engine
    still decides the entry.
    """
    out = {"available": False}
    if spot is None or atm is None:
        return out
    q = [r for r in chain if r.get("snapshot_id") == latest_sid_used]
    if not q:
        return out
    by_strike = {int(_f(r["strike"])): r for r in q if _f(r.get("strike")) is not None}
    if not by_strike:
        return out

    # ---- 1. implied forward (put-call parity) ----
    # Reliability gate: parity only holds on tight, executable quotes. Stale or
    # wide closing quotes (e.g. the +76 pt divergence at 15:28) break the
    # C-P relationship and must not be reported as an anticipation signal.
    atm_row = by_strike.get(int(atm))
    fwd_reliable = False
    if atm_row:
        c_bid = _f(atm_row.get("ce_bid")); c_ask = _f(atm_row.get("ce_ask"))
        p_bid = _f(atm_row.get("pe_bid")); p_ask = _f(atm_row.get("pe_ask"))
        if all(v is not None and v > 0 for v in (c_bid, c_ask, p_bid, p_ask)):
            c_mid = (c_bid + c_ask) / 2.0; p_mid = (p_bid + p_ask) / 2.0
            c_sp = (c_ask - c_bid) / c_mid; p_sp = (p_ask - p_bid) / p_mid
            fwd_reliable = c_sp <= MAX_OPTION_SPREAD_PCT and p_sp <= MAX_OPTION_SPREAD_PCT
    forwards = []
    for st in (atm - 100, atm - 50, atm, atm + 50, atm + 100):
        r = by_strike.get(st)
        if not r:
            continue
        c = _f(r.get("ce_mid")); p = _f(r.get("pe_mid"))
        if c and p and c > 0 and p > 0:
            forwards.append(st + (c - p))
    fwd = fwd_spread = fwd_divergence = fwd_lean = None
    near_close_noise = False
    extreme_parity_dislocation = False
    if fwd_reliable and len(forwards) >= 3:
        fwd = round(statistics.fmean(forwards), 2)
        fwd_spread = round(max(forwards) - min(forwards), 2)
        fwd_divergence = round(fwd - spot, 2)
        fwd_lean = "up" if fwd_divergence > 0 else "down" if fwd_divergence < 0 else "flat"
        if abs(fwd_divergence) > 50.0:
            # A >50 pt parity violation on tight quotes near the CLOSE is the
            # chain spot showing the closing-auction print while options still
            # quote pre-auction - a synchronisation artefact, not a forecast.
            # DURING OPEN HOURS the same size violation is a REAL dislocation:
            # an extreme inverted skew / crowded-long regime. Distinguish by
            # minutes_to_close and surface the latter instead of discarding it.
            if minutes_to_close is not None and minutes_to_close < 5.0:
                near_close_noise = True
                fwd_reliable = False
                fwd = fwd_spread = fwd_divergence = fwd_lean = None
            else:
                extreme_parity_dislocation = True

    # ---- group chain by snapshot for series features ----
    sid_rows = {}
    for r in chain:
        sid = r.get("snapshot_id")
        if sid and _f(r.get("strike")) is not None:
            sid_rows.setdefault(sid, []).append(r)
    sids = sorted(sid_rows)
    straddle_series = []
    skew_series = []
    vol_pcr_series = []
    for sid in sids:
        rows = sid_rows[sid]
        s_spot = _f(rows[0].get("spot")); s_atm = _f(rows[0].get("current_atm"))
        if s_spot is None or s_atm is None:
            continue
        by_st = {int(_f(r["strike"])): r for r in rows if _f(r.get("strike")) is not None}
        atm_r = by_st.get(int(s_atm))
        if not atm_r:
            continue
        c = _f(atm_r.get("ce_mid")); p = _f(atm_r.get("pe_mid"))
        if c and p and c > 0 and p > 0:
            straddle_series.append((sid, c + p))
        # IV must have repriced for the skew history to be meaningful
        ce_iv = _f(atm_r.get("ce_iv")); pe_iv = _f(atm_r.get("pe_iv"))
        iv_reprice = (ce_iv and pe_iv and ce_iv > 0 and pe_iv > 0
                      and abs(ce_iv - pe_iv) <= MAX_ATM_IV_SPREAD)
        if iv_reprice:
            put_iv = call_iv = None
            for d in (100, 150, 200):
                if put_iv is None:
                    pr = by_st.get(int(s_atm) - d)
                    if pr and _f(pr.get("pe_iv")) and _f(pr.get("pe_iv")) > 0:
                        put_iv = _f(pr.get("pe_iv"))
                if call_iv is None:
                    cr = by_st.get(int(s_atm) + d)
                    if cr and _f(cr.get("ce_iv")) and _f(cr.get("ce_iv")) > 0:
                        call_iv = _f(cr.get("ce_iv"))
            if put_iv and call_iv:
                skew_series.append((sid, round(put_iv - call_iv, 2)))
        ce_v = sum(_f(r.get("ce_interval_volume")) or 0.0 for r in rows)
        pe_v = sum(_f(r.get("pe_interval_volume")) or 0.0 for r in rows)
        if ce_v > 0:
            vol_pcr_series.append((sid, pe_v / ce_v))

    # ---- 2. straddle expansion vs spot-range compression ----
    straddle_change = None
    if len(straddle_series) >= 2:
        idx = max(0, len(straddle_series) - 6)
        straddle_change = round(straddle_series[-1][1] - straddle_series[idx][1], 2)
    spot_series = []
    for r in usable:
        t = parse_nse_timestamp(r.get("source_timestamp")); v = _f(r.get("spot"))
        if t and v is not None:
            spot_series.append((t, v))
    spot_series.sort()
    spots = [v for _, v in spot_series]
    rng_now = rng_prev = None
    if len(spots) >= 10:
        rng_now = max(spots[-5:]) - min(spots[-5:])
        rng_prev = max(spots[-10:-5]) - min(spots[-10:-5])
    compression = bool(rng_now is not None and rng_prev and rng_prev > 0 and rng_now < rng_prev)
    iv_bid = bool(straddle_change is not None and straddle_change > 0 and compression)

    # ---- 3. skew velocity + z-score ----
    skew_velocity = skew_z = None
    if len(skew_series) >= 3:
        recent = [v for _, v in skew_series[-3:]]
        skew_velocity = round(recent[-1] - recent[0], 2)
    skew_vals = [v for _, v in skew_series]
    if len(skew_vals) >= 10:
        mu = statistics.fmean(skew_vals); sd = statistics.pstdev(skew_vals)
        if sd > 1e-9:
            skew_z = round((skew_vals[-1] - mu) / sd, 2)

    # ---- 4. interval volume PCR (latest) ----
    vol_pcr = round(vol_pcr_series[-1][1], 3) if vol_pcr_series else None

    # ---- 5. gamma regime proxy ----
    oi_near = oi_wide = 0.0
    for st, r in by_strike.items():
        tot = (_f(r.get("ce_oi")) or 0.0) + (_f(r.get("pe_oi")) or 0.0)
        if abs(st - atm) <= 500:
            oi_wide += tot
            if abs(st - atm) <= 100:
                oi_near += tot
    gamma_proxy = round(oi_near / oi_wide, 3) if oi_wide > 0 else None
    gamma_regime = None
    if gamma_proxy is not None:
        if gamma_proxy >= 0.5:
            gamma_regime = "high_gamma_containment"
        elif gamma_proxy <= 0.35:
            gamma_regime = "low_gamma_amplification"
        else:
            gamma_regime = "moderate_gamma"

    # ---- momentum vs implied-forward disagreement ----
    momentum_sign = None
    if len(spots) >= 5:
        net5 = spots[-1] - spots[-5]
        momentum_sign = "up" if net5 > 2 else "down" if net5 < -2 else "flat"
    divergence_note = None
    if (fwd_lean in ("up", "down") and momentum_sign in ("up", "down")
            and fwd_lean != momentum_sign):
        divergence_note = (
            f"options-market disagreement: implied forward leans {fwd_lean} "
            f"(divergence {fwd_divergence:+.1f} pts) while 5-obs momentum is {momentum_sign}"
        )

    # ---- sectoral breadth / sector-lead (from allIndices, context only) ----
    sector_fields: Dict[str, Any] = {
        "sector_available": False,
        "bank_nifty_pchange": None, "nifty_it_pchange": None,
        "sector_breadth_up": None, "sector_breadth_down": None,
        "sector_breadth_net": None,
        "sector_lead_name": None, "sector_lead_pchange": None,
        "sector_note": None,
    }
    sector_conflict = False
    sector_rows = _read_sector_rows(output_dir, date_label)
    if sector_rows:
        by_sid = {r.get("snapshot_id"): r for r in sector_rows if r.get("snapshot_id")}
        srow = by_sid.get(latest_sid_used) or sector_rows[-1]
        bank_pc = _f(srow.get("bank_pchange"))
        it_pc = _f(srow.get("it_pchange"))
        nifty_pc = _f(srow.get("nifty_pchange"))
        pchanges = [(k, _f(srow.get(f"{k}_pchange"))) for _n, k in SECTOR_INDICES]
        pchanges = [(k, v) for k, v in pchanges if v is not None]
        if pchanges:
            up = sum(1 for _, v in pchanges if v > 0.05)
            down = sum(1 for _, v in pchanges if v < -0.05)
            net = up - down
            lead_key, lead_pc = max(pchanges, key=lambda kv: abs(kv[1]))
            sector_fields["sector_available"] = True
            sector_fields["bank_nifty_pchange"] = bank_pc
            sector_fields["nifty_it_pchange"] = it_pc
            sector_fields["sector_breadth_up"] = up
            sector_fields["sector_breadth_down"] = down
            sector_fields["sector_breadth_net"] = net
            sector_fields["sector_lead_name"] = lead_key
            sector_fields["sector_lead_pchange"] = lead_pc
            note_parts = []
            if bank_pc is not None and it_pc is not None:
                nifty_txt = f"{nifty_pc:+.2f}%" if nifty_pc is not None else "n/a"
                note_parts.append(f"Bank {bank_pc:+.2f}% / IT {it_pc:+.2f}% vs NIFTY {nifty_txt}")
            note_parts.append(f"breadth {up}U/{down}D")
            if momentum_sign == "down" and net > 0:
                note_parts.append("broad market NOT confirming the down move")
                sector_conflict = True
            elif momentum_sign == "down" and net < 0:
                note_parts.append("broad market confirming the down move")
            elif momentum_sign == "up" and net < 0:
                note_parts.append("broad market NOT confirming the up move")
                sector_conflict = True
            elif momentum_sign == "up" and net > 0:
                note_parts.append("broad market confirming the up move")
            sector_fields["sector_note"] = "; ".join(note_parts)

    armed = bool(
        (fwd_divergence is not None and abs(fwd_divergence) >= 25.0)
        or iv_bid
        or (skew_velocity is not None and abs(skew_velocity) >= 1.5)
        or (skew_z is not None and abs(skew_z) >= 1.5)
        or extreme_parity_dislocation
        or sector_conflict
    )

    out = {
        "available": True,
        "implied_forward": fwd,
        "implied_forward_reliable": fwd_reliable,
        "near_close_noise": near_close_noise,
        "extreme_parity_dislocation": extreme_parity_dislocation,
        "implied_forward_divergence": fwd_divergence,
        "implied_forward_cross_strike_spread": fwd_spread,
        "implied_forward_lean": fwd_lean,
        "straddle_change_5obs": straddle_change,
        "spot_range_compression": compression,
        "iv_bid": iv_bid,
        "skew_velocity_3obs": skew_velocity,
        "skew_z": skew_z,
        "interval_volume_pcr": vol_pcr,
        "gamma_proxy_atm2_of_atm5": gamma_proxy,
        "gamma_regime": gamma_regime,
        "momentum_disagreement": divergence_note,
        **sector_fields,
        "armed": armed,
        "note": "Anticipatory context only: may lean before a move but is never a trade trigger. The breakout engine decides the entry.",
    }
    return out


def _range_strangle_candidate(candidates, atm, spot):
    """V12: build a RANGE-day SHORT STRANGLE (defined-risk iron condor) from the
    captured chain. The from-scratch 4-day test proved SELLING OTM premium is the
    profitable edge on range/choppy days (where buying options bleeds theta):
      08-21 +190rs, 08-26 +350rs on real chain premiums. Sells the OTM put and
    OTM call RANGE_STRANGLE_DIST strikes from ATM, buys protective wings
    RANGE_STRANGLE_WING strikes further out (defined risk). Returns the strikes
    and per-leg quotes, or None if quotes are unavailable/illiquid."""
    if not candidates or atm is None or spot is None:
        return None
    put_short = int(atm) - STRIKE_GAP * RANGE_STRANGLE_DIST
    put_long = put_short - STRIKE_GAP * RANGE_STRANGLE_WING
    call_short = int(atm) + STRIKE_GAP * RANGE_STRANGLE_DIST
    call_long = call_short + STRIKE_GAP * RANGE_STRANGLE_WING
    def q(strike, side):
        r = candidates.get(strike)
        if not r:
            return None
        return _quote(r, side)
    ps = q(put_short, "pe"); pl = q(put_long, "pe")
    cs = q(call_short, "ce"); cl = q(call_long, "ce")
    # need sellable short legs (bid>0) and buyable protective wings (ask>0)
    if not (ps and ps["executable"] and cs and cs["executable"]):
        return None
    if not (pl and pl["bid"] and cl and cl["bid"]):
        return None
    put_credit = (ps["bid"] or 0) - (pl["ask"] or pl["bid"] or 0)
    call_credit = (cs["bid"] or 0) - (cl["ask"] or cl["bid"] or 0)
    total_credit = put_credit + call_credit
    if total_credit <= 0:
        return None
    return {
        "kind": "SHORT_STRANGLE",
        "put_short": put_short, "put_long": put_long,
        "call_short": call_short, "call_long": call_long,
        "put_credit": round(put_credit, 2), "call_credit": round(call_credit, 2),
        "total_credit": round(total_credit, 2),
        "t1_target": round(total_credit * (1 - RANGE_STRANGLE_T1), 2),  # buy back at this debit
        "stop_debit": round(total_credit * RANGE_STRANGLE_STOP, 2),     # max loss to close
        "entry_spot": round(spot, 2),
    }


def _mode_b_candidate(spot, atm, levels, quote, crossed_side, gap_ok, quotes_ok,
                     day_high=None, day_low=None, fresh_high=None, fresh_low=None):
    """V2.2: MODE-B capture profile (separate from MODE-A).
    Target ~20 pts, stop 5-10 pts (structural), min R:R 2, earlier trigger.
    Still blocked by chain freshness and non-executable quotes. The target
    must stay inside the session range (day high/low +/- 5 pts buffer) UNLESS
    the cross is at a FRESH 30-observation extreme (range extension)."""
    out = {"enabled": MODE_B_ENABLED, "decision": "WAIT / NO TRADE", "direction": None}
    blocking = []
    if not MODE_B_ENABLED:
        return out
    if not gap_ok:
        blocking.append("chain gap exceeds gate; premiums unverifiable")
    if not quotes_ok or quote is None:
        blocking.append("candidate quotes not executable")
    if spot is None or atm is None:
        blocking.append("no synchronized spot/ATM")
    if blocking:
        out["blocking"] = blocking
        return out
    floor = levels.get("nearest_confirmed_floor"); ceil = levels.get("nearest_confirmed_ceiling")
    direction = crossed_side  # "CALL" or "PUT" or None (initial break qualifies for MODE-B)
    if direction is None:
        out["blocking"] = ["no initial break beyond a near level"]
        return out
    prov_ceil_mb = levels.get("provisional_ceiling_10")
    prov_floor_mb = levels.get("provisional_floor_10")
    if direction == "PUT":
        # AUDIT-F2: provisional rolling ceiling when no confirmed pivot exists
        stop = ceil if ceil is not None else prov_ceil_mb
        stop_source = "confirmed" if ceil is not None else "provisional"
        if stop is None or not (MODE_B_MIN_RISK <= stop - spot <= MODE_B_MAX_RISK):
            out["blocking"] = [f"no structural stop within {MODE_B_MIN_RISK}-{MODE_B_MAX_RISK} pts (nearest ceiling {stop})"]
            return out
        t1, t2 = spot - MODE_B_T1, spot - MODE_B_TARGET
        risk = stop - spot
        # AUDIT-S6: exempt fresh 30-obs lows — the range is being EXTENDED,
        # so requiring T2 inside the old range made MODE-B de facto PUT-only.
        if day_low is not None and t2 < day_low - 5.0 and fresh_low is not True:
            out["blocking"] = [f"target_2 {round(t2, 2)} extends beyond day low {day_low} (not a fresh range extension)"]
            return out
        rr = MODE_B_TARGET / risk
        side, strike = "pe", int(atm)
    else:
        stop = floor if floor is not None else prov_floor_mb  # structural stop BELOW entry
        stop_source = "confirmed" if floor is not None else "provisional"
        if stop is None or not (MODE_B_MIN_RISK <= spot - stop <= MODE_B_MAX_RISK):
            out["blocking"] = [f"no structural stop within {MODE_B_MIN_RISK}-{MODE_B_MAX_RISK} pts (nearest floor {stop})"]
            return out
        t1, t2 = spot + MODE_B_T1, spot + MODE_B_TARGET
        risk = spot - stop
        # AUDIT-S6: same exemption for fresh 30-obs highs.
        if day_high is not None and t2 > day_high + 5.0 and fresh_high is not True:
            out["blocking"] = [f"target_2 {round(t2, 2)} extends beyond day high {day_high} (not a fresh range extension)"]
            return out
        rr = MODE_B_TARGET / risk
        side, strike = "ce", int(atm)
    # NOTE: with risk capped at MODE_B_MAX_RISK=10, rr = 20/risk >= 2.0 always,
    # so this check is currently vacuous; retained as a guard if caps change.
    if rr < MODE_B_MIN_RR:
        out["blocking"] = [f"R:R {round(rr, 2)} below {MODE_B_MIN_RR}"]
        return out
    out["decision"] = "BUY PUT NOW" if direction == "PUT" else "BUY CALL NOW"
    out["direction"] = direction
    out["strike"] = strike
    out["side"] = side
    out["entry_spot"] = round(spot, 2)
    out["entry_ask"] = quote.get("ask")
    out["exit_bid"] = quote.get("bid")
    out["stop"] = stop
    out["stop_source"] = stop_source
    out["target_1"] = round(t1, 2)
    out["target_2"] = round(t2, 2)
    out["risk_points"] = round(risk, 2)
    out["reward_risk"] = round(rr, 2)
    out["quote"] = quote
    out["profile_note"] = "MODE-B capture profile: ~20-pt target, 5-10 pt structural stop, higher frequency, higher whipsaw risk. Separate budget from MODE-A."
    return out




# =============================================================================
# PAPER TRADING (simulated P&L ledger)
# =============================================================================

PAPER_TRADE_COLUMNS = [
    "trade_id", "date", "mode", "direction", "expiry", "strike", "side",
    "entry_time", "entry_underlying", "entry_ask", "lot_size",
    "stop_initial", "stop_current", "target_1", "target_2",
    "risk_points", "reward_risk",
    "exit_time", "exit_reason", "exit_underlying", "exit_bid",
    "t1_reached", "trailed_to_entry",
    "pnl_points", "pnl_rupees", "return_pct", "status", "notes",
]

PAPER_SUMMARY_COLUMNS = [
    "date", "trades", "open_trades", "wins", "losses", "win_rate_pct",
    "total_pnl_points", "total_pnl_rupees", "avg_win_rupees", "avg_loss_rupees",
    "profit_factor", "best_rupees", "worst_rupees", "generated_at",
]


def _paper_state_path(output_dir: Path, date_label: str) -> Path:
    return output_dir / f"paper_state-{date_label}.json"


def _paper_trades_path(output_dir: Path, date_label: str) -> Path:
    return output_dir / f"paper_trades-{date_label}.csv"


def _paper_summary_path(output_dir: Path, date_label: str) -> Path:
    return output_dir / f"paper_summary-{date_label}.csv"


def _paper_load_state(output_dir: Path, date_label: str) -> Dict[str, Any]:
    path = _paper_state_path(output_dir, date_label)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def _paper_save_state(output_dir: Path, date_label: str, state: Dict[str, Any]) -> None:
    try:
        atomic_json_write(_paper_state_path(output_dir, date_label), state)
    except Exception:
        logging.exception("paper state write failed")


def _paper_lot_size(output_dir: Path, date_label: str) -> int:
    """NIFTY lot size from the collected chain; fall back to the configured default."""
    for r in _read_csv(output_dir / f"nifty-option-chain-v2-{date_label}.csv"):
        lot = number(r.get("lot_size"), integer=True)
        if lot and lot > 0:
            return int(lot)
    return UPSTOX_DEFAULT_LOT_SIZE


def _paper_open_trade(output_dir: Path, date_label: str, packet: Dict[str, Any]) -> Optional[str]:
    """Open a simulated trade when the engine emits an executable BUY signal."""
    if not PAPER_TRADING_ENABLED or PAPER_SUSPENDED:
        return None
    decision = packet.get("decision") or ""
    mode = None
    strike = side = entry_ask = stop = target_1 = target_2 = risk_points = reward_risk = None
    if decision.startswith("BUY") and packet.get("decision_grade") == "execution_candidate":
        mode = "MODE-A"
        cand = packet.get("option_candidate") or {}
        geo = packet.get("risk_geometry") or {}
        strike = number(cand.get("strike"), integer=True)
        side = cand.get("side")
        entry_ask = number(cand.get("entry_ask"))
        stop = number(geo.get("stop"))
        target_2 = number(geo.get("target_2"))
        risk_points = number(geo.get("risk_points"))
        reward_risk = number(geo.get("reward_risk"))
    else:
        mb = packet.get("mode_b") or {}
        if (mb.get("decision") or "").startswith("BUY"):
            mode = "MODE-B"
            strike = number(mb.get("strike"), integer=True)
            side = mb.get("side")
            entry_ask = number(mb.get("entry_ask"))
            stop = number(mb.get("stop"))
            target_1 = number(mb.get("target_1"))
            target_2 = number(mb.get("target_2"))
            risk_points = number(mb.get("risk_points"))
            reward_risk = number(mb.get("reward_risk"))
    if not mode or strike is None or not side or entry_ask is None or stop is None:
        return None
    latest = packet.get("latest") or {}
    entry_underlying = number(latest.get("spot"))
    # V12: RANGE_PREMIUM short-strangle. Open as a defined-risk iron condor:
    # sell OTM put + call, buy protective wings. The paper trade records the
    # net credit collected; exit buys it back (debit) at T1 or the stop.
    if mode == "RANGE_PREMIUM":
        rs = packet.get("range_signal") or {}
        if not rs:
            return None
        entry_underlying = number(latest.get("spot"))
        if entry_underlying is None:
            return None
        expiry = latest.get("expiry")
        state = _paper_load_state(output_dir, date_label)
        seq = int(state.get("trade_seq") or 0) + 1
        trade = {
            "trade_id": f"{date_label.replace('-', '')}-{seq:03d}",
            "date": date_label, "mode": mode, "direction": "STRANGLE",
            "expiry": expiry, "strike": rs.get("put_short"), "side": "pe",
            "entry_time": iso_or_none(now_ist()),
            "entry_source_ts": latest.get("source_timestamp"),
            "entry_underlying": round(entry_underlying, 2),
            "entry_ask": round(rs.get("total_credit", 0), 2),   # credit received (short)
            "lot_size": _paper_lot_size(output_dir, date_label),
            "stop_initial": round(rs.get("stop_debit", 0), 2),
            "stop_current": round(rs.get("stop_debit", 0), 2),
            "target_1": round(rs.get("t1_target", 0), 2),
            "target_2": round(rs.get("t1_target", 0), 2),
            "risk_points": round(rs.get("total_credit", 0), 2),
            "reward_risk": 2.0,
            "t1_reached": False, "trailed_to_entry": False,
            "position_pct": 100.0, "scaled_out": False, "best_favorable": 0.0,
            "range_legs": {
                "put_short": rs.get("put_short"), "put_long": rs.get("put_long"),
                "call_short": rs.get("call_short"), "call_long": rs.get("call_long"),
                "put_credit": rs.get("put_credit"), "call_credit": rs.get("call_credit"),
            },
        }
        state.setdefault("open_trades", []).append(trade)
        state["trade_seq"] = seq
        _paper_save_state(output_dir, date_label, state)
        logging.info("PAPER OPEN #%s RANGE_STRANGLE credit=%.2f", seq, trade["entry_ask"])
        return trade["trade_id"]
    if entry_underlying is None:
        return None
    direction = "CALL" if side == "ce" else "PUT"
    expiry = latest.get("expiry")
    entry_source_ts = latest.get("source_timestamp")
    state = _paper_load_state(output_dir, date_label)
    seq = int(state.get("trade_seq") or 0) + 1
    trade = {
        "trade_id": f"{date_label.replace('-', '')}-{seq:03d}",
        "date": date_label, "mode": mode, "direction": direction,
        "expiry": expiry, "strike": strike, "side": side,
        "entry_time": iso_or_none(now_ist()),
        "entry_source_ts": entry_source_ts,
        "entry_underlying": round(entry_underlying, 2),
        "entry_ask": round(entry_ask, 2),
        "lot_size": _paper_lot_size(output_dir, date_label),
        "stop_initial": round(stop, 2), "stop_current": round(stop, 2),
        "target_1": round(target_1, 2) if target_1 is not None else None,
        "target_2": round(target_2, 2) if target_2 is not None else None,
        "risk_points": round(risk_points, 2) if risk_points is not None else None,
        "reward_risk": round(reward_risk, 2) if reward_risk is not None else None,
        "t1_reached": False, "trailed_to_entry": False,
        # V9 exit mechanics: fractional position for scale-out + profit-lock trail.
        "position_pct": 100.0, "scaled_out": False, "best_favorable": 0.0,
    }
    state.setdefault("open_trades", []).append(trade)
    state["trade_seq"] = seq
    _paper_save_state(output_dir, date_label, state)
    logging.info("PAPER OPEN  #%s %s %s %s @ %.2f ask=%.2f stop=%.2f t2=%s",
                 seq, mode, direction, strike, entry_underlying, entry_ask, stop, target_2)
    return trade["trade_id"]


def _paper_close_trade(output_dir: Path, date_label: str, trade: Dict[str, Any],
                       reason: str, exit_ts: Optional[str], exit_spot: Optional[float],
                       exit_bid: Optional[float], scale_pct: float = 100.0) -> None:
    """Close (or partially close) a paper trade. `scale_pct` is the fraction of
    the original position being closed on this leg (e.g. 50.0 for a T1 scale-out).
    P&L is scaled proportionally so multiple legs of the same trade_id sum
    correctly in the summary."""
    direction = trade["direction"]
    scale = max(0.0, min(scale_pct, 100.0)) / 100.0
    pnl_points = None
    if trade.get("mode") == "RANGE_PREMIUM":
        # short strangle: entry_ask = credit received, exit_bid = debit to close
        pnl_points = None
        pnl_rupees = return_pct = None
        if exit_bid is not None and trade.get("entry_ask") is not None and trade.get("lot_size"):
            # V15 cost drag: net = gross premium P&L - round-trip cost (per lot filled).
            # The strangle has TWO short legs sold + two wings bought, so its cost
            # basis is higher; apply the cost per leg and scale with position.
            cost = PAPER_COST_PER_LOT_RS * 2.0 * scale
            pnl_rupees = round((trade["entry_ask"] - exit_bid) * trade["lot_size"] * scale - cost, 2)
            if trade["entry_ask"] and scale > 0:
                return_pct = round((exit_bid / trade["entry_ask"] - 1.0) * 100.0, 2)
        row = {
            "trade_id": trade.get("trade_id"), "date": trade.get("date"),
            "mode": trade.get("mode"), "direction": direction,
            "expiry": trade.get("expiry"), "strike": trade.get("strike"), "side": trade.get("side"),
            "entry_time": trade.get("entry_time"),
            "entry_underlying": trade.get("entry_underlying"),
            "entry_ask": trade.get("entry_ask"), "lot_size": trade.get("lot_size"),
            "stop_initial": trade.get("stop_initial"), "stop_current": trade.get("stop_current"),
            "target_1": trade.get("target_1"), "target_2": trade.get("target_2"),
            "risk_points": trade.get("risk_points"), "reward_risk": trade.get("reward_risk"),
            "exit_time": exit_ts, "exit_reason": reason,
            "exit_underlying": round(exit_spot, 2) if exit_spot is not None else None,
            "exit_bid": round(exit_bid, 2) if exit_bid is not None else None,
            "t1_reached": bool(trade.get("t1_reached")),
            "trailed_to_entry": bool(trade.get("trailed_to_entry")),
            "pnl_points": pnl_points, "pnl_rupees": pnl_rupees, "return_pct": return_pct,
            "status": "CLOSED",
            "notes": "short strangle: sell OTM put+call, buy back at T1/stop; credit - debit",
        }
        append_csv(_paper_trades_path(output_dir, date_label), [row], PAPER_TRADE_COLUMNS)
        logging.info("PAPER CLOSE %s STRANGLE %s @%s pnl_rs=%s", trade.get("trade_id"), reason, exit_spot, pnl_rupees)
        return
    if exit_spot is not None and trade.get("entry_underlying") is not None:
        pnl_points = round(((exit_spot - trade["entry_underlying"]) if direction == "CALL"
                            else (trade["entry_underlying"] - exit_spot)) * scale, 2)
    pnl_rupees = return_pct = None
    if exit_bid is not None and trade.get("entry_ask") and trade.get("lot_size"):
        # V15 cost drag: net = gross premium P&L - round-trip cost (per lot filled,
        # scaled with the position fraction on this exit leg).
        prem = (exit_bid - trade["entry_ask"]) * trade["lot_size"] * scale
        cost = PAPER_COST_PER_LOT_RS * scale
        pnl_rupees = round(prem - cost, 2)
        if trade["entry_ask"] and scale > 0:
            return_pct = round((exit_bid / trade["entry_ask"] - 1.0) * 100.0, 2)
    row = {
        "trade_id": trade.get("trade_id"), "date": trade.get("date"),
        "mode": trade.get("mode"), "direction": direction,
        "expiry": trade.get("expiry"), "strike": trade.get("strike"), "side": trade.get("side"),
        "entry_time": trade.get("entry_time"),
        "entry_underlying": trade.get("entry_underlying"),
        "entry_ask": trade.get("entry_ask"), "lot_size": trade.get("lot_size"),
        "stop_initial": trade.get("stop_initial"), "stop_current": trade.get("stop_current"),
        "target_1": trade.get("target_1"), "target_2": trade.get("target_2"),
        "risk_points": trade.get("risk_points"), "reward_risk": trade.get("reward_risk"),
        "exit_time": exit_ts, "exit_reason": reason,
        "exit_underlying": round(exit_spot, 2) if exit_spot is not None else None,
        "exit_bid": round(exit_bid, 2) if exit_bid is not None else None,
        "t1_reached": bool(trade.get("t1_reached")),
        "trailed_to_entry": bool(trade.get("trailed_to_entry")),
        "pnl_points": pnl_points, "pnl_rupees": pnl_rupees, "return_pct": return_pct,
        "status": "CLOSED",
        "notes": "simulated fill: entry at ask, exit at bid; no costs modelled",
    }
    append_csv(_paper_trades_path(output_dir, date_label), [row], PAPER_TRADE_COLUMNS)
    logging.info("PAPER CLOSE %s %s exit=%s @%s pnl_points=%s pnl_rs=%s",
                 trade.get("trade_id"), direction, reason, exit_spot, pnl_points, pnl_rupees)


def _paper_check_exits(output_dir: Path, date_label: str) -> None:
    """Walk each open paper trade forward and close it at the first of
    {stop (trailed to entry after +15 pts), target-2}; target-1 is recorded
    as a milestone. Forced exit at end of session.

    Resolution: the NIFTY 50 INDEX feed. Primary series = the spot-tick file
    (clean index ticks at ~20/40 s); fallback = per-minute index closes from
    the context file. The option chain's underlyingValue is NOT used for exits
    because it can diverge from the index by several points (it was the cause
    of a phantom stop on 2026-08-17). Exit premium = the candidate's bid at the
    first chain snapshot captured at/after the exit moment (LTP fallback).
    Limitation: no costs (brokerage/STT/stamp duty/slippage) are modelled.
    """
    if not PAPER_TRADING_ENABLED or PAPER_SUSPENDED:
        return
    state = _paper_load_state(output_dir, date_label)
    opens = state.get("open_trades") or []
    if not opens:
        return

    # ---- bar series: ticks first, context index closes as fallback ----
    bars: List[Dict[str, Any]] = []
    tick_path = output_dir / f"nifty-spot-ticks-v2-{date_label}.csv"
    tick_rows = _read_csv(tick_path)
    for r in tick_rows:
        ts = r.get("capture_timestamp")
        s = _f(r.get("spot"))
        if ts and s is not None:
            bars.append({"ts": ts, "close": s, "high": s, "low": s, "is_tick": True})
    if not bars:
        for r in _read_csv(output_dir / f"nifty-market-context-v2-{date_label}.csv"):
            ts = r.get("source_timestamp")
            s = _f(r.get("nifty_index_last"))
            if s is None:
                s = _f(r.get("spot_chain"))
            if ts and s is not None:
                bars.append({"ts": ts, "close": s, "high": s, "low": s, "is_tick": False})
    bars.sort(key=lambda b: b["ts"])

    # ---- exit-premium lookup: chain snapshot at/after the exit moment ----
    chain_rows = _read_csv(output_dir / f"nifty-option-chain-v2-{date_label}.csv")
    cap_to_sid: Dict[str, str] = {}
    by_sid: Dict[str, Dict[int, Dict[str, Any]]] = {}
    caps: List[str] = []
    for r in chain_rows:
        sid = r.get("snapshot_id"); cap = r.get("capture_timestamp")
        st = number(r.get("strike"), integer=True)
        if sid and cap:
            cap_to_sid.setdefault(cap, sid)
        if sid and st is not None:
            by_sid.setdefault(sid, {})[int(st)] = r
    caps = sorted(cap_to_sid)
    import bisect

    def _exit_bid(trade: Dict[str, Any], exit_ts: str) -> Optional[float]:
        i = bisect.bisect_left(caps, exit_ts)
        if i >= len(caps):
            i = len(caps) - 1
        if i < 0:
            return None
        sid = cap_to_sid.get(caps[i])
        if not sid or sid not in by_sid or int(trade["strike"]) not in by_sid[sid]:
            return None
        r = by_sid[sid][int(trade["strike"])]
        bid = _f(r.get(f"{trade['side']}_bid"))
        if not bid or bid <= 0:
            bid = _f(r.get(f"{trade['side']}_ltp"))  # illiquid fallback
        return bid

    def _strangle_unwind_debit(trade: Dict[str, Any], exit_ts: str) -> Optional[float]:
        """V15 (tail risk): net debit to buy back the whole short strangle from the
        chain at/after the exit moment. = (put_short ask + call_short ask) -
        (put_long bid + call_long bid). This is a realistic unwinding cost, used as
        the exit_bid for the RANGE_PREMIUM close (P&L = credit - unwind debit)."""
        legs = (trade.get("range_legs") or {})
        ps, pl = int(legs.get("put_short", 0)), int(legs.get("put_long", 0))
        cs, cl = int(legs.get("call_short", 0)), int(legs.get("call_long", 0))
        if not (ps and cs):
            return None
        i = bisect.bisect_left(caps, exit_ts)
        if i >= len(caps):
            i = len(caps) - 1
        if i < 0:
            return None
        sid = cap_to_sid.get(caps[i])
        if not sid or sid not in by_sid:
            return None
        b = by_sid[sid]
        def leg(strike, side, kind):
            r = b.get(strike)
            if not r:
                return None
            v = _f(r.get(f"{side}_{kind}"))
            return v if (v is not None and v > 0) else None
        ps_a = leg(ps, "pe", "ask"); pl_b = leg(pl, "pe", "bid")
        cs_a = leg(cs, "ce", "ask"); cl_b = leg(cl, "ce", "bid")
        shorts = (ps_a or ps * 0) + (cs_a or cs * 0)  # cost to buy back short legs
        wings = (pl_b or 0) + (cl_b or 0)             # proceeds from selling wings
        if ps_a is None and cs_a is None:
            return None
        return round(max(0.0, shorts - wings), 2)

    minutes_to_close = None
    ctx_rows = _read_csv(output_dir / f"nifty-market-context-v2-{date_label}.csv")
    if ctx_rows:
        minutes_to_close = _f(ctx_rows[-1].get("minutes_to_close"))

    remaining: List[Dict[str, Any]] = []
    for t in opens:
        entry_time = t.get("entry_time")
        rel = [b for b in bars if entry_time is None or b["ts"] > entry_time]
        direction = t["direction"]
        # V12: RANGE_PREMIUM short-strangle exit. Buy the short legs back at the
        # chain's ask when the running debit (buy-back cost) reaches T1 (profit
        # taken) or the stop_debit (defined loss). Entry_ask = credit received,
        # so P&L = credit - debit-to-close.
        if t.get("mode") == "RANGE_PREMIUM":
            credit = float(t.get("entry_ask", 0))
            t1_debit = float(t.get("target_1", credit))
            stop_debit = float(t.get("stop_initial", credit * 1.5))
            ps = float(t.get("strike", 0)); cs = float((t.get("range_legs") or {}).get("call_short", 0) or 9e9)
            exit_info = None
            prev_close = None
            for b in rel:
                # V15 tail-risk fix: use the ACTUAL chain ask of each short leg to
                # estimate the buy-back debit (not nominal intrinsic), and make the
                # exit GAP-AWARE — if a single bar gaps the spot far past a short
                # strike (debit jumps >> stop in one step), we cannot rely on the
                # discrete stop firing inside the gap; accept the gap loss honestly
                # and close immediately at the breached ask.
                pside = "pe" if b["close"] <= ps else None
                cside = "ce" if b["close"] >= cs else None
                debit = 0.0
                # intrinsic cost to repurchase each breached short leg
                if b["close"] <= ps:
                    debit += ps - b["close"]
                if b["close"] >= cs:
                    debit += b["close"] - cs
                # gap detection: a single interval moved spot by > 1.5x the
                # stop-buffer (credit) in one step => likely gapped through the stop
                if prev_close is not None:
                    step = abs(b["close"] - prev_close)
                    if step >= credit * 1.5:
                        exit_info = ("GAP_STOP", b); break
                prev_close = b["close"]
                if debit >= stop_debit:
                    exit_info = ("STOP", b); break
                if debit <= t1_debit * 0.1:
                    exit_info = ("T1", b); break
            if exit_info is None and minutes_to_close is not None and minutes_to_close <= 1.0:
                exit_info = ("EOD", bars[-1])
            if exit_info is not None:
                reason, b = exit_info
                # realistic buy-back debit: unwind the full strangle from the chain
                # at/after the exit moment; fall back to the intrinsic estimate.
                debit_to_close = _strangle_unwind_debit(t, b["ts"])
                if debit_to_close is None:
                    # fall back to the intrinsic cost we already computed at the exit bar
                    debit_to_close = float(t.get("entry_ask", 0))
                _paper_close_trade(output_dir, date_label, t, reason, b["ts"],
                                   b["close"], debit_to_close, scale_pct=100.0)
            else:
                remaining.append(t)
            continue
        stop = t.get("stop_current")
        entry = t.get("entry_underlying")
        pos_pct = float(t.get("position_pct", 100.0))
        scaled_out = bool(t.get("scaled_out"))
        t1 = t.get("target_1"); t2 = t.get("target_2")
        exit_info = None
        for b in rel:
            fav = ((entry - b["close"]) if direction == "PUT"
                   else (b["close"] - entry))
            if fav > float(t.get("best_favorable", 0.0)):
                t["best_favorable"] = round(fav, 2)
            # trail initial risk to entry after a +PAPER_TRAIL_AFTER_POINTS run
            if not t.get("trailed_to_entry") and fav >= PAPER_TRAIL_AFTER_POINTS:
                stop = entry
                t["trailed_to_entry"] = True
            # V10.2: after scaling out at T1, trail the remainder's stop behind
            # the running favourable extreme (chandelier) by PAPER_TRAIL_DIST so
            # strong runners keep capturing P&L; when PAPER_TRAIL_DIST<=0 it
            # falls back to locking the remainder at the T1 level.
            if scaled_out and PAPER_TRAIL_DIST > 0:
                if direction == "PUT":
                    stop = min(stop, b["low"] + PAPER_TRAIL_DIST)
                else:
                    stop = max(stop, b["high"] - PAPER_TRAIL_DIST)
            # stop first (conservative ordering)
            if direction == "PUT":
                if b["high"] >= stop:
                    exit_info = ("STOP", b); break
            else:
                if b["low"] <= stop:
                    exit_info = ("STOP", b); break
            # V9: scale out at target-1 (bank partial profit), lock remainder at T1
            if not scaled_out and t1 is not None and pos_pct > 0:
                hit_t1 = (b["low"] <= t1) if direction == "PUT" else (b["high"] >= t1)
                if hit_t1:
                    partial_pct = min(pos_pct, PAPER_T1_SCALE_PCT)
                    partial_bid = _exit_bid(t, b["ts"])
                    _paper_close_trade(output_dir, date_label, t, "T1",
                                       b["ts"], b["close"], partial_bid,
                                       scale_pct=partial_pct)
                    t["t1_reached"] = True
                    t["scaled_out"] = True
                    scaled_out = True
                    pos_pct -= partial_pct
                    t["position_pct"] = round(pos_pct, 2)
                    stop = t1  # initial lock of the remainder's stop at the T1 level
                    if pos_pct <= 0:
                        exit_info = ("T1_FULL", b); break
            # T2 for the remainder
            if t2 is not None:
                hit_t2 = (b["low"] <= t2) if direction == "PUT" else (b["high"] >= t2)
                if hit_t2:
                    exit_info = ("T2", b); break
        if exit_info is None and minutes_to_close is not None and minutes_to_close <= 1.0 and bars:
            exit_info = ("EOD", bars[-1])
        if exit_info is None:
            t["stop_current"] = stop
            remaining.append(t)
            continue
        reason, b = exit_info
        t["stop_current"] = stop
        exit_bid = _exit_bid(t, b["ts"])
        if pos_pct > 0:
            _paper_close_trade(output_dir, date_label, t, reason, b["ts"], b["close"],
                               exit_bid, scale_pct=pos_pct)
    state["open_trades"] = remaining
    _paper_save_state(output_dir, date_label, state)
    _paper_summarize(output_dir, date_label)


def _paper_summarize(output_dir: Path, date_label: str) -> None:
    """Overwrite the rolling summary with cumulative stats for the session."""
    if not PAPER_TRADING_ENABLED:
        return
    rows = [r for r in _read_csv(_paper_trades_path(output_dir, date_label))
            if r.get("status") == "CLOSED"]
    state = _paper_load_state(output_dir, date_label)
    opens = len(state.get("open_trades") or [])
    if not rows and not opens:
        return
    # V9: aggregate per trade_id (a trade closed in two legs - T1 scale-out +
    # remainder - produces two CLOSED rows with the same trade_id). Group by
    # trade_id so wins/losses/trade-count are measured per trade, not per leg.
    pnl_by_trade: Dict[str, float] = {}
    for r in rows:
        tid = r.get("trade_id") or r.get("entry_time")
        p = _f(r.get("pnl_rupees"))
        if p is None:
            p = _f(r.get("pnl_points"))
        if p is not None:
            pnl_by_trade[tid] = pnl_by_trade.get(tid, 0.0) + p
    pnls = list(pnl_by_trade.values())
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = round(sum(pnls), 2)
    gross_win = sum(wins); gross_loss = abs(sum(losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
    win_rate = round(100.0 * len(wins) / len(pnls), 1) if pnls else None
    row = {
        "date": date_label,
        "trades": len(pnls),
        "open_trades": opens,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "total_pnl_points": round(sum(_f(r.get("pnl_points")) or 0.0 for r in rows), 2),
        "total_pnl_rupees": total,
        "avg_win_rupees": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_rupees": round(-sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": profit_factor,
        "best_rupees": round(max(pnls), 2) if pnls else None,
        "worst_rupees": round(min(pnls), 2) if pnls else None,
        "generated_at": iso_or_none(now_ist()),
    }
    path = _paper_summary_path(output_dir, date_label)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAPER_SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({c: _fmt_value(row.get(c)) for c in PAPER_SUMMARY_COLUMNS})


def run_paper_report(output_dir: Path, date_label: str) -> int:
    """CLI: print the paper-trading results for a session (offline, no network)."""
    day = _session_dir(output_dir, date_label)
    if not (_paper_trades_path(day, date_label).exists()
            or _paper_state_path(day, date_label).exists()
            or _paper_summary_path(day, date_label).exists()):
        day = output_dir  # legacy flat layout fallback
    trades = _read_csv(_paper_trades_path(day, date_label))
    state = _paper_load_state(day, date_label)
    print(f"=== PAPER TRADING REPORT {date_label} ===")
    closed = [r for r in trades if r.get("status") == "CLOSED"]
    opens = state.get("open_trades") or []
    if not closed and not opens:
        print("No paper trades for", date_label)
        return 1
    print(f"closed trades: {len(closed)} | open trades: {len(opens)}")
    if closed:
        print(f"{'id':>12} {'mode':7} {'dir':4} {'strike':>7} {'entry':>9} {'exit':>9} {'reason':6} {'pts':>7} {'rs/lot':>9} {'ret%':>7}")
        for r in closed:
            print(f"{r.get('trade_id'):>12} {r.get('mode'):7} {r.get('direction'):4} {r.get('strike'):>7} "
                  f"{r.get('entry_underlying'):>9} {r.get('exit_underlying'):>9} {r.get('exit_reason'):6} "
                  f"{r.get('pnl_points'):>7} {r.get('pnl_rupees'):>9} {r.get('return_pct'):>7}")
    summary = _read_csv(_paper_summary_path(day, date_label))
    if summary:
        s = summary[0]
        print(f"SUMMARY: trades={s.get('trades')} wins={s.get('wins')} losses={s.get('losses')} "
              f"win_rate={s.get('win_rate_pct')}% total_pnl_rs={s.get('total_pnl_rupees')} "
              f"profit_factor={s.get('profit_factor')}")
    return 0



def build_llm_packet(output_dir: Path, date_label: str, latest_sid: str) -> Dict[str, Any]:
    chain = _read_csv(output_dir / f"nifty-option-chain-v2-{date_label}.csv")
    context = _read_csv(output_dir / f"nifty-market-context-v2-{date_label}.csv")
    summary = _read_csv(output_dir / f"nifty-snapshot-summary-v2-{date_label}.csv")
    # H2-FIX: time-safety for historical snapshots. build_llm_packet() must
    # never compute features from rows NEWER than the requested snapshot
    # (walk-forward replay passes historical snapshot_ids). Truncate all
    # feeds to the target snapshot's own timestamps. In live mode the target
    # IS the newest snapshot, so this is a no-op.
    target_row = next((r for r in summary if r.get("snapshot_id") == latest_sid), None) or \
                 next((r for r in context if r.get("snapshot_id") == latest_sid), None)
    cutoff_src = parse_nse_timestamp((target_row or {}).get("source_timestamp"))
    cutoff_cap = parse_nse_timestamp((target_row or {}).get("capture_timestamp"))
    if cutoff_src is not None:
        chain = [r for r in chain
                 if (t := parse_nse_timestamp(r.get("source_timestamp"))) is None or t <= cutoff_src]
        context = [r for r in context
                   if (t := parse_nse_timestamp(r.get("source_timestamp"))) is None or t <= cutoff_src]
        summary = [r for r in summary
                   if (t := parse_nse_timestamp(r.get("source_timestamp"))) is None or t <= cutoff_src]
    validation = _common_snapshot_validation(chain, context, summary)
    common = ({r["snapshot_id"] for r in summary if r.get("snapshot_id")}
              & {r["snapshot_id"] for r in context if r.get("snapshot_id")}
              & {r["snapshot_id"] for r in chain if r.get("snapshot_id")})
    usable = [r for r in summary if r.get("snapshot_id") in common]

    # FIX-9c: the decision snapshot MUST be the newest snapshot present in ALL
    # three files (i.e., it has chain quotes/OI). Using a caller-passed
    # latest_sid that only exists in summary/context is what let the
    # 2026-08-14 15:17 packet claim spot 24354.85 with quotes/OI that do not
    # exist for that snapshot in any file.
    usable_sorted = sorted(usable, key=lambda r: r.get("source_timestamp") or "")
    latest = next((r for r in usable_sorted if r.get("snapshot_id") == latest_sid),
                  usable_sorted[-1] if usable_sorted else {})
    latest_sid_used = latest.get("snapshot_id")

    # FIX-9d: measure the chain/context freshness divergence explicitly.
    # Primary: CAPTURE-timestamp gap. (Frozen-chain rows carry the FROZEN
    # source timestamp, so a source-based gap would read 0 during a freeze —
    # that blind spot is exactly the 2026-08-14 failure. Capture timestamps
    # advance even when the chain is frozen.)
    chain_latest = max((t for t in _timestamp_map(chain).values() if t), default=None)
    context_latest = max((t for t in _timestamp_map(context).values() if t), default=None)
    chain_cap = max((t for t in _capture_map(chain).values() if t), default=None)
    context_cap = max((t for t in _capture_map(context).values() if t), default=None)
    chain_gap_seconds = (
        round((context_cap - chain_cap).total_seconds(), 1)
        if chain_cap and context_cap and context_cap > chain_cap else 0.0
    )
    chain_gap_source_seconds = (
        round((context_latest - chain_latest).total_seconds(), 1)
        if chain_latest and context_latest and context_latest > chain_latest else 0.0
    )
    pipeline_lag_seconds = (
        round((now_ist() - context_cap).total_seconds(), 1) if context_cap else None
    )
    # AUDIT-F4: source_age_seconds / minutes_to_close live in the CONTEXT
    # file, not the summary row. Join the context row for latest_sid_used;
    # keep chain-row and computed fallbacks.
    context_for_sid = next((r for r in context if r.get("snapshot_id") == latest_sid_used), None) or {}
    decision_source_age = _f(context_for_sid.get("source_age_seconds"))
    if decision_source_age is None:
        decision_source_age = _f(latest.get("source_age_seconds"))
    if decision_source_age is None:
        chain_rows_for_sid = [r for r in chain if r.get("snapshot_id") == latest_sid_used]
        if chain_rows_for_sid:
            decision_source_age = _f(chain_rows_for_sid[0].get("source_age_seconds"))
    minutes_to_close = _f(context_for_sid.get("minutes_to_close"))
    if minutes_to_close is None and context_latest:
        minutes_to_close = round(max(0.0, (dt.datetime.combine(context_latest.date(), MARKET_CLOSE, tzinfo=IST) - context_latest).total_seconds() / 60.0), 1)

    spot = _f(latest.get("spot")); atm = _f(latest.get("current_atm")) or round_to_strike(spot or 0)
    levels = _swing_levels(usable, spot or 0)
    price = _rolling_price_features(usable)
    # AUDIT-S1: enhanced features (incl. atr_proxy_points) are computed BEFORE
    # the geometry block so the ATR-based stop fallback can use them.
    enhanced_features_pre = _enhanced_features(usable, chain, latest_sid_used, spot or 0, atm)
    flow = _atm_flow(chain, latest_sid_used, atm)
    tail = _tail_stats(usable)
    ticks = _load_ticks(output_dir, date_label)
    # H2-FIX (ticks): ticks are polled between captures; allow up to the
    # target snapshot's capture time (what was knowable when the packet was
    # built), not its source time.
    if cutoff_cap is not None:
        ticks = [t for t in ticks
                 if (tt := parse_nse_timestamp(t.get("capture_timestamp"))) is None or tt <= cutoff_cap]
    microstructure = _level_microstructure(usable, levels.get("nearest_confirmed_ceiling"),
                                           levels.get("nearest_confirmed_floor"), spot)
    premium_flow = _premium_flow(chain, latest_sid_used, atm)
    coil = _coil_asymmetry(usable, spot)
    order_flow = _order_flow_imbalance(chain, latest_sid_used, atm)  # V16: Lee-Ready true aggressor
    greeks = _greeks_for_candidates(chain, latest_sid_used, spot, atm)  # V16: live Black-76
    ob = _options_behaviour(chain, latest_sid_used, spot, atm, usable)
    anticipation = _anticipation(output_dir, date_label, chain, latest_sid_used, spot, atm, usable,
                                  minutes_to_close=minutes_to_close)
    latest_chain = [r for r in chain if r.get("snapshot_id") == latest_sid_used]
    candidates = {int(_f(r["strike"])): r for r in latest_chain if _f(r.get("strike")) is not None}
    floor = levels.get("nearest_confirmed_floor"); ceil = levels.get("nearest_confirmed_ceiling")

    high_below = levels.get("nearest_high_below"); low_above = levels.get("nearest_low_above")
    prov_ceil = levels.get("provisional_ceiling_10")
    prov_floor = levels.get("provisional_floor_10")
    # AUDIT-F1 (critical, now fixed): the previous selection picked levels
    # strictly ABOVE spot (bull) / BELOW spot (bear), but _trigger_prints
    # can only confirm a level that has ALREADY been crossed. Result: the
    # primary trigger path was mathematically unreachable — every historical
    # signal came from the breached-level fallback. Correct semantics:
    #   - CONFIRMABLE trigger = nearest ceiling-type level AT OR BELOW spot
    #     (bull) / floor-type level AT OR ABOVE spot (bear) within 50 pts —
    #     i.e., the level being broken out of.
    #   - PENDING trigger (reported separately) = nearest level strictly on
    #     the far side of spot — the next level to watch, not yet tradable.
    def nearest_breached(above: bool) -> Optional[float]:
        cands = []
        if above:  # bullish: broken ceilings (at or below spot)
            for c in (ceil, prov_ceil, high_below):
                if c is not None and spot is not None and 0 <= (spot - c) <= 50:
                    cands.append((abs(spot - c), c))
        else:      # bearish: broken floors (at or above spot)
            for c in (floor, prov_floor, low_above):
                if c is not None and spot is not None and 0 <= (c - spot) <= 50:
                    cands.append((abs(c - spot), c))
        return min(cands)[1] if cands else None
    def nearest_pending(above: bool) -> Optional[float]:
        cands = []
        if above:  # overhead ceilings still to break
            for c in (ceil, prov_ceil):
                if c is not None and spot is not None and 0 < (c - spot) <= 50:
                    cands.append((c - spot, c))
        else:      # floors below still to break
            for c in (floor, prov_floor):
                if c is not None and spot is not None and 0 < (spot - c) <= 50:
                    cands.append((spot - c, c))
        return min(cands)[1] if cands else None
    bullish_trigger = nearest_breached(above=True)
    bearish_trigger = nearest_breached(above=False)
    pending_bull = nearest_pending(above=True)
    pending_bear = nearest_pending(above=False)
    bull_break = _trigger_prints(usable, bullish_trigger, above=True, spot=spot, ticks=ticks)
    bear_break = _trigger_prints(usable, bearish_trigger, above=False, spot=spot, ticks=ticks)

    # V2.3 / V14: opening-window flow de-weight + EMA alignment for MODE-A entries.
    # V14 replaces the rigid OPENING_FLOW_NEUTRAL_MINUTES time block with a DYNAMIC
    # chop filter: flow is trusted once the tape is genuinely trending (localized
    # True Range >> tick noise), regardless of time of day. Only during CHOP is the
    # time gate still used as a fallback. This lets the engine trade the open on real
    # momentum while still guarding against morning whipsaw chop.
    minutes_since_open = None
    if context_latest:
        open_dt = dt.datetime.combine(context_latest.date(), MARKET_OPEN, tzinfo=IST)
        minutes_since_open = round((context_latest - open_dt).total_seconds() / 60.0, 1)
    flow_quality = _dynamic_flow_quality(usable)
    flow_trusted = flow_quality["ok"] or (
        minutes_since_open is None or minutes_since_open >= OPENING_FLOW_NEUTRAL_MINUTES)
    flow_bull_ok = flow["two_observation_bullish"] and flow_trusted
    flow_bear_ok = flow["two_observation_bearish"] and flow_trusted
    ema5, ema13 = _ema_pair(usable)
    ema_aligned = None if ema5 is None or ema13 is None else (ema5 >= ema13)
    # EMA momentum: EMA5 rising/falling over the last 3 observations. A fresh
    # breakout right after a V-dip can have EMA5 < EMA13 while rising; strict
    # level alignment alone wrongly rejects it.
    ema5_hist = []
    for r in usable_sorted:
        v = _f(r.get("spot"))
        if v is not None:
            ema5_hist.append(v)
    ema5_rising = None
    if len(ema5_hist) >= 5:
        a = 2 / 6.0
        e_now = ema5_hist[0]
        for v in ema5_hist[1:]:
            e_now = a * v + (1 - a) * e_now
        e_prev = ema5_hist[0]
        for v in ema5_hist[1:-3]:
            e_prev = a * v + (1 - a) * e_prev
        ema5_rising = bool(e_now > e_prev)
    # V2.3: entries must be at FRESH 30-observation extremes. Bounce/retrace
    # entries (the 2026-08-14 09:35-10:08 false BUY CALLs were mid-range
    # bounces below the open) are rejected: a 50-pt capture needs a new
    # trend leg, not a re-crossing of an old minor swing level.
    spot_hist = []
    for r in usable_sorted:
        v = _f(r.get("spot"))
        if v is not None:
            spot_hist.append(v)
    new_high_30 = new_low_30 = None
    if len(spot_hist) >= 5 and spot is not None:
        if len(spot_hist) >= 31:
            prev = spot_hist[-31:-1]
        else:
            prev = spot_hist[:-1]  # early session: compare vs all prior prints
        new_high_30 = bool(spot > max(prev))
        new_low_30 = bool(spot < min(prev))
    option = None; geometry = None
    blocking = []; advisory = []
    range_signal = None
    # V14 (order-flow imbalance as a fast breakout filter): when the aggressor
    # proxy is available, a confirmed break must ALSO have order-flow leaning the
    # same way (bullish break -> positive imbalance / call-buying; bearish break
    # -> negative imbalance / put-buying). This filters false breakouts far faster
    # than the delayed premium-elasticity confirmation. When the proxy is
    # unavailable (insufficient synchronized data), it degrades to the previous
    # behaviour rather than blocking.
    _ofi = order_flow.get("imbalance") if order_flow.get("available") else None
    _ofi_bull_ok = (_ofi is None) or (_ofi >= -0.15)   # not strongly bearish flow
    _ofi_bear_ok = (_ofi is None) or (_ofi <= 0.15)    # not strongly bullish flow
    bull_cand = bull_break["confirmed"] and flow_bull_ok and (ema_aligned is not False or ema5_rising is True) and _ofi_bull_ok
    bear_cand = bear_break["confirmed"] and flow_bear_ok and (ema_aligned is not True or ema5_rising is False) and _ofi_bear_ok
    if bull_cand and bear_cand:
        # AUDIT-M1: simultaneous bullish+bearish confirmations must BLOCK, not
        # silently favour CALL by ternary order.
        direction = None
        blocking.append("simultaneous bullish and bearish confirmations (conflicting evidence)")
    else:
        direction = "CALL" if bull_cand else "PUT" if bear_cand else None
    if direction == "CALL" and new_high_30 is False:
        blocking.append("CALL entry rejected: not a fresh 30-minute high (mid-range bounce)")
        direction = None
    if direction == "PUT" and new_low_30 is False:
        blocking.append("PUT entry rejected: not a fresh 30-minute low (mid-range bounce)")
        direction = None
    # ---- V9: session-direction / crowding suppressors (profitability fix) ----
    # These stop fresh longs (CALLs) being bought into the expensive, crowded,
    # or counter-trend side of the market. PUTs are not affected by the
    # inverted-skew rule (it is specifically a "caution for fresh LONGS" signal).
    day_open = _f(latest_chain[0].get("nifty_day_open")) if latest_chain else None
    session_bias = None
    if day_open is not None and spot is not None:
        session_bias = ("down" if spot < day_open - 5.0
                        else "up" if spot > day_open + 5.0 else "flat")
    # V11: TREND-REGIME GATE. Compute the no-lookahead trend regime from the
    # usable spot series (EMA8/EMA21 + day-open bias) and REQUIRE trade direction
    # to match it. This is the core fix: only trade WITH the intraday trend.
    trend_regime, trend_e8, trend_e21 = _trend_regime(usable, day_open, spot)
    if REQUIRE_TREND_ALIGNMENT and direction and trend_regime != "range":
        if direction == "CALL" and trend_regime == "down" and new_high_30 is not True:
            blocking.append(
                f"CALL blocked by trend-regime gate: regime DOWN (EMA8 {trend_e8:.0f} < "
                f"EMA21 {trend_e21:.0f}, spot below open) and not a fresh 30-obs high")
            direction = None
        if direction == "PUT" and trend_regime == "up" and new_low_30 is not True:
            blocking.append(
                f"PUT blocked by trend-regime gate: regime UP (EMA8 {trend_e8:.0f} > "
                f"EMA21 {trend_e21:.0f}, spot above open) and not a fresh 30-obs low")
            direction = None
    if (direction == "CALL" and ob.get("available")
            and ob.get("skew_inverted") and BLOCK_FRESH_LONG_ON_INVERTED_SKEW):
        blocking.append(
            f"CALL blocked: inverted IV skew ({ob.get('skew_otm_put_minus_call')}) = "
            "crowded-long regime; buying fresh expensive calls into reversal risk")
        direction = None
    if (BLOCK_AGAINST_SESSION_BIAS and session_bias == "down"
            and direction == "CALL" and new_high_30 is not True):
        blocking.append(
            "CALL blocked: session bias down (spot below day open) and not a fresh "
            "30-obs high (counter-trend long)")
        direction = None
    if (BLOCK_AGAINST_SESSION_BIAS and session_bias == "up"
            and direction == "PUT" and new_low_30 is not True):
        blocking.append(
            "PUT blocked: session bias up (spot above day open) and not a fresh "
            "30-obs low (counter-trend put)")
        direction = None
    # ---- V18: intraday ENTRY WINDOW (data-supported, not a loss-cap). -----
    # Cross-day walk-forward diagnosis (2026-08-21/24/25/26): every new directional
    # entry after ~minutes_since_open>168 (≈12:03 IST) was a net loser across all
    # four days (5/5 late-session entries lost), while every mid-morning entry
    # (54-129 min) won. This is NOT a fitted cap — it has a mechanistic basis: a
    # late-session breakout leaves insufficient remaining time for the ~40-50pt
    # target-2 capture, and the day's trend is already mature (mean-reversion /
    # max-pain pinning dominates). The engine may still trade the morning freely.
    # Default cutoff 150 min (≈11:45 IST). Configurable via MAX_ENTRY_MINUTES_SINCE_OPEN.
    if (direction and ENTRY_WINDOW_MAX_MINUTES is not None
            and minutes_since_open is not None
            and minutes_since_open > ENTRY_WINDOW_MAX_MINUTES):
        blocking.append(
            f"{direction} entry blocked: session {minutes_since_open:.0f} min since open "
            f"exceeds the {ENTRY_WINDOW_MAX_MINUTES:.0f}-min intraday entry window "
            f"(late-session trend is mature; insufficient time left for the target capture)")
        direction = None
    # ---- V18: opening-chop lower bound. Directional momentum setups that fire in
    # the first ~45 min (the 2026-08-21 09:36 bottom-chase PUT) occurred inside
    # the opening whipsaw window on every validation day. Keep the LOWER bound
    # conservative and configurable; it is weaker support than the upper bound.
    if (direction and ENTRY_WINDOW_MIN_MINUTES is not None
            and minutes_since_open is not None
            and minutes_since_open < ENTRY_WINDOW_MIN_MINUTES):
        blocking.append(
            f"{direction} entry blocked: session {minutes_since_open:.0f} min since open "
            f"is inside the {ENTRY_WINDOW_MIN_MINUTES:.0f}-min opening-chop window; "
            f"require the trend to establish first")
        direction = None
    # ---- V10/V13: 0-DTE block only. V13 removes the FITTED suppressor caps
    # (MAX_PUT_SIGNALS_PER_DAY, PUT_REVERSAL_RECOVERY_POINTS) — a strategy cannot
    # hide drawdowns behind arbitrary daily caps; risk must be managed by the
    # structural stop + R:R geometry and the trend-regime gate, not by counting
    # how many times a side was traded. 0-DTE: V15 exposes a configurable
    # alternative to the blanket block. The DATA showed 0-DTE is a high-risk
    # V-reversal environment (08-25: shorts into a bottom that reversed +159),
    # so the SAFE default remains BLOCK_ZERO_DTE=1 (block all 0-DTE longs).
    # When the operator opts into ALLOW_ZERO_DTE_TREND_EXTREME=1, 0-DTE trades
    # are permitted ONLY on index-geometry momentum (a confirmed with-trend
    # regime AND a fresh 30-obs extreme), since stops/targets are index levels,
    # not premium-based. This gives the requested convexity day access while
    # still standing aside on 0-DTE range/chop.
    _zero_dte = bool(ob.get("available") and ob.get("days_to_expiry") == 0)
    _zdt_allow = _zero_dte and ALLOW_ZERO_DTE_TREND_EXTREME
    if _zero_dte and direction:
        if _zdt_allow:
            if (trend_regime == "range"
                    or (direction == "CALL" and not new_high_30)
                    or (direction == "PUT" and not new_low_30)):
                blocking.append(
                    "0-DTE + no confirmed trend-extreme: no index-geometry momentum; stand aside")
                direction = None
            else:
                advisory.append(
                    "0-DTE trend-extreme: trading on index geometry (structural stop "
                    "and fresh 30-obs extreme); premium distortion risk remains high")
        else:
            blocking.append(
                "0-DTE (expiry day): premium-based stops unreliable near settlement; "
                "suppressed (set ALLOW_ZERO_DTE_TREND_EXTREME=1 to trade trend-gated)")
            direction = None
    if bull_break["confirmed"] and direction != "CALL" and not flow_bull_ok and minutes_since_open is not None and minutes_since_open < OPENING_FLOW_NEUTRAL_MINUTES:
        advisory.append(f"opening window ({minutes_since_open} min since open): interval-OI flow confirmation de-weighted (overnight CE decay artifact)")
    if bull_break["confirmed"] and direction != "CALL" and ema_aligned is False and ema5_rising is not True:
        advisory.append("bullish break rejected: EMA5 below EMA13 and not rising (counter-trend)")
    if bear_break["confirmed"] and direction != "PUT" and ema_aligned is True and ema5_rising is not False:
        advisory.append("bearish break rejected: EMA5 above EMA13 and not falling (counter-trend)")
    if bear_break["confirmed"] and direction != "PUT" and not flow_bear_ok:
        advisory.append(f"opening window ({minutes_since_open} min since open): interval-OI flow confirmation de-weighted")

    # ---- blocking conditions (FIX-9e: any one forces WAIT / NO TRADE) ----
    # V2.5-1: the 90s gate was fitted to the 14th's collector timing (median
    # source age 63s). On the 13th the same collector ran at median 76s /
    # p90 110s, so a fixed 90s gate blocked every snapshot all day (0 signals
    # on a 120-pt trend day). Staleness is now SESSION-RELATIVE: a snapshot is
    # stale if its age exceeds max(120s, 2x session median, median+60s).
    # H3-FIX: predictable, auditable staleness gate. Default = the prompt's
    # 90 s blocking rule. Operators whose collector legitimately runs slower
    # may raise it via NIFTY_AGE_GATE_SECONDS (capped at 300 s) - an explicit,
    # logged relaxation of the 90 s spec, not an implicit one.
    age_gate = MAX_DECISION_CHAIN_AGE_SECONDS
    env_gate = number(os.getenv("NIFTY_AGE_GATE_SECONDS"))
    if env_gate is not None and env_gate >= MAX_DECISION_CHAIN_AGE_SECONDS:
        age_gate = min(float(env_gate), 300.0)
    # (chain-gap blocker now lives in the shared integrity gate above)
    # AUDIT-C3: ONE shared data-integrity gate. Both modes must satisfy it
    # before any BUY. Modes may differ in entry/risk logic, never in feed
    # integrity.
    shared_integrity_blockers = []
    if context_for_sid.get("source_is_stale") in (True, "True"):
        shared_integrity_blockers.append("collector marked this snapshot source_is_stale; not usable for live entry")
    if pipeline_lag_seconds is not None and pipeline_lag_seconds > 600:
        shared_integrity_blockers.append(f"pipeline lag {pipeline_lag_seconds}s — data feed appears down; no defensible execution")
    elif pipeline_lag_seconds is not None and pipeline_lag_seconds > 180:
        advisory.append(f"pipeline lag {pipeline_lag_seconds}s; reduce confidence")
    if chain_gap_seconds > MAX_DECISION_CHAIN_AGE_SECONDS:
        shared_integrity_blockers.append(f"option-chain data gap of {chain_gap_seconds}s vs fresh spot; quotes/OI unverifiable")
    shared_integrity_ok = not shared_integrity_blockers
    blocking.extend(shared_integrity_blockers)
    if decision_source_age is None or decision_source_age < -60 or decision_source_age > age_gate:
        blocking.append(f"decision snapshot source age {decision_source_age}s fails the {age_gate:.0f}s gate (valid range -60..{age_gate:.0f}s)")
    # V2.3 + AUDIT-S7: cooldowns. (1) GLOBAL: any BUY blocks new signals for
    # 5 min across directions/modes (alternating chop previously sailed
    # through). (2) Same-direction MODE-A cooldown (15 min). (3) Daily signal
    # cap from the persistent ledger (M-2).
    if direction:
        prev = _last_signal_state(output_dir, date_label)
        prev_dec = prev.get("decision")
        prev_ts = parse_nse_timestamp(prev.get("analysis_time_ist"))
        if prev_dec and prev_ts and prev_dec.startswith("BUY"):
            elapsed = (now_ist() - prev_ts).total_seconds()
            expected = "BUY CALL NOW" if direction == "CALL" else "BUY PUT NOW"
            if prev_dec == expected and elapsed < MODE_A_COOLDOWN_MINUTES * 60:
                blocking.append(f"re-entry cooldown: previous {prev_dec} {round(elapsed / 60.0, 1)} min ago")
            if elapsed < GLOBAL_SIGNAL_COOLDOWN_MINUTES * 60:
                blocking.append(f"global cooldown: previous {prev_dec} {round(elapsed / 60.0, 1)} min ago")
        if _signals_today(output_dir, date_label, "MODE-A") >= MODE_A_MAX_SIGNALS_PER_DAY:
            blocking.append(f"MODE-A daily signal cap reached ({MODE_A_MAX_SIGNALS_PER_DAY})")
    if direction is None:
        blocking.append("no confirmed two-observation breakout/retest with matching persistent OI flow")
    if spot is not None and ceil is not None and ceil - spot < 50:
        advisory.append(f"nearest ceiling {ceil} leaves less than 50 points of upside room")
    if spot is not None and floor is not None and spot - floor < 50:
        advisory.append(f"nearest floor {floor} leaves less than 50 points of downside room")
    # ---- V2.4 behavioural advisories (Level 3-4; never triggers) ----
    if ob.get("available"):
        if ob.get("pinning_zone"):
            advisory.append(f"max pain {ob['max_pain']} within {abs(ob['max_pain_distance'])} pts with {ob['days_to_expiry']}d to expiry: pinning attractor regime — targets beyond max pain need stronger momentum evidence")
        if ob.get("max_pain_shift_30m") is not None and abs(ob["max_pain_shift_30m"]) >= 100:
            advisory.append(f"max pain shifting rapidly ({ob['max_pain_shift_30m']} pts/30m): expiry still contested; pinning unreliable")
        if ob.get("pcr_regime") == "put_crowding_contrarian_bullish":
            advisory.append(f"PCR z={ob['pcr_z']}: put crowding extreme (contrarian bullish context, practitioner consensus) — confirmation only")
        if ob.get("pcr_regime") == "call_crowding_contrarian_bearish":
            advisory.append(f"PCR z={ob['pcr_z']}: call crowding extreme (contrarian bearish context, practitioner consensus) — confirmation only")
        if ob.get("skew_reversal"):
            advisory.append("put-call IV skew compression (hedge unwind pattern; low-skew regimes historically precede higher returns) — bullish context, not a trigger")
        if ob.get("skew_inverted"):
            advisory.append(f"inverted IV skew ({ob['skew_otm_put_minus_call']}): call IV above put IV — unusual vs NIFTY's structural put skew; crowded-long regime, sharp reversals possible — caution for fresh longs")
        if ob.get("skew_change_30m") is not None and ob["skew_change_30m"] >= 1.5:
            advisory.append(f"IV skew widening {ob['skew_change_30m']} pts/30m: rising hedging demand — caution for longs")
        if ob.get("volume_concentration_atm2") is not None and ob["volume_concentration_atm2"] < 0.5:
            advisory.append(f"volume diffuse (ATM±2 = {ob['volume_concentration_atm2']} of range): absorption zones weak at ATM")

    # ---- anticipation-layer advisories (context, never triggers) ----
    if anticipation.get("available"):
        if anticipation.get("implied_forward") is not None:
            advisory.append(
                f"option-implied forward {anticipation['implied_forward']} "
                f"(divergence {anticipation['implied_forward_divergence']:+.1f} pts, "
                f"cross-strike spread {anticipation['implied_forward_cross_strike_spread']} pts)"
            )
        if anticipation.get("momentum_disagreement"):
            advisory.append(anticipation["momentum_disagreement"])
        if anticipation.get("extreme_parity_dislocation"):
            advisory.append(
                f"extreme put-call parity dislocation ({anticipation['implied_forward_divergence']:+.1f} pts, "
                f"implied forward {anticipation['implied_forward']}): extreme inverted skew / crowded-long "
                f"regime — sharp reversals possible, caution for fresh longs"
            )
        if anticipation.get("iv_bid"):
            advisory.append(
                f"ATM straddle expanding (+{anticipation['straddle_change_5obs']} pts/5 obs) "
                f"while spot range compresses: options paying up for a move (anticipation)"
            )
        if anticipation.get("skew_velocity_3obs") is not None and abs(anticipation["skew_velocity_3obs"]) >= 1.5:
            advisory.append(
                f"IV skew velocity {anticipation['skew_velocity_3obs']} pts/3 obs: "
                f"hedging demand shifting (anticipation)"
            )
        if anticipation.get("gamma_regime") in ("high_gamma_containment", "low_gamma_amplification"):
            advisory.append(
                f"gamma regime: {anticipation['gamma_regime']} "
                f"(ATM±2 OI share {anticipation['gamma_proxy_atm2_of_atm5']} of ATM±5)"
            )
        if anticipation.get("sector_available"):
            if anticipation.get("sector_note"):
                advisory.append("sectoral: " + anticipation["sector_note"])
            if anticipation.get("sector_lead_name"):
                advisory.append(
                    f"sector leader: {anticipation['sector_lead_name']} "
                    f"({anticipation['sector_lead_pchange']:+.2f}%)"
                )

    # ---- FIX-9f / V14: 50-pt feasibility vs observed volatility and time left ----
    # V14 removes the look-ahead bias: the old rule hard-blocked a late-session
    # breakout purely because the PRECEDING windows were quiet, even when the
    # current move was genuinely expanding. The p95 gate now only blocks when the
    # tape is NOT already expanding (recent range <= prior range, i.e. no fresh
    # range expansion). A genuine fresh expansion proves the move is real and
    # must not be denied on stale historical p95.
    required_rate = None
    _recent_expansion = False
    if len(spot_hist) >= 2 * RANGE_EXPANSION_BARS:
        _recent = spot_hist[-RANGE_EXPANSION_BARS:]
        _prior = spot_hist[-2*RANGE_EXPANSION_BARS:-RANGE_EXPANSION_BARS]
        _rng_recent = max(_recent) - min(_recent)
        _rng_prior = max(_prior) - min(_prior)
        if _rng_prior > 0:
            _recent_expansion = _rng_recent >= RANGE_EXPANSION_RATIO * _rng_prior
    if minutes_to_close and minutes_to_close > 0:
        required_rate = round(50.0 / minutes_to_close, 2)
        # Directional feasibility: compare the 50-pt requirement against the
        # p95 of |net move| over windows that MATCH the time remaining (a
        # 300-minute horizon must be tested on ~60-minute windows, not 10-min).
        available_windows = [w for w in (10, 15, 20, 30, 45, 60) if w <= minutes_to_close]
        window = max(available_windows) if available_windows else 10
        p95_net = (tail.get(f"abs_net_move_{window}m") or {}).get("p95")
        # V14: if the tape is already expanding, the p95 gate must NOT block —
        # the expansion is direct evidence of a real move that stale historical
        # windows would wrongly deny. Only a non-expanding (still-quiet) tape is
        # eligible for the historical-volatility block.
        if p95_net is not None and not _recent_expansion:
            if minutes_to_close is not None and minutes_to_close < 60 and 50.0 > p95_net:
                blocking.append(f"50-pt target exceeds session p95 directional move of {p95_net} pts over {window}-min windows with {minutes_to_close} min left; observed volatility provides insufficient evidence for a 50-point move (and no fresh range expansion)")
            elif 50.0 > 3 * p95_net:
                blocking.append(f"50-pt target exceeds 3x session p95 directional move ({p95_net} pts over {window}-min windows); volatility gap extreme")
            elif 50.0 > p95_net:
                advisory.append(f"50-pt target above session p95 directional move ({p95_net} pts over {window}-min windows) but {minutes_to_close} min remain; feasibility reduced, not blocked")

    if direction:
        strike = int(atm)
        r = candidates.get(strike)
        side = "ce" if direction == "CALL" else "pe"
        if r is None:
            # AUDIT-C1: a directional signal with no option candidate must
            # FAIL CLOSED, not skip the geometry block silently.
            blocking.append(f"no option-chain row for ATM strike {strike}")
            option, geometry = None, None
        else:
            q = _quote(r, side); entry = q["ask"]
            # AUDIT-M4: contract selection policy — ATM leg must have minimum
            # OI and display depth, else it is not executable.
            oi_sel = _f(r.get(f"{side}_oi"))
            bid_qty = _f(r.get(f"{side}_bid_qty"))
            ask_qty = _f(r.get(f"{side}_ask_qty"))
            if oi_sel is None or oi_sel < MIN_OPTION_OI:
                blocking.append(f"ATM {side.upper()} OI {oi_sel} below minimum {MIN_OPTION_OI}")
            if bid_qty is None or ask_qty is None or bid_qty < MIN_QUOTE_QTY or ask_qty < MIN_QUOTE_QTY:
                blocking.append(f"ATM {side.upper()} quote depth below minimum {MIN_QUOTE_QTY} (bid {bid_qty}/ask {ask_qty})")
            # AUDIT-F2: stop falls back to PROVISIONAL rolling levels when no
            # windowed pivot has confirmed (monotonic trends confirm almost
            # none). stop_source marks the fallback explicitly.
            # AUDIT-S1: fresh 30-obs extremes usually have NO structural stop
            # inside the 25-pt cap (min of last 10 bars is far away after a
            # real impulse) -> MODE-A self-blocked in exactly the trends it
            # targets. Stop priority: confirmed pivot -> provisional rolling
            # -> ATR-proxy (clamp(3x ATR, 10, 25)), each flagged by source.
            atr_proxy = (enhanced_features_pre or {}).get("atr_proxy_points")
            atr_dist = None
            if ATR_STOP_FALLBACK_ENABLED and atr_proxy is not None and atr_proxy > 0:
                atr_dist = min(max(MODE_A_ATR_STOP_MULT * atr_proxy, MIN_STRUCTURAL_STOP_POINTS), MAX_UNDERLYING_RISK)
            if direction == "CALL":
                if floor is not None and spot is not None and 0 < (spot - floor) <= MAX_UNDERLYING_RISK:
                    stop, stop_source = floor, "confirmed"
                elif levels.get("provisional_floor_10") is not None and spot is not None and 0 < (spot - levels["provisional_floor_10"]) <= MAX_UNDERLYING_RISK:
                    stop, stop_source = levels["provisional_floor_10"], "provisional"
                elif atr_dist is not None and spot is not None:
                    stop, stop_source = round(spot - atr_dist, 2), "atr_proxy_3x"
                else:
                    stop, stop_source = None, None
            else:
                if ceil is not None and spot is not None and 0 < (ceil - spot) <= MAX_UNDERLYING_RISK:
                    stop, stop_source = ceil, "confirmed"
                elif levels.get("provisional_ceiling_10") is not None and spot is not None and 0 < (levels["provisional_ceiling_10"] - spot) <= MAX_UNDERLYING_RISK:
                    stop, stop_source = levels["provisional_ceiling_10"], "provisional"
                elif atr_dist is not None and spot is not None:
                    stop, stop_source = round(spot + atr_dist, 2), "atr_proxy_3x"
                else:
                    stop, stop_source = None, None
            stop_usable = levels.get("floor_stop_usable") if direction == "CALL" else levels.get("ceiling_stop_usable")
            # V10.3: volatility-adaptive MODE-A target, R:R-PRESERVING.
            # A fixed 50-pt target is a >p90 tail event on low-vol sessions, but
            # shrinking it must NEVER break the reward/risk gate: the adaptive
            # target is floored at max(MODE_A_T2_MIN, 2*risk) so R:R stays >= 2.0,
            # and capped at MIN_TARGET_2 (50). Falls back to 50 when volatility
            # can't be measured. (v10.2 regressed the 2026-08-24 11:01 winner by
            # shrinking T2 to ~30 and blocking it on R:R=1.37; this fixes that.)
            _p95_20 = (tail.get("abs_net_move_20m") or {}).get("p95")
            risk = abs(spot - stop) if stop is not None and spot is not None else None
            adaptive_t2 = MIN_TARGET_2
            if _p95_20 is not None and _p95_20 > 0:
                _rr_floor = 2.0 * risk if risk else MODE_A_T2_MIN
                adaptive_t2 = min(MIN_TARGET_2, max(MODE_A_T2_MIN, _p95_20, _rr_floor))
            target2 = (spot + adaptive_t2 if direction == "CALL" else spot - adaptive_t2) if spot is not None else None
            rr = (target2 - spot) / risk if direction == "CALL" and risk else (spot - target2) / risk if risk else None
            passes = (q["executable"] and risk is not None
                      and MIN_STRUCTURAL_STOP_POINTS <= risk <= MAX_UNDERLYING_RISK
                      and rr is not None and rr >= MIN_REWARD_RISK)
            if risk is not None and risk < MIN_STRUCTURAL_STOP_POINTS:
                blocking.append(f"stop distance {round(risk, 2)} pts is below structural minimum {MIN_STRUCTURAL_STOP_POINTS} pts (noise level)")
            if risk is not None and risk > MAX_UNDERLYING_RISK:
                blocking.append(f"stop distance {round(risk, 2)} pts exceeds the {MAX_UNDERLYING_RISK} pts cap")
            if q["executable"] is False:
                blocking.append("candidate option quotes are not executable (spread > 0.5% or missing bid/ask)")
            if rr is not None and rr < MIN_REWARD_RISK:
                blocking.append(f"target-2 reward/risk {round(rr, 2)} is below the {MIN_REWARD_RISK} minimum")
            target1 = (spot - MODE_A_T1_POINTS if direction == "PUT"
                       else spot + MODE_A_T1_POINTS) if spot is not None else None
            geometry = {"entry_underlying": spot, "stop": stop, "target_1": target1,
                        "target_2": target2,
                        "risk_points": risk, "reward_risk": rr, "stop_usable": bool(stop_usable),
                        "stop_source": stop_source,
                        "passes_hard_filters": bool(passes)}
            option = {"strike": strike, "side": side, "entry_ask": entry, "exit_bid": q["bid"],
                      "quote": q, "quote_snapshot_id": latest_sid_used}
            # V16: attach live Black-76 Greeks to the chosen candidate and flag IV
            # crush risk. Real-time delta/gamma/vega/theta replace the backward
            # elasticity for sizing/stop insight; high gamma + high entry IV warns
            # that the premium can collapse (IV crush) the moment the level breaks.
            _gk = greeks.get((int(strike), side))
            if _gk:
                option["greeks"] = _gk
                iv_crush_risk = (_gk.get("gamma", 0) * spot * 1.0 > 0.5) if spot else False
                if iv_crush_risk:
                    advisory.append(
                        f"IV-crush risk: {side.upper()} {int(strike)} gamma "
                        f"{_gk['gamma']:.5f}, vega {_gk['vega']:.2f}, theta "
                        f"{_gk['theta']:.3f}/yr — premium can collapse on a break; "
                        "size to live delta, not trailing premium elasticity")

    # AUDIT-C1: fail closed — a directional signal must NEVER survive with
    # missing candidate, missing geometry, or failed hard filters.
    if direction and option is None:
        blocking.append("no executable option candidate for the directional signal")
    if direction and geometry is None:
        blocking.append("risk geometry unavailable for the directional signal")
    if direction and geometry is not None and not geometry.get("passes_hard_filters"):
        blocking.append("risk geometry failed hard filters")

    # ---- V2.2 MODE-B: separate capture profile (initial break qualifies) ----
    # AUDIT-B4 + C3: MODE-B uses the SAME integrity gate as MODE-A
    # (shared_integrity_ok) plus the session-relative age window.
    gap_ok = (shared_integrity_ok
              and decision_source_age is not None
              and -60 <= decision_source_age <= age_gate)
    # AUDIT-F1b: with breached-level triggers, `crossed` is true almost
    # whenever a trigger level exists. MODE-B is an INITIAL-BREAK profile, so
    # it requires the cross to be FRESH (within the last 2 observations).
    # M2-FIX: None must never read as a pass. A fresh 30-observation extreme
    # is REQUIRED for MODE-B, exactly as it is for MODE-A.
    mode_b_crossed = ("CALL" if bull_break["fresh"] and new_high_30 is True
                       else "PUT" if bear_break["fresh"] and new_low_30 is True
                       else None)
    mode_b_side = "ce" if mode_b_crossed == "CALL" else "pe" if mode_b_crossed == "PUT" else None
    mode_b_quote = None
    if mode_b_side and int(atm) in candidates:
        mode_b_quote = _quote(candidates[int(atm)], mode_b_side)
    day_high = _f(latest_chain[0].get("nifty_day_high")) if latest_chain else None
    day_low = _f(latest_chain[0].get("nifty_day_low")) if latest_chain else None
    mode_b = _mode_b_candidate(spot, atm, levels, mode_b_quote, mode_b_crossed, gap_ok,
                               bool(mode_b_quote and mode_b_quote["executable"]),
                               day_high=day_high, day_low=day_low,
                               fresh_high=new_high_30, fresh_low=new_low_30)
    # V9: apply the same session-direction / crowding suppressors to MODE-B.
    # Without this, a suppressed MODE-A long (inverted-skew / counter-trend)
    # could still be re-opened by MODE-B's tighter-stop profile.
    if mode_b.get("decision", "").startswith("BUY"):
        _mb_dir = mode_b.get("direction")
        _mb_block = []
        if (_mb_dir == "CALL" and ob.get("available") and ob.get("skew_inverted")
                and BLOCK_FRESH_LONG_ON_INVERTED_SKEW):
            _mb_block.append("MODE-B CALL blocked: inverted IV skew = crowded-long regime; fresh long suppressed")
        if (BLOCK_AGAINST_SESSION_BIAS and _mb_dir == "CALL"
                and session_bias == "down" and new_high_30 is not True):
            _mb_block.append("MODE-B CALL blocked: session bias down and not a fresh 30-obs high (counter-trend long)")
        if (BLOCK_AGAINST_SESSION_BIAS and _mb_dir == "PUT"
                and session_bias == "up" and new_low_30 is not True):
            _mb_block.append("MODE-B PUT blocked: session bias up and not a fresh 30-obs low (counter-trend put)")
        # ---- V11: trend-regime gate for MODE-B ----
        if REQUIRE_TREND_ALIGNMENT and trend_regime != "range":
            if _mb_dir == "CALL" and trend_regime == "down" and new_high_30 is not True:
                _mb_block.append(f"MODE-B CALL blocked by trend-regime gate (regime DOWN)")
            if _mb_dir == "PUT" and trend_regime == "up" and new_low_30 is not True:
                _mb_block.append(f"MODE-B PUT blocked by trend-regime gate (regime UP)")
        # ---- V18: intraday ENTRY WINDOW for MODE-B (same data-supported window
        # as MODE-A). A late-session MODE-B breakout is subject to the same
        # mature-trend / insufficient-time failure mode and lost on every
        # validation day it fired (e.g. 08-21 12:05 MODE-B PUT at ~170 min). ----
        if (ENTRY_WINDOW_MIN_MINUTES is not None
                and minutes_since_open is not None
                and minutes_since_open < ENTRY_WINDOW_MIN_MINUTES):
            _mb_block.append(
                f"MODE-B {_mb_dir} blocked: inside {ENTRY_WINDOW_MIN_MINUTES:.0f}-min "
                f"opening-chop window (session {minutes_since_open:.0f} min since open)")
        if (ENTRY_WINDOW_MAX_MINUTES is not None
                and minutes_since_open is not None
                and minutes_since_open > ENTRY_WINDOW_MAX_MINUTES):
            _mb_block.append(
                f"MODE-B {_mb_dir} blocked: session {minutes_since_open:.0f} min since open "
                f"exceeds {ENTRY_WINDOW_MAX_MINUTES:.0f}-min intraday entry window")
        # ---- V10/V13/V15: 0-DTE gate (MODE-B). Fitted caps removed (see above).
        # V15: block by default; when opted-in, allow 0-DTE only on index-geometry
        # momentum (with-trend + fresh-extreme). ----
        if _zero_dte and _mb_dir:
            if not _zdt_allow:
                _mb_block.append("MODE-B 0-DTE suppressed (set ALLOW_ZERO_DTE_TREND_EXTREME=1 to trade trend-gated)")
            elif trend_regime == "range" or (_mb_dir == "CALL" and not new_high_30) or (_mb_dir == "PUT" and not new_low_30):
                _mb_block.append("MODE-B 0-DTE: no confirmed trend-extreme (index geometry) - stand aside")
        if _mb_block:
            mode_b["decision"] = "WAIT / NO TRADE"
            mode_b["direction"] = None
            mode_b.setdefault("blocking", []).extend(_mb_block)
    # AUDIT-F1b: MODE-B previously had NO cooldown (observed 2026-08-14:
    # three BUY PUTs in 3 min at 14:18/14:20/14:21; 2026-08-12: two BUY CALLs
    # 64s apart). Same-direction cooldown now applies to MODE-B too, sharing
    # the signal-state file with MODE-A.
    if mode_b.get("decision", "").startswith("BUY"):
        prev = _last_signal_state(output_dir, date_label)
        prev_dec = prev.get("decision")
        prev_ts = parse_nse_timestamp(prev.get("analysis_time_ist"))
        if prev_dec and prev_ts and prev_dec.startswith("BUY"):
            elapsed = (now_ist() - prev_ts).total_seconds()
            if prev_dec == mode_b["decision"] and elapsed < MODE_A_COOLDOWN_MINUTES * 60:
                mode_b["decision"] = "WAIT / NO TRADE"
                mode_b["direction"] = None
                mode_b.setdefault("blocking", []).append(
                    f"re-entry cooldown: previous {prev_dec} {round(elapsed / 60.0, 1)} min ago")
            elif elapsed < GLOBAL_SIGNAL_COOLDOWN_MINUTES * 60:
                mode_b["decision"] = "WAIT / NO TRADE"
                mode_b["direction"] = None
                mode_b.setdefault("blocking", []).append(
                    f"global cooldown: previous {prev_dec} {round(elapsed / 60.0, 1)} min ago")
        if _signals_today(output_dir, date_label, "MODE-B") >= MODE_B_MAX_SIGNALS_PER_DAY:
            mode_b["decision"] = "WAIT / NO TRADE"
            mode_b["direction"] = None
            mode_b.setdefault("blocking", []).append(
                f"MODE-B daily signal cap reached ({MODE_B_MAX_SIGNALS_PER_DAY})")
    # AUDIT-C4: no side effects here — the arbitration layer below commits at
    # most ONE signal per packet.

    # AUDIT-C6/S2: compute cross/confirmation ages BEFORE the decision is
    # finalized — a stale cross must be able to BLOCK, and the grade must be
    # able to relabel the decision.
    trigger_age_seconds = None
    cross_age_seconds = None
    trigger_break = bull_break if direction == "CALL" else bear_break if direction == "PUT" else None
    if trigger_break:
        conf_t = parse_nse_timestamp(trigger_break.get("confirmation_timestamp"))
        cross_t = parse_nse_timestamp(trigger_break.get("cross_timestamp"))
        if conf_t:
            trigger_age_seconds = round((now_ist() - conf_t).total_seconds(), 1)
        if cross_t:
            cross_age_seconds = round((now_ist() - cross_t).total_seconds(), 1)
    if cross_age_seconds is not None and cross_age_seconds > MAX_CROSS_AGE_SECONDS:
        blocking.append(f"stale breakout: cross {round(cross_age_seconds / 60.0, 1)} min ago exceeds {round(MAX_CROSS_AGE_SECONDS / 60.0, 0)} min cap")

    # ---- V12: RANGE-day short-strangle (premium selling). When the intraday
    # regime is RANGE (no confirmed trend) and not 0-DTE, the profitable action
    # is SELL OTM premium (collect theta+spread) rather than force a losing long.
    # This is the complementary edge to with-trend longs on trend days.
    # NOTE: computed BEFORE the decision block so the SELL STRANGLE decision can
    # fire (previously it was computed after `decision`, so it never triggered).
    range_signal = None
    if (RANGE_SELL_PREMIUM and trend_regime == "range" and not _zero_dte
            and shared_integrity_ok):
        range_signal = _range_strangle_candidate(candidates, atm, spot)
        if range_signal:
            blocking.append(
                f"RANGE regime (EMA8~EMA21, no trend): SELLING OTM premium "
                f"(short strangle {range_signal['put_short']}/{range_signal['call_short']} "
                f"iron-condor, credit {range_signal['total_credit']}) instead of a long")

    decision = ("BUY CALL NOW" if direction == "CALL"
                else "BUY PUT NOW" if direction == "PUT"
                else "WAIT / NO TRADE")
    # V12: a confirmed range-regime short-strangle overrides WAIT on range days
    # (it is the profitable action there). It must have fresh/executable quotes
    # and no conflicting directional signal already set.
    if (range_signal and decision == "WAIT / NO TRADE"
            and not (mode_b.get("decision") or "").startswith("BUY")
            and not direction):
        decision = "SELL STRANGLE NOW"
        signal_mode, signal_label, signal_execution = "RANGE_PREMIUM", "SELL STRANGLE NOW", True
    if blocking and decision.startswith("SELL"):
        decision = "WAIT / NO TRADE"
    elif blocking:
        decision = "WAIT / NO TRADE"
    # H8: the execution ceiling is ABSOLUTE (120 s), never session-relative.
    decision_grade = "execution_candidate" if (decision.startswith("BUY")
                                               and trigger_age_seconds is not None
                                               and trigger_age_seconds <= EXECUTION_AGE_CEILING_SECONDS
                                               and decision_source_age is not None
                                               and 0 <= decision_source_age <= EXECUTION_AGE_CEILING_SECONDS
                                               and chain_gap_seconds <= EXECUTION_AGE_CEILING_SECONDS) else "journal"
    if decision.startswith("BUY") and decision_grade != "execution_candidate":
        # AUDIT-H1: a stale/feed-lagged signal must not carry the BUY NOW
        # label. Journal-grade detections are relabelled.
        side_label = "CALL" if "CALL" in decision else "PUT"
        decision = f"HISTORICAL SETUP DETECTED ({side_label})"
    # AUDIT-C4 + H1/M9-FIX: single arbitration commit. MODE-A has priority;
    # exactly one signal (mode, decision, ledger row, state write) per packet.
    # Journal-grade detections (relabelled HISTORICAL SETUP DETECTED) are
    # ledgered so near-miss signals stay auditable, but they do NOT update
    # last-signal state (no execution cooldown).
    if decision.startswith("SELL STRANGLE"):
        signal_mode, signal_label, signal_execution = "RANGE_PREMIUM", "SELL STRANGLE NOW", True
    elif decision.startswith("BUY") or decision.startswith("HISTORICAL SETUP DETECTED"):
        signal_mode, signal_label, signal_execution = "MODE-A", decision, decision.startswith("BUY")
    elif mode_b.get("decision", "").startswith("BUY") or mode_b.get("decision", "").startswith("HISTORICAL"):
        signal_mode, signal_label, signal_execution = "MODE-B", mode_b["decision"], mode_b["decision"].startswith("BUY")
    else:
        signal_mode, signal_label, signal_execution = None, None, False
    if ob.get("days_to_expiry") == 0:
        advisory.append("expiry-day regime: premium-based stops are unreliable near settlement; treat geometry as journal-grade")
    reasons = [("BLOCKING: " + x) for x in blocking] + [("ADVISORY: " + x) for x in advisory]
    if signal_mode:
        _commit_signal_records(output_dir, date_label, signal_mode, signal_label, reasons, signal_execution)
    # LLM escalation level (event-driven scheduling; see _append_llm_escalation).
    llm_escalation = "none"
    if decision.startswith("BUY"):
        llm_escalation = "execute"
    elif mode_b.get("decision", "").startswith("BUY"):
        # a MODE-B BUY is also an executable signal (its own ~20-pt budget)
        llm_escalation = "execute"
    elif decision.startswith("HISTORICAL SETUP DETECTED"):
        llm_escalation = "review"
    elif (bull_break.get("crossed") or bear_break.get("crossed")) and not (
            bull_break.get("confirmed") or bear_break.get("confirmed")):
        llm_escalation = "armed"
    elif flow.get("two_observation_bullish") or flow.get("two_observation_bearish"):
        llm_escalation = "armed"
    elif anticipation.get("armed"):
        # anticipation tension (implied-forward divergence / IV bid / skew
        # velocity) raises the alert level so context is pre-loaded before a
        # possible break - never an execution trigger.
        llm_escalation = "armed"
    packet = {"analytics_schema": ANALYTICS_SCHEMA, "decision": decision,
              "analysis_time_ist": iso_or_none(now_ist()),
              "validation": validation, "latest": latest, "latest_snapshot_id_used": latest_sid_used,
              "trigger_age_seconds": trigger_age_seconds,
              "cross_age_seconds": cross_age_seconds,
              "decision_grade": decision_grade,
              "llm_escalation": llm_escalation,
              "feed_note": "Broker market feeds lag real time; BUY decisions are journal-grade unless trigger_age_seconds <= 120 and the chain gap is within the age gate",
              "chain_gap_seconds": chain_gap_seconds,
              "chain_gap_source_seconds": chain_gap_source_seconds,
              "pipeline_lag_seconds": pipeline_lag_seconds,
              "decision_source_age_seconds": decision_source_age,
              "minutes_to_close": minutes_to_close,
              "minutes_since_open": minutes_since_open,
              "required_rate_pts_per_min": required_rate,
              "price_structure": price, "swing_levels": levels, "atm_flow": flow,
              "tail_stats": tail, "microstructure": microstructure,
              "premium_flow": premium_flow, "coil": coil, "options_behaviour": ob,
              "anticipation": anticipation,
              "mode_b": mode_b,
              "range_signal": range_signal,
              "order_flow": order_flow,
              "flow_quality": flow_quality,
              "breakout_state": {"bullish_trigger": bullish_trigger, "bearish_trigger": bearish_trigger,
                                 "pending_bullish": pending_bull, "pending_bearish": pending_bear,
                                 "bullish": bull_break, "bearish": bear_break,
                                 "two_observation_confirmed": bool(direction)},
              "option_candidate": option, "risk_geometry": geometry, "reasons": reasons,
              "limitations": {"futures_available": latest.get("true_futures_context_available") in (True, "True"),
                              "scheduled_event_data_available": bool(SCHEDULED_EVENT),
                              "premium_geometry_modeled": False,
                              "premium_geometry_note": "Stops/targets are in underlying points; option P&L, theta, and costs are not modelled here"}}
    packet["enhanced_features"] = enhanced_features_pre
    packet["directional_evidence_score"] = _directional_score(packet)
    packet["report_version"] = "3.2.0"
    try:
        _append_llm_escalation(output_dir, date_label, packet)
    except Exception:
        logging.exception("LLM escalation queue append failed")
    try:
        _maybe_gemini_review(output_dir, date_label, packet, llm_escalation)
    except Exception:
        logging.exception("Gemini review scheduling failed")
    try:
        _paper_open_trade(output_dir, date_label, packet)
    except Exception:
        logging.exception("Paper trade open failed")
    atomic_json_write(output_dir / f"nifty-llm-analysis-v2-{date_label}.json", packet)
    atomic_json_write(output_dir / f"nifty-trader-report-v2-{date_label}.json", {
        "decision": packet["decision"], "snapshot": latest, "market": packet["price_structure"],
        "levels": packet["swing_levels"], "enhanced_features": packet["enhanced_features"],
        "directional_evidence_score": packet["directional_evidence_score"],
        "risk_geometry": packet["risk_geometry"], "option_candidate": packet["option_candidate"],
        "reasons": packet["reasons"], "limitations": packet["limitations"],
        "tail_stats": packet["tail_stats"], "chain_gap_seconds": packet["chain_gap_seconds"],
        "microstructure": packet["microstructure"], "premium_flow": packet["premium_flow"],
        "coil": packet["coil"], "options_behaviour": packet["options_behaviour"],
        "mode_b": packet["mode_b"]})
    return packet


# =============================================================================
# LOOP AND CLI
# =============================================================================


def sleep_to_next_capture_second() -> None:
    now = time.time()
    current_minute = math.floor(now / 60) * 60
    target = current_minute + CAPTURE_SECOND
    if target <= now:
        target += 60
    time.sleep(max(0.2, target - now))


def sleep_with_ticks(collector: "UpstoxCollector") -> None:
    """FIX-10: poll allIndices at SPOT_TICK_SECONDS offsets between chain
    captures so a true 1-min spot high/low is available even without a
    futures feed. Ticks go to nifty-spot-ticks-v2-YYYY-MM-DD.csv and feed
    spot_high_1m / spot_low_1m in the next context row."""
    while True:
        now = time.time()
        current_minute = math.floor(now / 60) * 60
        next_actions = [current_minute + CAPTURE_SECOND] + [current_minute + s for s in SPOT_TICK_SECONDS]
        future = [a for a in next_actions if a > now + 0.2]
        target = min(future) if future else current_minute + 60 + CAPTURE_SECOND
        time.sleep(max(0.2, target - now))
        if target in [current_minute + s for s in SPOT_TICK_SECONDS]:
            try:
                collector.tick_once()
            except Exception:
                logging.exception("Spot tick capture failed")
            continue
        return


def _pid_alive(pid: int) -> bool:
    """AUDIT-B1: on Windows, os.kill(pid, 0) TERMINATES the target process
    (any signal other than CTRL_* calls TerminateProcess) — a second
    accidental start would kill the live collector. Probe via OpenProcess
    on win32; POSIX keeps os.kill(pid, 0). Unknown state fails CLOSED."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True  # cannot determine -> treat as alive (fail closed)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return True


def acquire_instance_lock(output_dir: Path) -> Optional[Path]:
    """Prevent two collector processes from interleaving rows into the same
    CSVs and racing on collector_state.json. Returns the lock path on
    success, None if another live instance holds it.

    AUDIT-F5: made ATOMIC with O_CREAT|O_EXCL (the previous exists()->read()
    ->write_text() sequence was TOCTOU-racy: two simultaneous starts could
    both acquire). Stale locks from dead PIDs are detected and retried once."""
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = output_dir / "collector.lock"
    for attempt in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            finally:
                os.close(fd)
            return lock
        except FileExistsError:
            try:
                other_pid = int(lock.read_text(encoding="utf-8").strip() or "0")
            except (ValueError, OSError):
                other_pid = 0
            alive = False
            if other_pid > 0:
                alive = _pid_alive(other_pid)
            if alive:
                logging.error("Another collector instance (pid %s) holds %s; exiting", other_pid, lock)
                return None
            # stale lock from a dead process: remove and retry once
            try:
                lock.unlink()
            except OSError:
                return None
    logging.error("Could not acquire instance lock %s", lock)
    return None


def release_instance_lock(lock: Optional[Path]) -> None:
    if lock is None:
        return
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        pass


def configure_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "collector.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")],
    )


def run_selftest() -> int:
    """V2.7: offline regression suite (no network, no playwright). Runs
    tests/run_regression_tests.py which stubs the NSE client and exercises
    the decision packet builder against synthetic and real fixtures."""
    candidates = [
        Path(__file__).resolve().parent / "tests" / "run_regression_tests.py",
        Path.cwd() / "tests" / "run_regression_tests.py",
    ]
    test_file = next((p for p in candidates if p.exists()), None)
    if test_file is None:
        print("SELFTEST: tests/run_regression_tests.py not found next to the "
              "collector script or in the working directory. Ship the suite "
              "alongside the script; it is the regression safety net for the "
              "decision engine.")
        return 2
    os.environ["NIFTY_COLLECTOR_SCRIPT"] = str(Path(__file__).resolve())
    print(f"SELFTEST: running offline regression suite ({test_file})")
    try:
        spec = importlib.util.spec_from_file_location("collector_selftest", str(test_file))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        code = mod.main()
        return int(code or 0)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print("SELFTEST crashed:", exc)
        return 1


def _offline_packet(output_dir: Path, date: str):
    """V2.7: rebuild the decision packet from an already-collected day
    WITHOUT touching production outputs. Files are copied to a temp dir so
    the analysis JSONs land there and production files stay untouched.
    Returns the packet or None."""
    import shutil as _sh
    import tempfile as _tf
    names = ("nifty-option-chain", "nifty-market-context", "nifty-snapshot-summary")
    # Per-day layout first (nifty_data/<date>/), fall back to flat layout.
    day = _session_dir(output_dir, date)
    src_dir = day if (day / f"nifty-option-chain-v2-{date}.csv").exists() else output_dir
    paths = [src_dir / f"{name}-v2-{date}.csv" for name in names]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print("OFFLINE-PACKET: missing file(s):", ", ".join(missing))
        return None

    def sids(p):
        with open(p, newline="", encoding="utf-8") as f:
            return {r.get("snapshot_id") for r in csv.DictReader(f)}

    common = sids(paths[0]) & sids(paths[1]) & sids(paths[2])
    if not common:
        print("OFFLINE-PACKET: no common synchronized snapshots for", date)
        return None
    latest = max(common)
    work = Path(_tf.mkdtemp(prefix="nifty_offline_"))
    for name in names:
        _sh.copy(src_dir / f"{name}-v2-{date}.csv", work / f"{name}-v2-{date}.csv")
    # AUDIT-B3: replay fidelity — copy the tick feed and signal state so
    # tick-confirmations and cooldowns behave as they do live, and set a
    # REPLAY CLOCK so pipeline_lag is measured against the data's own time
    # instead of wall-clock (which produced bogus multi-hour lag blocks).
    ticks_src = src_dir / f"nifty-spot-ticks-v2-{date}.csv"
    if ticks_src.exists():
        _sh.copy(ticks_src, work / f"nifty-spot-ticks-v2-{date}.csv")
    sector_src = src_dir / f"nifty-sector-context-v2-{date}.csv"
    if sector_src.exists():
        _sh.copy(sector_src, work / f"nifty-sector-context-v2-{date}.csv")
    # AUDIT-H4: do NOT copy end-state last_signal_state.json or the end-of-day
    # ledger — that is future-state contamination for a single-shot packet
    # (cooldowns/caps would see decisions made later in the day). The offline
    # run starts with clean cooldown state; the true walk-forward runner is a
    # separate tool (tools/run_day_audit.py).
    # replay clock: newest context capture + 45 s
    replay_now = None
    try:
        with open(work / f"nifty-market-context-v2-{date}.csv", newline="", encoding="utf-8") as f:
            cap_ts = [parse_nse_timestamp(r.get("capture_timestamp"))
                      for r in csv.DictReader(f) if r.get("capture_timestamp")]
        cap_ts = [t for t in cap_ts if t]
        if cap_ts:
            replay_now = max(cap_ts) + dt.timedelta(seconds=45)
    except Exception:
        replay_now = None
    global now_ist, GEMINI_SUSPENDED, PAPER_SUSPENDED
    orig_now_ist = now_ist
    orig_gemini_suspended = GEMINI_SUSPENDED
    orig_paper_suspended = PAPER_SUSPENDED
    if replay_now is not None:
        now_ist = lambda: replay_now
    GEMINI_SUSPENDED = True   # offline replay must never hit the network
    PAPER_SUSPENDED = True    # offline replay must never open/close paper trades
    try:
        return build_llm_packet(work, date, latest)
    except Exception as exc:
        print("OFFLINE-PACKET failed:", exc)
        return None
    finally:
        now_ist = orig_now_ist
        GEMINI_SUSPENDED = orig_gemini_suspended
        PAPER_SUSPENDED = orig_paper_suspended


def _print_packet(packet, date: str) -> None:
    latest = packet.get("latest") or {}
    print(f"=== OFFLINE DECISION RUN {date} @ {latest.get('source_timestamp')} "
          f"(snapshot {packet.get('latest_snapshot_id_used')}) ===")
    print("decision:", packet.get("decision"))
    print("spot:", latest.get("spot"),
          "| chain_gap_s:", packet.get("chain_gap_seconds"),
          "| minutes_to_close:", packet.get("minutes_to_close"),
          "| pipeline_lag_s:", packet.get("pipeline_lag_seconds"))
    ant = packet.get("anticipation") or {}
    if ant.get("available"):
        print("anticipation: implied_forward=", ant.get("implied_forward"),
              "| divergence=", ant.get("implied_forward_divergence"),
              "| iv_bid=", ant.get("iv_bid"),
              "| skew_v=", ant.get("skew_velocity_3obs"),
              "| gamma=", ant.get("gamma_regime"),
              "| armed=", ant.get("armed"))
    for r in packet.get("reasons") or []:
        print("  ", r)


def run_analyze_once(output_dir: Path, date: str) -> int:
    """V2.7: offline decision-engine test against collected files for DATE.
    No network. Looks in --output-dir, then nifty_data/, then the cwd."""
    candidates = [_session_dir(output_dir, date), output_dir, Path("nifty_data"), Path(".")]
    for d in candidates:
        if (d / f"nifty-option-chain-v2-{date}.csv").exists():
            output_dir = d
            break
    packet = _offline_packet(output_dir, date)
    if packet is None:
        return 2
    _print_packet(packet, date)
    return 0


def _print_after_capture(output_dir: Path) -> None:
    """V2.7: after a --once --test capture, rebuild and print the decision
    packet from the freshly written files so the whole pipeline is visible."""
    dates = sorted({
        p.parent.name
        for p in output_dir.glob("*/nifty-option-chain-v2-*.csv")
    } | {
        p.stem.split("-v2-")[-1]
        for p in output_dir.glob("nifty-option-chain-v2-*.csv")
        if p.stem.startswith("nifty-option-chain-v2-")
    })
    if not dates:
        print("TEST MODE: no capture files found; skipping decision packet")
        return
    packet = _offline_packet(output_dir, dates[-1])
    if packet is not None:
        _print_packet(packet, dates[-1])


# =============================================================================
# UPSTOX DATA SOURCE (alternative to NSE scraping)
# =============================================================================
# Uses the authenticated Upstox Developer API (no Akamai 403 problem) and maps
# its option-chain + market-quote responses onto the SAME CSV schemas, so the
# entire downstream analytics / decision / Gemini pipeline is unchanged.
#
# =============================================================================
# UPSTOX CREDENTIALS (hardcoded per operator instruction, v5)
# =============================================================================
# WARNING: the access token EXPIRES (typically end of trading session). When
# the collector starts logging 401 errors, regenerate the token in the Upstox
# developer console and paste the new value below. If these credentials leak,
# regenerate all three in the Upstox console immediately.
# V19: these now also honour values from env.txt (see _load_env_file above).
# Real env vars win, then env.txt, then the hardcoded defaults below.
UPSTOX_API_KEY = os.getenv("UPSTOX_API_KEY", "ee2e4f16-0691-42d9-9a35-3f841ec29cbc")
UPSTOX_API_SECRET = os.getenv("UPSTOX_API_SECRET", "d3rr2a0uib")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN",
    "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ."
    "eyJzdWIiOiIzUkNRNFMiLCJqdGkiOiI2YTkxMGViYTg4NDA4OTMzYTY2ZmExY2MiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzg3ODkxMzg2LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3ODc5NTQ0MDB9."
    "Qhasp3-LH6wWDMaBfqZ7XoHSDVKH0MmrJpd11yShQsw")
UPSTOX_BASE_URL = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com")
UPSTOX_TIMEOUT = float(os.getenv("UPSTOX_TIMEOUT", "25"))
UPSTOX_NIFTY_KEY = "NSE_INDEX|Nifty 50"
UPSTOX_VIX_KEY = "NSE_INDEX|India VIX"
UPSTOX_HTTP_PROXY = os.getenv("UPSTOX_HTTP_PROXY", "")  # e.g. http://user:pass@host:port
UPSTOX_USE_CURL = os.getenv("UPSTOX_USE_CURL", "1") in ("1", "true", "True", "yes")
UPSTOX_DEFAULT_LOT_SIZE = int(os.getenv("UPSTOX_LOT_SIZE", "25"))  # NIFTY lot (25 post-Nov-2024)

# --- PAPER TRADING (simulated P&L ledger, no real orders) --------------------
# Every execution-grade BUY signal (MODE-A or MODE-B) opens a simulated trade;
# the trade is then exited at stop / target-2 (target-1 is recorded as a
# milestone) using the SAME per-minute spot+premium data the engine collects.
# Results land in paper_trades-<date>.csv + a rolling paper_summary-<date>.csv.
# Honest caveats: entry=ask, exit=bid (spread included), but NO brokerage, STT,
# stamp duty, slippage or impact cost are modelled; 1-minute bars mean the
# intra-bar order of stop-vs-target is resolved conservatively (stop first).
PAPER_TRADING_ENABLED = os.getenv("PAPER_TRADING_ENABLED", "1") in ("1", "true", "True", "yes")
# V15 (cost drag): the paper ledger previously modelled ZERO costs. For a system
# executing tight 15-pt T1 scales and 10-pt stops, bid/ask spread + STT + slippage
# (~1.5 pts round-trip) consume a meaningful share of gross profit. These are now
# modelled as a per-contract round-trip cost in RUPEES per lot, applied on every
# fill (entry + each exit leg), so net profitability reflects realistic frictions.
# PAPER_COST_PER_LOT_RS: estimated round-trip cost per lot (brokerage + STT +
# stamp + slippage). Default ~₹60/lot (₹25/lot is the STT+brokerage floor; the
# value is configurable). Applied once per filled lot per trade.
PAPER_COST_PER_LOT_RS = float(os.getenv("PAPER_COST_PER_LOT_RS", "60.0"))
PAPER_TRAIL_AFTER_POINTS = float(os.getenv("PAPER_TRAIL_AFTER_POINTS", "15"))
# V9 profitability fixes: scale-out at T1 with the remainder locked at T1.
# PAPER_T1_SCALE_PCT: close this fraction of the position at target-1 (bank a
#   partial profit instead of giving a favourable run back to the stop), then
#   lock the REMAINDER's stop to the T1 level so it banks the T1 gain too while
#   still being able to ride on to target-2. The old code only moved the stop
#   to entry, giving back the whole favourable excursion to ~breakeven.
PAPER_T1_SCALE_PCT = float(os.getenv("PAPER_T1_SCALE_PCT", "50.0"))
# V10.2: trailing-stop capture. On the audited days the winning-direction trades
# had huge favourable excursions (MFE up to 138-177 pts) but the fixed-T2 exit
# banked only ~20-50 pts. After scaling out at T1, trail the REMAINDER's stop
# behind the running favourable extreme by PAPER_TRAIL_DIST pts (a chandelier
# trail) instead of locking it flat at T1, so strong runners keep adding P&L
# while adverse pull-backs still exit at a locked profit. Set to 0 to revert to
# the T1-lock behaviour.
PAPER_TRAIL_DIST = float(os.getenv("PAPER_TRAIL_DIST", "15.0"))
# V9 profitability fixes: suppressors. These stop the engine entering the wrong
# side / the expensive crowded side of the market.
# BLOCK_FRESH_LONG_ON_INVERTED_SKEW: inverted IV skew (call IV > put IV) is a
#   crowded-long regime; buying fresh CALLs there is buying expensive, crowded
#   premium into reversal risk -> suppress fresh CALLs.
# BLOCK_AGAINST_SESSION_BIAS: do not take a fresh long against the session's
#   dominant direction (spot on the wrong side of day open) unless at a fresh
#   30-obs extreme (trend-extension).
BLOCK_FRESH_LONG_ON_INVERTED_SKEW = os.getenv("BLOCK_FRESH_LONG_ON_INVERTED_SKEW", "1") in ("1", "true", "True", "yes")
BLOCK_AGAINST_SESSION_BIAS = os.getenv("BLOCK_AGAINST_SESSION_BIAS", "1") in ("1", "true", "True", "yes")
# V11: TREND-REGIME GATE. The 4-day audit proved the engine's biggest loss
# driver was trading COUNTER to the dominant intraday trend (08-25: shorted into
# a +159 up day; 08-26: bought CALLs into a -134 down day). A no-lookahead
# EMA8/EMA21 + day-open trend rule made money on 3/4 days. When on, a trade is
# only executable when its direction MATCHES the trend regime (or it is a fresh
# 30-obs extreme that could start a new leg). Range regime = no long.
REQUIRE_TREND_ALIGNMENT = os.getenv("REQUIRE_TREND_ALIGNMENT", "1") in ("1", "true", "True", "yes")
# V12: RANGE-DAY PREMIUM SELLING (the complementary edge). The from-scratch
# 4-day test proved two OPPOSITE edges: on TREND days buying options WITH the
# trend wins; on RANGE/choppy days buying options loses to theta+spread drag,
# but SELLING OTM premium (short strangle) COLLECTS theta and profits (08-21
# +190rs, 08-26 +350rs on real chain premiums). When the intraday regime is
# RANGE and not 0-DTE, the engine emits a SHORT STRANGLE (sell OTM put + OTM
# call) instead of forcing a losing long. Distances are in strikes (50-pt).
RANGE_SELL_PREMIUM = os.getenv("RANGE_SELL_PREMIUM", "1") in ("1", "true", "True", "yes")
RANGE_STRANGLE_DIST = int(os.getenv("RANGE_STRANGLE_DIST", "3"))   # strikes from ATM (e.g. 3*50=150)
RANGE_STRANGLE_WING = int(os.getenv("RANGE_STRANGLE_WING", "2"))   # credit-spread wing width (strikes)
RANGE_STRANGLE_T1 = 0.35   # buy back after collecting 35% of credit
RANGE_STRANGLE_STOP = 1.5  # buy back for a 1.5x credit loss (defined risk)
# V10/V13/V15: 0-DTE (expiry-day) handling. On settlement day (0 days to expiry)
# premium-based stops are unreliable near settlement, but the highest-gamma day
# is also the most convex. V15 NO LONGER blanket-blocks 0-DTE: instead it trades
# 0-DTE ONLY via UNDERLYING INDEX GEOMETRY — a confirmed with-trend regime AND a
# fresh 30-obs extreme (the stops/targets are index levels, not premium-based),
# and stands aside on 0-DTE when the tape is range/chop (no confirmable extreme).
# BLOCK_ZERO_DTE: master switch for 0-DTE handling (default ON = block all 0-DTE
# longs). When ON and ALLOW_ZERO_DTE_TREND_EXTREME is also ON, 0-DTE is traded only
# on index-geometry momentum (with-trend regime + fresh 30-obs extreme). Default
# BLOCK_ZERO_DTE=1 / ALLOW_ZERO_DTE_TREND_EXTREME=0 preserves the validated result.
BLOCK_ZERO_DTE = os.getenv("BLOCK_ZERO_DTE", "1") in ("1", "true", "True", "yes")
ALLOW_ZERO_DTE_TREND_EXTREME = os.getenv("ALLOW_ZERO_DTE_TREND_EXTREME", "0") in ("1", "true", "True", "yes")
PAPER_SUSPENDED = False  # set True during offline replay (no state pollution)
# sector index names AS UPSTOX SPELLS THEM (verified against the live API)
UPSTOX_SECTOR_KEYS = [
    ("bank", "Nifty Bank"),
    ("it", "Nifty IT"),
    ("fin_service", "Nifty Fin Service"),
    ("fmcg", "Nifty FMCG"),
    ("auto", "Nifty Auto"),
    ("metal", "Nifty Metal"),
    ("pharma", "Nifty Pharma"),
    ("energy", "Nifty Energy"),
    ("psu_bank", "Nifty PSU Bank"),
    ("realty", "Nifty Realty"),
    ("infra", "Nifty Infra"),
]


def _upstox_norm(key: str) -> str:
    """Normalise Upstox instrument keys for matching. The quotes response keys
    use ':' (e.g. 'NSE_INDEX:Nifty 50') while the request uses '|'; both must
    resolve to the same lookup key."""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


class UpstoxClient:
    """Thin authenticated HTTP client for the Upstox Developer API."""

    def __init__(self, access_token: str = UPSTOX_ACCESS_TOKEN,
                 base_url: str = UPSTOX_BASE_URL) -> None:
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")

    def _proxies(self) -> Dict[str, str]:
        if UPSTOX_HTTP_PROXY:
            return {"http": UPSTOX_HTTP_PROXY, "https": UPSTOX_HTTP_PROXY}
        return {}

    def _curl_get(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
        cmd = ["curl", "-sS", "--compressed", "-m", str(int(UPSTOX_TIMEOUT))]
        if UPSTOX_HTTP_PROXY:
            cmd += ["-x", UPSTOX_HTTP_PROXY]
        for key, value in headers.items():
            cmd += ["-H", f"{key}: {value}"]
        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, timeout=UPSTOX_TIMEOUT + 5)
        stdout = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(
                f"curl exit {proc.returncode}: {proc.stderr.decode(errors='replace')[:200]}")
        if not stdout:
            raise RuntimeError("curl returned an empty body")
        if stdout[0] not in "{[":
            # Cloudflare challenge page / HTML block, not JSON.
            raise RuntimeError(f"curl returned non-JSON (blocked?): {stdout[:120]}")
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise RuntimeError("curl returned non-object JSON")
        return data

    def _get(self, path: str, params: List[Tuple[str, str]]) -> Dict[str, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Authorization": "Bearer " + self.access_token,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
            "sec-ch-ua": '"Chromium";v="125", "Google Chrome";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        # Cloudflare intermittently challenges the API edge; retry the whole
        # transport cascade a few times with a short backoff before giving up.
        for attempt in range(3):
            if attempt:
                time.sleep(1.5 * attempt)
            errors: List[str] = []
            # Transport 1: curl subprocess. Windows 10+ ships curl.exe whose
            # TLS stack is far less likely to be flagged by Cloudflare.
            if UPSTOX_USE_CURL and shutil.which("curl"):
                try:
                    return self._curl_get(url, headers)
                except Exception as exc:
                    errors.append(f"curl: {exc}")
            # Transport 2: requests (urllib3 TLS) when installed.
            try:
                import requests  # noqa: F401
                resp = requests.get(url, headers=headers, timeout=UPSTOX_TIMEOUT,
                                    proxies=self._proxies() or None)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data
                    raise RuntimeError("non-object JSON")
                raise RuntimeError(f"HTTP {resp.status_code}")
            except ImportError:
                pass
            except Exception as exc:
                errors.append(f"requests: {exc}")
            # Transport 3: urllib (last resort - most likely to be flagged).
            try:
                request = urllib.request.Request(url, headers=headers, method="GET")
                if self._proxies():
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler(self._proxies()))
                else:
                    opener = urllib.request.build_opener()
                with opener.open(request, timeout=UPSTOX_TIMEOUT) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data
                raise RuntimeError("non-object JSON")
            except Exception as exc:
                errors.append(f"urllib: {exc}")
        raise RuntimeError(f"Upstox fetch failed for {path} after retries: " + " | ".join(errors))

    def quotes(self, keys: List[str]) -> Dict[str, Any]:
        payload = self._get("/v2/market-quote/quotes", [("instrument_key", k) for k in keys])
        out: Dict[str, Any] = {}
        for k, v in (payload.get("data") or {}).items():
            out[_upstox_norm(k)] = v
        return out

    def ltp(self, keys: List[str]) -> Dict[str, Any]:
        payload = self._get("/v2/market-quote/ltp", [("instrument_key", k) for k in keys])
        out: Dict[str, Any] = {}
        for k, v in (payload.get("data") or {}).items():
            out[_upstox_norm(k)] = v
        return out

    def option_chain(self, instrument_key: str, expiry: str) -> List[Dict[str, Any]]:
        payload = self._get("/v2/option/chain",
                            [("instrument_key", instrument_key), ("expiry_date", expiry)])
        return payload.get("data") or []

    def option_contracts(self, instrument_key: str) -> List[Dict[str, Any]]:
        payload = self._get("/v2/option/contract", [("instrument_key", instrument_key)])
        return payload.get("data") or []


class UpstoxCollector:
    """Collects NIFTY option-chain + market context from Upstox and writes the
    SAME CSVs as the NSE collector (nifty-option-chain-v2, market-context,
    snapshot-summary, sector-context, spot-ticks) so the analytics layer is
    byte-identical."""

    def __init__(self, output_dir: Path, test_mode: bool = False) -> None:
        self.output_dir = output_dir
        self.test_mode = test_mode
        self.client = UpstoxClient()
        self.state_path = output_dir / "upstox_state.json"
        self.state: Dict[str, Any] = self._load_state()
        self._recent_ticks: List[Dict[str, Any]] = []
        self._last_capture: Optional[dt.datetime] = None

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}
        return {}

    def _save_state(self) -> None:
        try:
            atomic_json_write(self.state_path, self.state)
        except Exception:
            pass

    def _prepare_session(self, session_date: dt.date) -> None:
        if self.state.get("session_date") != session_date.isoformat():
            self.state = {
                "session_date": session_date.isoformat(),
                "session_anchor_atm": None,
                "contracts": {},
            }

    @staticmethod
    def _nearest_weekly_expiry(today: dt.date) -> dt.date:
        """NIFTY weekly expiry is every Tuesday (SEBI framework, since Sep 2025)."""
        return today + dt.timedelta(days=(1 - today.weekday()) % 7)

    def _choose_expiry(self, today: dt.date) -> str:
        if self.state.get("expiry_cache_date") == today.isoformat() and self.state.get("expiry"):
            return self.state["expiry"]
        expiry = os.getenv("UPSTOX_EXPIRY", "") or None
        lot = number(os.getenv("UPSTOX_LOT_SIZE", ""), integer=True)
        if expiry is None or lot is None:
            # The definitive endpoint (exact expiry list + lot size). Falls back
            # to the local Tuesday rule when Cloudflare blocks it.
            try:
                contracts = self.client.option_contracts(UPSTOX_NIFTY_KEY)
                if expiry is None:
                    expiries = sorted({c["expiry"] for c in contracts if c.get("expiry")})
                    near = next((e for e in expiries if e >= today.isoformat()), None)
                    if near:
                        expiry = near
                if lot is None:
                    lot = number(next((c.get("lot_size") for c in contracts if c.get("lot_size")), None),
                                 integer=True)
            except Exception as exc:
                logging.warning("Upstox option/contract unavailable (%s); using local expiry fallback", exc)
        if expiry is None:
            expiry = self._nearest_weekly_expiry(today).isoformat()
        if lot is None or lot <= 0:
            lot = UPSTOX_DEFAULT_LOT_SIZE
        self.state["expiry_cache_date"] = today.isoformat()
        self.state["expiry"] = expiry
        self.state["lot_size"] = int(lot)
        self._save_state()
        return expiry

    def _resolve_futures_token(self) -> Optional[str]:
        """Best-effort NIFTY near-month futures instrument key from Upstox's
        public instrument master. May be blocked on some networks (403) - in
        that case futures context stays blank (graceful, same as NSE mode)."""
        if self.state.get("futures_resolved"):
            return self.state.get("futures_token")
        token: Optional[str] = None
        try:
            url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE_FO.json.gz"
            raw: Optional[bytes] = None
            # curl first (Cloudflare-friendly TLS), then urllib fallback.
            if UPSTOX_USE_CURL and shutil.which("curl"):
                proc = subprocess.run(
                    ["curl", "-sS", "-m", "40", "--compressed", "-A", USER_AGENT, url],
                    capture_output=True, timeout=45)
                if proc.returncode == 0:
                    raw = proc.stdout
            if raw is None:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
            try:
                data = json.loads(gzip.decompress(raw))
            except Exception:
                data = json.loads(raw.decode("utf-8", "replace"))  # maybe served uncompressed
            today = now_ist().date()
            futs = [d for d in data
                    if d.get("instrument_type") == "FUT" and d.get("name") == "NIFTY" and d.get("expiry")]
            futs.sort(key=lambda d: d.get("expiry") or "")
            for d in futs:
                ed = parse_expiry(d.get("expiry"))
                if ed and ed >= today:
                    token = d.get("instrument_key")
                    break
        except Exception as exc:
            logging.warning("NIFTY futures token resolution unavailable (%s); futures left blank", exc)
        self.state["futures_resolved"] = True
        self.state["futures_token"] = token
        self._save_state()
        return token

    def _tick_stats_1m(self, capture: dt.datetime) -> Dict[str, Any]:
        cutoff = capture - dt.timedelta(seconds=60)
        spots = [float(t["spot"]) for t in self._recent_ticks
                 if t.get("spot") is not None
                 and parse_nse_timestamp(t.get("ts")) is not None
                 and parse_nse_timestamp(t.get("ts")) >= cutoff]
        if not spots:
            return {"high": None, "low": None, "samples": 0}
        return {"high": max(spots), "low": min(spots), "samples": len(spots)}

    def _side_values(self, prefix: str, side: Dict[str, Any], spot: float, strike: int,
                     prev: Optional[Dict[str, Any]], source_ts: Optional[str],
                     prev_capture: Optional[dt.datetime], lot_size: Optional[int]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        md = side.get("market_data") or {}
        gr = side.get("option_greeks") or {}
        # Upstox reports OI/volume in QUANTITY (shares). Normalise to CONTRACTS
        # (oi / lot_size) so the values match the NSE schema's unit and the
        # downstream OI gates / max-pain / flow logic stays comparable.
        lot = lot_size if (lot_size and lot_size > 0) else 1
        def _contracts(v: Optional[float]) -> Optional[int]:
            if v is None:
                return None
            return int(round(v / lot))

        oi = _contracts(number(md.get("oi"), integer=True))
        prev_oi = _contracts(number(md.get("prev_oi"), integer=True))
        volume = _contracts(number(md.get("volume"), integer=True))
        ltp = number(md.get("ltp"))
        bid = number(md.get("bid_price"))
        ask = number(md.get("ask_price"))
        mid, spread, spread_pct = option_mid_spread(bid, ask)
        iv = number(gr.get("iv"))
        close_price = number(md.get("close_price"))
        exch_change = (oi - prev_oi) if (oi is not None and prev_oi is not None) else None
        price_change = (ltp - close_price) if (ltp is not None and close_price is not None) else None
        price_pchange = ((ltp - close_price) / close_price * 100.0
                         if price_change is not None and close_price else None)
        interval_oi = interval_vol = interval_seconds = None
        if prev and prev.get("source_timestamp") != source_ts:
            old_oi = number(prev.get("oi"), integer=True)
            old_vol = number(prev.get("volume"), integer=True)
            if oi is not None and old_oi is not None:
                interval_oi = oi - old_oi
            if volume is not None and old_vol is not None and volume >= old_vol:
                interval_vol = volume - old_vol
        if prev_capture is not None and source_ts is not None:
            prev_ts = parse_nse_timestamp(prev.get("source_timestamp")) if prev else None
            cur_ts = parse_nse_timestamp(source_ts)
            if prev_ts and cur_ts and cur_ts > prev_ts:
                interval_seconds = round((cur_ts - prev_ts).total_seconds(), 1)
        intrinsic = max(spot - strike, 0.0) if prefix == "ce" else max(strike - spot, 0.0)
        extrinsic = (ltp - intrinsic) if ltp is not None else None
        values = {
            "identifier": side.get("instrument_key"),
            "oi": oi,
            "exchange_change_oi": exch_change,
            "exchange_pchange_oi": None,
            "volume_cumulative": volume,
            "ltp": ltp,
            "price_change": price_change,
            "price_pchange": price_pchange,
            "iv": iv,
            "iv_valid": iv is not None and iv > 0,
            "bid": bid,
            "bid_qty": number(md.get("bid_qty"), integer=True),
            "ask": ask,
            "ask_qty": number(md.get("ask_qty"), integer=True),
            "total_buy_qty": None,
            "total_sell_qty": None,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
            "intrinsic": intrinsic,
            "extrinsic_ltp": extrinsic,
            "interval_seconds": interval_seconds,
            "interval_oi_change": interval_oi,
            "interval_volume": interval_vol,
            "volume_counter_reset": False,
        }
        state = {"oi": oi, "volume": volume, "source_timestamp": source_ts}
        return values, state

    def collect_once(self) -> bool:
        capture = now_ist()
        self._prepare_session(capture.date())
        expiry = self._choose_expiry(capture.date())
        chain = self.client.option_chain(UPSTOX_NIFTY_KEY, expiry)
        if not chain:
            raise RuntimeError("Upstox: empty option chain response")

        fut_token = self._resolve_futures_token()
        quote_keys = [UPSTOX_NIFTY_KEY, UPSTOX_VIX_KEY] + [f"NSE_INDEX|{name}" for _, name in UPSTOX_SECTOR_KEYS]
        if fut_token:
            quote_keys.append(fut_token)
        quotes = self.client.quotes(quote_keys)
        nifty = quotes.get(_upstox_norm(UPSTOX_NIFTY_KEY), {})
        vix = quotes.get(_upstox_norm(UPSTOX_VIX_KEY), {})

        spot_raw = number(chain[0].get("underlying_spot_price"))
        spot = spot_raw if (spot_raw and spot_raw > 0) else number(nifty.get("last_price"))
        if not spot or spot <= 0:
            raise RuntimeError("Upstox: no NIFTY spot price")
        spot = float(spot)
        current_atm = round_to_strike(spot)
        if self.state.get("session_anchor_atm") is None:
            self.state["session_anchor_atm"] = current_atm
        anchor = int(self.state["session_anchor_atm"])
        fixed_low = anchor - STRIKE_GAP * STRIKES_ON_EACH_SIDE
        fixed_high = anchor + STRIKE_GAP * STRIKES_ON_EACH_SIDE
        range_low = min(fixed_low, current_atm - STRIKE_GAP * MIN_CURRENT_ATM_COVERAGE)
        range_high = max(fixed_high, current_atm + STRIKE_GAP * MIN_CURRENT_ATM_COVERAGE)

        snapshot_id = capture.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        source_timestamp = iso_or_none(capture)
        chain_hash = hash_payload(chain)[:16]
        nifty_last = number(nifty.get("last_price"))
        nifty_net = number(nifty.get("net_change"))
        nifty_ohlc = nifty.get("ohlc") or {}
        nifty_prev = (nifty_last - nifty_net) if (nifty_last is not None and nifty_net is not None) else number(nifty_ohlc.get("close"))
        vix_last = number(vix.get("last_price"))
        vix_net = number(vix.get("net_change"))
        vix_ohlc = vix.get("ohlc") or {}
        vix_prev = (vix_last - vix_net) if (vix_last is not None and vix_net is not None) else number(vix_ohlc.get("close"))
        fut = quotes.get(_upstox_norm(fut_token), {}) if fut_token else {}
        nifty_futures = number(fut.get("last_price"))
        futures_vwap = number(fut.get("average_price"))

        base = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "capture_timestamp": source_timestamp,
            "source_timestamp": source_timestamp,
            "timezone": "Asia/Kolkata",
            "source": "UPSTOX",
            "underlying": "NIFTY",
            "expiry": expiry,
            "lot_size": self.state.get("lot_size"),
            "chain_hash": chain_hash,
            "source_age_seconds": 0.0,
            "source_is_stale": False,
            "spot": spot,
            "nifty_index_last": nifty_last,
            "nifty_day_open": number(nifty_ohlc.get("open")),
            "nifty_day_high": number(nifty_ohlc.get("high")),
            "nifty_day_low": number(nifty_ohlc.get("low")),
            "nifty_previous_close": nifty_prev,
            "india_vix": vix_last,
            "current_atm": current_atm,
            "session_anchor_atm": anchor,
        }

        output_rows: List[Dict[str, Any]] = []
        pending: Dict[str, Dict[str, Any]] = {}
        contracts_state = self.state.setdefault("contracts", {})
        for item in chain:
            strike = number(item.get("strike_price"), integer=True)
            if strike is None or not (range_low <= strike <= range_high):
                continue
            row = dict(base)
            row["strike"] = strike
            row["strike_distance_from_spot"] = strike - spot
            row["moneyness"] = infer_moneyness(strike, spot)
            row["interval_seconds"] = None
            for prefix, side_key in (("ce", "call_options"), ("pe", "put_options")):
                side = item.get(side_key) or {}
                key = f"{expiry}|{strike}|{prefix.upper()}"
                prev = contracts_state.get(key)
                values, state = self._side_values(prefix, side, spot, strike, prev,
                                                  source_timestamp, self._last_capture,
                                                  self.state.get("lot_size"))
                for k, v in values.items():
                    row[f"{prefix}_{k}"] = v
                pending[key] = state
            ce_oi = row.get("ce_oi"); pe_oi = row.get("pe_oi")
            row["strike_oi_pcr"] = safe_ratio(pe_oi, ce_oi)
            row["exchange_change_oi_ce_minus_pe"] = safe_sub(
                row.get("ce_exchange_change_oi"), row.get("pe_exchange_change_oi"))
            output_rows.append(row)

        if not output_rows:
            raise RuntimeError("Upstox: no strikes in range after filtering")
        output_rows.sort(key=lambda r: int(r["strike"]))

        self._recent_ticks.append({"ts": source_timestamp, "spot": float(spot),
                                   "vix": vix_last})
        del self._recent_ticks[:-MAX_TICKS_KEPT]
        tick_stats = self._tick_stats_1m(capture)
        minutes_to_close = round(
            max(0.0, (dt.datetime.combine(capture.date(), MARKET_CLOSE, tzinfo=IST) - capture).total_seconds() / 60.0), 1)

        context_row = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "capture_timestamp": source_timestamp,
            "source_timestamp": source_timestamp,
            "timezone": "Asia/Kolkata",
            "source": "UPSTOX",
            "expiry": expiry,
            "chain_hash": chain_hash,
            "source_age_seconds": 0.0,
            "source_is_stale": False,
            "spot_chain": spot,
            "chain_data_age_seconds": 0.0,
            "chain_frozen": False,
            "minutes_to_close": minutes_to_close,
            "spot_high_1m": tick_stats.get("high"),
            "spot_low_1m": tick_stats.get("low"),
            "spot_samples_1m": tick_stats.get("samples"),
            "nifty_index_last": nifty_last,
            "nifty_day_open": number(nifty_ohlc.get("open")),
            "nifty_day_high": number(nifty_ohlc.get("high")),
            "nifty_day_low": number(nifty_ohlc.get("low")),
            "nifty_previous_close": nifty_prev,
            "nifty_year_high": None,
            "nifty_year_low": None,
            "india_vix": vix_last,
            "vix_day_open": number(vix_ohlc.get("open")),
            "vix_day_high": number(vix_ohlc.get("high")),
            "vix_day_low": number(vix_ohlc.get("low")),
            "vix_previous_close": vix_prev,
            "current_atm": current_atm,
            "session_anchor_atm": anchor,
            "scheduled_event_note": SCHEDULED_EVENT or None,
            "external_source": None,
            "external_source_timestamp": None,
            "external_age_seconds": None,
            "external_is_stale": None,
            "nifty_futures": nifty_futures,
            "futures_open_1m": None,
            "futures_high_1m": None,
            "futures_low_1m": None,
            "futures_close_1m": None,
            "futures_volume_1m": None,
            "futures_vwap": futures_vwap,
            "true_futures_context_available": False,
        }

        summary = _build_summary(output_rows, context_row, range_low=range_low,
                                                   range_high=range_high, source_is_stale=False,
                                                   true_futures_context=False, test_mode=self.test_mode)

        date_label = capture.date().isoformat()
        day = _session_dir(self.output_dir, date_label)
        day.mkdir(parents=True, exist_ok=True)
        chain_path = day / f"nifty-option-chain-v2-{date_label}.csv"
        context_path = day / f"nifty-market-context-v2-{date_label}.csv"
        summary_path = day / f"nifty-snapshot-summary-v2-{date_label}.csv"
        append_csv(chain_path, output_rows, CHAIN_COLUMNS)
        append_csv(context_path, [context_row], CONTEXT_COLUMNS)
        append_csv(summary_path, [summary], SUMMARY_COLUMNS)

        # sectoral context (reuses the same _build_sector_row + SECTOR_COLUMNS)
        # NOTE: _build_sector_row requires the NIFTY 50 entry itself, so add it.
        indices_payload: Dict[str, Any] = {"data": [{
            "index": "NIFTY 50", "indexSymbol": "NIFTY 50",
            "last": nifty_last,
            "previousClose": nifty_prev,
        }]}
        for _key, name in UPSTOX_SECTOR_KEYS:
            q = quotes.get(_upstox_norm(f"NSE_INDEX|{name}"), {})
            last = number(q.get("last_price"))
            net = number(q.get("net_change"))
            prev = (last - net) if (last is not None and net is not None) else None
            indices_payload["data"].append({"index": name, "indexSymbol": name,
                                            "last": last, "previousClose": prev})
        try:
            sector_row = _build_sector_row(indices_payload, snapshot_id, capture, source_timestamp)
            if sector_row:
                append_csv(day / f"nifty-sector-context-v2-{date_label}.csv",
                           [sector_row], SECTOR_COLUMNS)
        except Exception:
            logging.debug("Sector context write failed (upstox)", exc_info=True)

        self.state.setdefault("contracts", {}).update(pending)
        self.state["last_snapshot_id"] = snapshot_id
        self.state["last_capture_timestamp"] = source_timestamp
        self._last_capture = capture
        self._save_state()

        try:
            _write_llm_compact_chain(day, date_label)
        except Exception:
            logging.exception("LLM compact chain export failed (upstox)")

        try:
            build_llm_packet(day, date_label, snapshot_id)
        except Exception:
            logging.exception("LLM analytics packet generation failed (upstox)")

        try:
            _paper_check_exits(day, date_label)
        except Exception:
            logging.exception("Paper trade exit check failed")

        logging.info("Upstox saved %d strikes | spot %.2f | ATM %d | expiry %s | futures %s | vwap %s",
                     len(output_rows), spot, current_atm, expiry, nifty_futures, futures_vwap)
        return True

    def tick_once(self) -> None:
        capture = now_ist()
        try:
            quotes = self.client.quotes([UPSTOX_NIFTY_KEY, UPSTOX_VIX_KEY])
        except Exception as exc:
            logging.warning("Upstox tick capture failed: %s", exc)
            return
        nifty = quotes.get(_upstox_norm(UPSTOX_NIFTY_KEY), {})
        vix = quotes.get(_upstox_norm(UPSTOX_VIX_KEY), {})
        spot = number(nifty.get("last_price"))
        if not spot or spot <= 0:
            return
        self._recent_ticks.append({"ts": iso_or_none(capture), "spot": float(spot),
                                   "vix": number(vix.get("last_price"))})
        del self._recent_ticks[:-MAX_TICKS_KEPT]
        nifty_ohlc = nifty.get("ohlc") or {}
        tick_day = _session_dir(self.output_dir, capture.date().isoformat())
        tick_day.mkdir(parents=True, exist_ok=True)
        append_csv(
            tick_day / f"nifty-spot-ticks-v2-{capture.date().isoformat()}.csv",
            [{
                "schema_version": SCHEMA_VERSION,
                "capture_timestamp": iso_or_none(capture),
                "spot": spot,
                "india_vix": number(vix.get("last_price")),
                "nifty_index_last": spot,
                "index_day_high": number(nifty_ohlc.get("high")),
                "index_day_low": number(nifty_ohlc.get("low")),
            }],
            ["schema_version", "capture_timestamp", "spot", "india_vix",
             "nifty_index_last", "index_day_high", "index_day_low"],
        )

    def close(self) -> None:
        return None


def run_upstox_check() -> int:
    """One-shot Upstox connectivity probe: verifies the access token and prints
    NIFTY spot + VIX from the authenticated API."""
    print("Probing Upstox API (token validity + NIFTY spot/VIX)")
    try:
        client = UpstoxClient()
        quotes = client.quotes([UPSTOX_NIFTY_KEY, UPSTOX_VIX_KEY])
        nifty = quotes.get(_upstox_norm(UPSTOX_NIFTY_KEY), {})
        vix = quotes.get(_upstox_norm(UPSTOX_VIX_KEY), {})
        print("UPSTOX CONNECTIVITY: OK")
        print("  NIFTY 50 last:", nifty.get("last_price"),
              "| day OHLC:", (nifty.get("ohlc") or {}).get("open"),
              (nifty.get("ohlc") or {}).get("high"), (nifty.get("ohlc") or {}).get("low"))
        print("  India VIX last:", vix.get("last_price"))
        try:
            contracts = client.option_contracts(UPSTOX_NIFTY_KEY)
            expiries = sorted({c["expiry"] for c in contracts if c.get("expiry")})[:4]
            lot = next((c.get("lot_size") for c in contracts if c.get("lot_size")), None)
            print("  nearest expiries:", expiries, "| lot size:", lot)
        except Exception as exc:
            print("  option/contract endpoint blocked (", exc, "); using local Tuesday-expiry fallback")
        return 0
    except Exception as exc:
        print("UPSTOX CONNECTIVITY: FAILED:", exc)
        print("  -> Check UPSTOX_ACCESS_TOKEN (tokens expire daily; regenerate in")
        print("     the Upstox developer console and set the env var).")
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NIFTY options collector (Upstox data source)")
    parser.add_argument("--once", action="store_true", help="capture one snapshot and exit")
    parser.add_argument(
        "--check-upstox",
        action="store_true",
        help="one-shot Upstox probe: verify the access token and print NIFTY spot/VIX",
    )
    parser.add_argument(
        "--paper-report",
        metavar="DATE",
        help="print the paper-trading results for DATE (YYYY-MM-DD) - offline, no network",
    )
    parser.add_argument(
        "--allow-outside-hours",
        action="store_true",
        help="permit capture outside nominal NSE hours (data will be marked stale)",
    )
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="output directory")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="V2.7: run the offline regression suite (no network, no playwright) and exit",
    )
    parser.add_argument(
        "--analyze",
        metavar="DATE",
        help="V2.7: offline decision-engine run on collected files for DATE (YYYY-MM-DD); "
             "no network, no writes to production outputs",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="V2.7 TEMPORARY TEST MODE: permits off-hours capture, redirects the default "
             "output dir to <output-dir>_test, and tags summary rows TEST-MODE. "
             "Use with --once for a single end-to-end test; combine with "
             "--output-dir to pick the test directory explicitly.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # One-shot connectivity probe: no output dirs, no lock.
    if args.check_upstox:
        return run_upstox_check()
    if args.paper_report:
        return run_paper_report(Path(args.output_dir), args.paper_report)

    # V2.7: offline commands first (no network, no playwright, no output dirs).
    if args.selftest:
        return run_selftest()

    output_dir = Path(args.output_dir)
    if args.test and args.output_dir == str(OUTPUT_DIR):
        output_dir = Path(str(OUTPUT_DIR) + "_test")
        print(f"TEST MODE: default output redirected to {output_dir} (production data untouched)")

    if args.analyze:
        return run_analyze_once(output_dir, args.analyze)

    configure_logging(output_dir)
    logging.info("Output directory: %s", output_dir.resolve())
    allow_outside = args.allow_outside_hours or args.test
    if args.test:
        logging.warning(
            "TEST MODE: off-hours capture enabled; writing to %s. "
            "Rows are tagged TEST-MODE in quality_notes. "
            "This data is NOT actionable for live decisions.",
            output_dir,
        )
    # AUDIT-F5: the lock is taken for --once too — a cron-driven once-per-
    # minute deployment is the most natural use and previously had NO mutual
    # exclusion, so overlapping runs interleaved CSV rows and raced on
    # collector_state.json.
    instance_lock = acquire_instance_lock(output_dir)
    if instance_lock is None:
        return 3
    collector = UpstoxCollector(output_dir, test_mode=args.test)
    logging.info(
        "Starting UPSTOX NIFTY collector: ATM ±%d strikes, one-minute aligned capture",
        STRIKES_ON_EACH_SIDE,
    )

    try:
        if args.once:
            if not allow_outside and not is_nominal_market_hours(now_ist()):
                logging.error("Outside nominal NSE hours; use --allow-outside-hours for a test capture")
                return 2
            try:
                collector.collect_once()
            except RuntimeError as exc:
                logging.error("Capture failed: %s", exc)
                return 1
            if args.test:
                _print_after_capture(output_dir)
            return 0

        while True:
            current = now_ist()
            if not allow_outside and not is_nominal_market_hours(current):
                logging.info("Outside nominal NSE hours at %s; waiting", current.isoformat(timespec="seconds"))
                time.sleep(30)
                continue
            try:
                collector.collect_once()
            except Exception:
                logging.exception("Snapshot cycle failed")
            sleep_with_ticks(collector)
    except KeyboardInterrupt:
        logging.info("Collector manually stopped")
        return 0
    finally:
        collector.close()
        release_instance_lock(instance_lock)
        logging.info("Collector closed")


if __name__ == "__main__":
    raise SystemExit(main())
