"""SCREENING ONLY — no trading logic.

Fixed-universe price-return momentum screening against the NIFTY 500 price index.
Own-price, cross-sectional and market-relative metrics remain separate. Frozen
multi-horizon qualifiers and an all-eligible composite produce screening reports.
Stage-2 buying checks apply confirmed breakout, relative strength, volume confirmation,
and extension filters to FINAL_QUALIFIER stocks, ranked by ENTRY_QUALITY_SCORE.
The aligned-matrix metric interface is pure and suitable for research harnesses.

All reports, candle caches, instrument-master JSON and log files are written under
stock-data/ beside this script (never under data/, output/ or logs/).

Credential loading: by default, env.txt is read from beside this Python file,
not from the shell's working directory. Override with --env /path/to/env.txt.
No manual environment-variable setup is necessary when that file is populated.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as wall_time, timedelta, timezone
import gzip
import html
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
import time
from typing import Any, Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from dotenv import dotenv_values

# =============================================================================
# Frozen configuration and fixed universe
# =============================================================================
# ---- FROZEN RESEARCH PARAMETERS (do not tune at runtime; change only via code + README changelog) ----
HORIZONS = (21, 63, 126, 252)          # trading days
MIN_HISTORY_DAYS = 253                 # closes needed for a 252-day return (252 returns)
MAX_MISSING_DAYS = 3                   # max gaps in the 253-row window that may be forward-filled
MIN_ELIGIBLE_FRACTION = 0.50           # abort if fewer than this fraction of the universe is eligible
FETCH_CALENDAR_DAYS = 550              # calendar-day lookback requested from Upstox (buffer for holidays)
VOL_ANNUALISE = True                   # Vol_n = std(daily simple returns, ddof=1) * sqrt(252)
RANK_BASIS = "R"                       # cross-sectional ranks computed on raw n-day return R_n ("RAM" = alternative)
P_STRONG = 70.0                        # UNVALIDATED DEFAULT — percentile cutoff for STRONG_XS_MOMENTUM
P_VERY_STRONG = 85.0                   # UNVALIDATED DEFAULT — percentile cutoff for VERY_STRONG_MOMENTUM
POS_COUNT_MIN = 3                      # "3 of 4" multi-horizon confirmation — a screen-design choice
COMPOSITE_WEIGHTS = {                  # FINAL_SCREEN_RANK components (all 0-100), equal weights
    "Rank252": 1.0, "Rank126": 1.0, "pct_MR252": 1.0, "pct_MR126": 1.0, "pct_RAM252": 1.0,
}
CORP_ACTION_JUMP = 0.25                # |daily return| above this in the window → SUSPECT_CORP_ACTION flag
BENCHMARK_NAME = "NIFTY 500"
BENCHMARK_EXPECTED_KEY = "NSE_INDEX|Nifty 500"

# Stage 2 Buying Checks frozen research parameters
RVOL20_MIN = 1.5                       # Minimum RVOL20 (current volume vs prior 20-session median)
BREAKOUT_DISTANCE20_MIN = 0.0          # Minimum ATR-normalized breakout distance
BREAKOUT_DISTANCE20_MAX = 1.5          # Maximum ATR-normalized breakout distance
ATR_PERIOD = 14                        # Wilder ATR period (calculated through session T-1)
HH_PERIOD = 20                         # Lookback for highest high (excluding session T)
RS20_PERIOD = 20                       # RS horizon for buying check 2 (trading days)
RS60_PERIOD = 60                       # RS horizon for buying check 3 (trading days)
BUYING_SCORE_WEIGHTS = {               # ENTRY_QUALITY_SCORE component weights
    "RS20": 0.20,
    "RS60": 0.20,
    "SectorRS20": 0.00,                # Omitted in version 1
    "RVOL20": 0.15,
    "BreakoutQuality": 0.15,
    "Extension": 0.15,
}

UNIVERSE = [
    "360ONE", "3MINDIA", "ABB", "ACC", "ACMESOLAR", "AIAENG", "APLAPOLLO", "AUBANK", "AWL", "AADHARHFC",
    "AARTIIND", "AAVAS", "ABBOTINDIA", "ACE", "ACUTAAS", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER",
    "ATGL", "ABCAPITAL", "ABFRL", "ABLBL", "ABREL", "ABSLAMC", "CPPLUS", "AEGISLOG", "AEGISVOPAK", "AFCONS",
    "AFFLE", "AJANTPHARM", "ALKEM", "ABDL", "ARE&M", "AMBER", "AMBUJACEM", "ANANDRATHI", "ANANTRAJ", "ANGELONE",
    "ANTHEM", "ANURAS", "APARINDS", "APOLLOHOSP", "APOLLOTYRE", "APTUS", "ASAHIINDIA", "ASHOKLEY", "ASIANPAINT", "ASTERDM",
    "ASTRAL", "ATHERENERG", "ATUL", "AUROPHARMA", "AIIL", "DMART", "AXISBANK", "BEML", "BLS", "BSE",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG", "BAJAJHFL", "BALKRISIND", "BALRAMCHIN", "BANDHANBNK", "BANKBARODA", "BANKINDIA",
    "MAHABANK", "BATAINDIA", "BAYERCROP", "BELRISE", "BERGEPAINT", "BDL", "BEL", "BHARATFORG", "BHEL", "BPCL",
    "BHARTIARTL", "BHARTIHEXA", "BIKAJI", "GROWW", "BIOCON", "BSOFT", "BLUEDART", "BLUEJET", "BLUESTARCO", "BBTC",
    "BOSCHLTD", "FIRSTCRY", "BRIGADE", "BRITANNIA", "MAPMYINDIA", "CCL", "CESC", "CGPOWER", "CIEINDIA", "CRISIL",
    "CANFINHOME", "CANBK", "CANHLIFE", "CAPLIPOINT", "CGCL", "CARBORUNIV", "CARTRADE", "CASTROLIND", "CEATLTD", "CEMPRO",
    "CENTRALBK", "CDSL", "CHALET", "CHAMBLFERT", "CHENNPETRO", "CHOICEIN", "CHOLAHLDNG", "CHOLAFIN", "CIPLA", "CUB",
    "CLEAN", "COALINDIA", "COCHINSHIP", "COFORGE", "COHANCE", "COLPAL", "CAMS", "CONCORDBIO", "CONCOR", "COROMANDEL",
    "CRAFTSMAN", "CREDITACC", "CROMPTON", "CUMMINSIND", "CYIENT", "DCMSHRIRAM", "DLF", "DOMS", "DABUR", "DALBHARAT",
    "DATAPATTNS", "DEEPAKFERT", "DEEPAKNTR", "DELHIVERY", "DEVYANI", "DIVISLAB", "DIXON", "LALPATHLAB", "DRREDDY", "EIDPARRY",
    "EIHOTEL", "EICHERMOT", "ELECON", "ELGIEQUIP", "EMAMILTD", "EMCURE", "EMMVEE", "ENDURANCE", "ENGINERSIN", "ERIS",
    "ESCORTS", "ETERNAL", "EXIDEIND", "NYKAA", "FEDERALBNK", "FACT", "FINCABLES", "FSL", "FIVESTAR", "FORCEMOT",
    "FORTIS", "GAIL", "GVT&D", "GMRAIRPORT", "GABRIEL", "GALLANTT", "GRSE", "GICRE", "GILLETTE", "GLAND",
    "GLAXO", "GLENMARK", "MEDANTA", "GODIGIT", "GPIL", "GODFRYPHLP", "GODREJCP", "GODREJIND", "GODREJPROP", "GRANULES",
    "GRAPHITE", "GRASIM", "GRAVITA", "GESHIP", "FLUOROCHEM", "GMDCLTD", "HEG", "HBLENGINE", "HCLTECH", "HDBFS",
    "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HFCL", "HAVELLS", "HEROMOTOCO", "HEXT", "HSCL", "HINDALCO", "HAL",
    "HINDCOPPER", "HINDPETRO", "HINDUNILVR", "HINDZINC", "POWERINDIA", "HOMEFIRST", "HONASA", "HONAUT", "HUDCO", "HYUNDAI",
    "ICICIBANK", "ICICIGI", "ICICIAMC", "ICICIPRULI", "IDBI", "IDFCFIRSTB", "IFCI", "IIFL", "IRB", "IRCON",
    "ITCHOTELS", "ITC", "ITI", "INDGN", "INDIACEM", "INDIAMART", "INDIANB", "IEX", "INDHOTEL", "IOC",
    "IOB", "IRCTC", "IRFC", "IREDA", "IGL", "INDUSTOWER", "INDUSINDBK", "NAUKRI", "INFY", "INOXWIND",
    "INTELLECT", "INDIGO", "IGIL", "IKS", "IPCALAB", "JBCHEPHARM", "JKCEMENT", "JBMA", "JKTYRE", "JMFINANCIL",
    "JSWCEMENT", "JSWDULUX", "JSWENERGY", "JSWINFRA", "JSWSTEEL", "JAINREC", "JPPOWER", "J&KBANK", "JINDALSAW", "JSL",
    "JINDALSTEL", "JIOFIN", "JUBLFOOD", "JUBLINGREA", "JUBLPHARMA", "JWL", "JYOTICNC", "KPRMILL", "KEI", "KPITTECH",
    "KAJARIACER", "KPIL", "KALYANKJIL", "KARURVYSYA", "KAYNES", "KEC", "KFINTECH", "KIRLOSENG", "KOTAKBANK", "KIMS",
    "LTF", "LTTS", "LGEINDIA", "LICHSGFIN", "LTFOODS", "LTM", "LT", "LATENTVIEW", "LAURUSLABS", "THELEELA",
    "LEMONTREE", "LENSKART", "LICI", "LINDEINDIA", "LLOYDSME", "LODHA", "LUPIN", "MMTC", "MRF", "MGL",
    "M&MFIN", "M&M", "MANAPPURAM", "MRPL", "MANKIND", "MARICO", "MARUTI", "MFSL", "MAXHEALTH", "MAZDOCK",
    "MEESHO", "MINDACORP", "MSUMI", "MOTILALOFS", "MPHASIS", "MCX", "MUTHOOTFIN", "NATCOPHARM", "NBCC", "NCC",
    "NHPC", "NLCINDIA", "NMDC", "NSLNISP", "NTPCGREEN", "NTPC", "NH", "NATIONALUM", "NAVA", "NAVINFLUOR",
    "NESTLEIND", "NETWEB", "NEULANDLAB", "NEWGEN", "NAM-INDIA", "NIVABUPA", "NUVAMA", "NUVOCO", "OBEROIRLTY", "ONGC",
    "OIL", "OLAELEC", "OLECTRA", "PAYTM", "ONESOURCE", "OFSS", "POLICYBZR", "PCBL", "PGEL", "PIIND",
    "PNBHOUSING", "PTCIL", "PVRINOX", "PAGEIND", "PARADEEP", "PATANJALI", "PERSISTENT", "PETRONET", "PFIZER", "PHOENIXLTD",
    "PWL", "PIDILITIND", "PINELABS", "PIRAMALFIN", "PPLPHARMA", "POLYMED", "POLYCAB", "POONAWALLA", "PFC", "POWERGRID",
    "PREMIERENE", "PRESTIGE", "PNB", "RRKABEL", "RBLBANK", "RECLTD", "RHIM", "RITES", "RADICO", "RVNL",
    "RAILTEL", "RAINBOW", "RKFORGE", "REDINGTON", "RELIANCE", "RPOWER", "SBFC", "SBICARD", "SBILIFE", "SJVN",
    "SRF", "SAGILITY", "SAILIFE", "SAMMAANCAP", "MOTHERSON", "SAPPHIRE", "SARDAEN", "SAREGAMA", "SCHAEFFLER", "SCHNEIDER",
    "SCI", "SHREECEM", "SHRIRAMFIN", "SHYAMMETL", "ENRIN", "SIEMENS", "SIGNATURE", "SOBHA", "SOLARINDS", "SONACOMS",
    "SONATSOFTW", "STARHEALTH", "SBIN", "SAIL", "SUMICHEM", "SUNPHARMA", "SUNTV", "SUNDARMFIN", "SUPREMEIND", "SPLPETRO",
    "SUZLON", "SWANCORP", "SWIGGY", "SYNGENE", "SYRMA", "TBOTEK", "TVSMOTOR", "TATACAP", "TATACHEM", "TATACOMM",
    "TCS", "TATACONSUM", "TATAELXSI", "TATAINVEST", "TMCV", "TMPV", "TATAPOWER", "TATASTEEL", "TATATECH", "TTML",
    "TECHM", "TECHNOE", "TEGA", "TEJASNET", "TENNIND", "NIACL", "RAMCOCEM", "THERMAX", "TIMKEN", "TITAGARH",
    "TITAN", "TORNTPHARM", "TORNTPOWER", "TARIL", "TRAVELFOOD", "TRENT", "TRIDENT", "TRITURBINE", "TIINDIA", "UCOBANK",
    "UNOMINDA", "UPL", "UTIAMC", "ULTRACEMCO", "UNIONBANK", "UBL", "UNITDSPR", "URBANCO", "USHAMART", "VTL",
    "VBL", "VEDL", "VIJAYA", "VMM", "IDEA", "VOLTAS", "WAAREEENER", "WELCORP", "WELSPUNLIV", "WHIRLPOOL",
    "WIPRO", "WOCKPHARMA", "YESBANK", "ZFCVINDIA", "ZEEL", "ZENTEC", "ZENSARTECH", "ZYDUSLIFE", "ZYDUSWELL", "ECLERX"
]
assert len(UNIVERSE) == len(set(UNIVERSE)) == 500

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "https://api.upstox.com"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

# All screener artefacts (reports, candle cache, instrument master, logs)
# live under this directory — nowhere else under the repo.
STOCK_DATA_ROOT = Path(__file__).resolve().parent / "stock-data"
STOCK_CACHE_DIR = STOCK_DATA_ROOT / "cache"
STOCK_LOG_DIR = STOCK_DATA_ROOT / "logs"
SCREEN_COLUMNS = (
    "Symbol", "Close", "R21", "R63", "R126", "R252",
    "RAM21", "RAM63", "RAM126", "RAM252",
    "Rank21", "Rank63", "Rank126", "Rank252",
    "MR21", "MR63", "MR126", "MR252", "POS_COUNT", "FINAL_SCREEN_RANK",
)
FLAG_COLUMNS = (
    "MARKET_OUTPERFORMER", "STRONG_TREND", "STRONG_XS_MOMENTUM",
    "VERY_STRONG_MOMENTUM", "CORE_QUALIFIER", "FINAL_QUALIFIER",
)
BUYING_REPORT_COLUMNS = (
    "Symbol", "Close", "HH20", "RS20", "RS60", "RVOL20",
    "ATR14", "BreakoutDistance20", "CloseLocation",
    "PRS20", "PRS60", "PRVOL20", "PQuality", "PExtension",
    "ExtensionScore", "ENTRY_QUALITY_SCORE", "RAW_SCORE",
)
AUTH_MESSAGE = "Access token rejected/expired — regenerate UPSTOX_ACCESS_TOKEN in env.txt and rerun."
ADJUSTMENT_NOTE = (
    "Upstox daily closes are stated to be split-adjusted; dividend adjustment is not "
    "guaranteed. Returns are price returns against the NIFTY 500 price index, not TRI. "
    "Later vendor revisions or split adjustments can change historical results."
)


def validate_config() -> None:
    """Reject inconsistent frozen parameters before credentials or network access."""
    if tuple(sorted(set(HORIZONS))) != HORIZONS or min(HORIZONS) < 2:
        raise ValueError("HORIZONS must be positive, unique and sorted.")
    if MIN_HISTORY_DAYS != max(HORIZONS) + 1:
        raise ValueError("MIN_HISTORY_DAYS must cover the largest horizon plus one close.")
    if not 0 <= P_STRONG <= P_VERY_STRONG <= 100:
        raise ValueError("Require 0 <= P_STRONG <= P_VERY_STRONG <= 100.")
    expected = {"Rank252", "Rank126", "pct_MR252", "pct_MR126", "pct_RAM252"}
    if set(COMPOSITE_WEIGHTS) != expected:
        raise ValueError("Composite component names do not match the frozen design.")
    if any(not math.isfinite(w) or w < 0 for w in COMPOSITE_WEIGHTS.values()):
        raise ValueError("Composite weights must be finite and nonnegative.")
    if sum(COMPOSITE_WEIGHTS.values()) <= 0:
        raise ValueError("Composite weights must have a positive sum.")
    if RANK_BASIS not in {"R", "RAM"}:
        raise ValueError("RANK_BASIS must be R or RAM.")
    if len(set(UNIVERSE)) != len(UNIVERSE):
        raise ValueError("The fixed universe must contain unique symbols.")
    if len(UNIVERSE) < 2:
        raise ValueError("The fixed universe must contain at least 2 symbols.")
    if RVOL20_MIN <= 0:
        raise ValueError("RVOL20_MIN must be positive.")
    if not (0.0 <= BREAKOUT_DISTANCE20_MIN < BREAKOUT_DISTANCE20_MAX):
        raise ValueError("Require 0.0 <= BREAKOUT_DISTANCE20_MIN < BREAKOUT_DISTANCE20_MAX.")
    expected_buying = {"RS20", "RS60", "SectorRS20", "RVOL20", "BreakoutQuality", "Extension"}
    if set(BUYING_SCORE_WEIGHTS) != expected_buying:
        raise ValueError("BUYING_SCORE_WEIGHTS component names do not match the frozen design.")
    if any(not math.isfinite(w) or w < 0 for w in BUYING_SCORE_WEIGHTS.values()):
        raise ValueError("BUYING_SCORE_WEIGHTS must be finite and nonnegative.")
    if sum(BUYING_SCORE_WEIGHTS.values()) <= 0:
        raise ValueError("BUYING_SCORE_WEIGHTS must have a positive sum.")


# =============================================================================
# Credentials and sanitized failures
# =============================================================================
class ScreenerError(Exception):
    """A user-facing failure whose message must not contain response bodies or secrets."""


class AuthenticationError(ScreenerError):
    """Authentication failure: always fatal, including during individual-stock fetches."""


class APIError(ScreenerError):
    """Sanitized market-data transport or response failure."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Keep only a safe message and an optional HTTP status code."""
        super().__init__(message)
        self.status = status


class CacheError(ScreenerError):
    """Missing offline cache or invalid cache; never silently use different data."""


@dataclass(frozen=True)
class Credentials:
    """Secret values are excluded from dataclass representations."""

    access_token: str = field(repr=False)
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)


def load_credentials(env_path: str | Path) -> Credentials:
    """Read the credential file before any network access; never log its values.

    The CLI defaults to env.txt beside this Python file. Explicit relative
    --env paths are relative to the shell's working directory, as usual.
    UTF-8 files with or without a BOM are supported. File entries take
    precedence; the process environment is consulted only for absent keys.
    """
    path = Path(env_path).expanduser().absolute()
    try:
        # Opening explicitly avoids dotenv_values silently accepting a missing
        # file. utf-8-sig also removes a possible Windows/editor UTF-8 BOM.
        with path.open("r", encoding="utf-8-sig") as handle:
            values = dotenv_values(stream=handle, interpolate=False)
    except FileNotFoundError:
        # Preserve the original environment-only usage, without requiring it.
        if not os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip():
            raise ScreenerError(
                f"Credential file not found: {path}. "
                "Put env.txt beside this Python file, or pass --env /full/path/env.txt. "
                "Check that the filename is not env.txt.txt."
            ) from None
        values = {}
        logging.warning("Credential file not found at %s; using process environment.", path)
    except (OSError, UnicodeError):
        raise ScreenerError(
            f"Cannot read credential file: {path}. "
            "Check permissions and save it as plain UTF-8 text."
        ) from None
    else:
        logging.info("Read credential file: %s (values are not logged).", path)

    def get_value(key: str) -> str | None:
        """Preserve explicit empty file values instead of overriding them."""
        raw = values[key] if key in values else os.environ.get(key)
        if raw is None:
            return None
        val = str(raw).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1].strip()
        return val if val else None

    token = get_value("UPSTOX_ACCESS_TOKEN")
    api_key = get_value("UPSTOX_API_KEY")
    api_secret = get_value("UPSTOX_API_SECRET")
    telegram_bot_token = get_value("TELEGRAM_STOCK_BOT_TOKEN") or get_value("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = get_value("TELEGRAM_STOCK_CHAT_ID") or get_value("TELEGRAM_CHAT_ID")

    for name, value in (("UPSTOX_API_KEY", api_key), ("UPSTOX_API_SECRET", api_secret)):
        if not value:
            logging.warning("%s is missing; it is not used for market data.", name)
    if not token:
        raise ScreenerError(
            f"UPSTOX_ACCESS_TOKEN is missing or empty in {path} "
            "and no applicable environment fallback is available. "
            "Use UPSTOX_ACCESS_TOKEN=your_complete_current_access_token. "
            "An explicitly empty file entry is not replaced by an environment value."
        )
    if any(character in token for character in ("\r", "\n")):
        raise ScreenerError("UPSTOX_ACCESS_TOKEN has an invalid format; keep it on one line.")
    if telegram_bot_token and any(character in telegram_bot_token for character in ("\r", "\n")):
        raise ScreenerError("TELEGRAM_STOCK_BOT_TOKEN has an invalid format; keep it on one line.")
    if telegram_chat_id and any(character in telegram_chat_id for character in ("\r", "\n")):
        raise ScreenerError("TELEGRAM_STOCK_CHAT_ID has an invalid format; keep it on one line.")

    if telegram_bot_token and telegram_chat_id:
        if ":" not in telegram_bot_token:
            logging.warning(
                "TELEGRAM_STOCK_BOT_TOKEN appears incomplete (missing ':'). "
                "Telegram bot tokens from @BotFather follow the format '<bot_id>:<token_secret>' "
                "(e.g., '123456789:ABCdefGhIJK...'). Check token from @BotFather."
            )
        logging.info("Telegram notification enabled for chat ID: %s (bot token is not logged).", telegram_chat_id)
    elif (telegram_bot_token and not telegram_chat_id) or (telegram_chat_id and not telegram_bot_token):
        logging.warning(
            "Telegram notification requires both TELEGRAM_STOCK_BOT_TOKEN and "
            "TELEGRAM_STOCK_CHAT_ID. One is missing; Telegram notifications will be disabled."
        )
    else:
        logging.info("Telegram notification not configured (skipping Telegram delivery).")

    return Credentials(token, api_key, api_secret, telegram_bot_token, telegram_chat_id)


# =============================================================================
# REST client, rate limiting, instrument resolution and raw caches
# =============================================================================
def atomic_text(path: Path, content: str) -> None:
    """Replace one UTF-8 file atomically; retain no partially written cache files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def empty_series() -> pd.Series:
    """Return an empty, typed daily close series."""
    return pd.Series(index=pd.DatetimeIndex([], name="date"), dtype=float, name="close")


def empty_ohlcv() -> pd.DataFrame:
    """Return an empty, typed daily OHLCV dataframe."""
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], name="date"),
        dtype=float,
    )


def normalize_series(series: pd.Series) -> pd.Series:
    """Normalize dates to naive IST calendar midnights, sort, keep last duplicates."""
    result = series.copy().astype(float)
    index = pd.DatetimeIndex(result.index)
    if index.hasnans:
        raise ValueError("Daily dates must not contain missing timestamps.")
    if index.tz is not None:
        index = index.tz_convert(IST).tz_localize(None)
    result.index = index.normalize()
    result = result.loc[~result.index.duplicated(keep="last")].sort_index()
    result.index.name = "date"
    result.name = "close"
    return result


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dates to naive IST calendar midnights, sort, keep last duplicates."""
    result = df.copy().astype(float)
    index = pd.DatetimeIndex(result.index)
    if index.hasnans:
        raise ValueError("Daily dates must not contain missing timestamps.")
    if index.tz is not None:
        index = index.tz_convert(IST).tz_localize(None)
    result.index = index.normalize()
    result = result.loc[~result.index.duplicated(keep="last")].sort_index()
    result.index.name = "date"
    return result


class UpstoxClient:
    """Read-only REST client with bounded retries and per-instance rate accounting."""

    def __init__(self, credentials: Credentials, cache_dir: Path,
                 refresh: bool = False, offline: bool = False,
                 today: date | None = None,
                 master_dir: Path | None = None) -> None:
        """Build separate authenticated API and unauthenticated asset sessions."""
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {credentials.access_token}",
                                     "Accept": "application/json"})
        self.asset_session = requests.Session()
        self.asset_session.headers.update({"Accept": "application/json"})
        self.cache_dir = Path(cache_dir)
        self.master_dir = Path(master_dir) if master_dir is not None else STOCK_DATA_ROOT
        self.refresh = refresh
        self.offline = offline
        self.today = today or datetime.now(IST).date()
        self.request_times: deque[float] = deque()
        self.sources: set[str] = set()
        if refresh and offline:
            raise ScreenerError("--refresh and --offline cannot be combined.")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.master_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Release both HTTP connection pools."""
        self.session.close()
        self.asset_session.close()

    def _throttle(self) -> None:
        """Space calls by 0.21 seconds and enforce rolling minute/30-minute caps."""
        while True:
            now = time.monotonic()
            while self.request_times and now - self.request_times[0] >= 1800:
                self.request_times.popleft()
            delay = 0.0
            if self.request_times:
                delay = max(delay, self.request_times[-1] + 0.21 - now)
            if len(self.request_times) >= 500:
                delay = max(delay, self.request_times[-500] + 60.001 - now)
            if len(self.request_times) >= 2000:
                delay = max(delay, self.request_times[-2000] + 1800.001 - now)
            if delay <= 0:
                self.request_times.append(now)
                return
            time.sleep(delay)

    def _get(self, url: str, authenticated: bool = True) -> requests.Response:
        """GET only known market-data URLs; five total attempts, no body logging."""
        if self.offline:
            raise CacheError("Offline mode forbids network requests.")
        if authenticated:
            if not (url.startswith(BASE_URL + "/v3/historical-candle/") or
                    url.startswith(BASE_URL + "/v2/historical-candle/")):
                raise APIError("Unsupported market-data URL.")
        elif url != MASTER_URL:
            raise APIError("Unsupported instrument-master URL.")
        session = self.session if authenticated else self.asset_session
        backoff = (1, 2, 4, 8, 16)
        for attempt in range(5):
            self._throttle()
            response: requests.Response | None = None
            try:
                response = session.get(url, timeout=(10, 45), allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError):
                failure = "Market-data request timed out or could not connect."
            except requests.RequestException:
                raise APIError("Market-data request failed; check local HTTP configuration.") from None
            else:
                status = response.status_code
                if status == 401:
                    response.close()
                    raise AuthenticationError(AUTH_MESSAGE)
                if status == 200:
                    return response
                response.close()
                if status != 429 and not 500 <= status <= 599:
                    raise APIError(f"Market-data HTTP {status}; not retried.", status)
                failure = f"Market-data HTTP {status}; retry budget exhausted."
            if attempt == 4:
                raise APIError(failure, response.status_code if response is not None else None)
            logging.warning("Transient market-data failure; retry %d of 4 in %ds.",
                            attempt + 1, backoff[attempt])
            time.sleep(backoff[attempt])
        raise APIError("Market-data request failed.")

    @staticmethod
    def _validate_master(payload: Any) -> list[dict[str, Any]]:
        """Validate relevant documented fields without inventing alternate schemas."""
        if not isinstance(payload, list) or not payload:
            raise ScreenerError("Instrument master schema: expected a nonempty JSON array.")
        for row in payload:
            if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) or not row[key]
                for key in ("segment", "instrument_type")
            ):
                raise ScreenerError("Instrument master schema: missing string segment/instrument_type.")
            if row["segment"] in {"NSE_EQ", "NSE_INDEX"}:
                fields = ["trading_symbol", "instrument_key", "name"]
                if row["segment"] == "NSE_EQ" and row["instrument_type"] == "EQ":
                    fields.append("isin")
                if any(not isinstance(row.get(key), str) or not row[key] for key in fields):
                    raise ScreenerError(
                        "Instrument master schema: expected name, trading_symbol, instrument_key "
                        "and equity isin strings; check current Upstox JSON documentation."
                    )
        return payload

    def instrument_master(self) -> list[dict[str, Any]]:
        """Use today's decompressed NSE master, or download the official gzip array."""
        path = self.master_dir / f"instruments_NSE_{self.today.isoformat()}.json"
        if path.exists() and not self.refresh:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                raise CacheError("Invalid instrument-master cache; refresh it online.") from None
            self.sources.add("instrument_master_cache")
            return self._validate_master(payload)
        if self.offline:
            raise CacheError("Offline instrument-master cache missing for today's IST date.")
        response = self._get(MASTER_URL, authenticated=False)
        try:
            content = response.content
            if content.startswith(b"\x1f\x8b"):
                content = gzip.decompress(content)
            payload = json.loads(content)
        except (OSError, EOFError, ValueError, UnicodeError):
            raise ScreenerError("Instrument master is not valid gzip/JSON.") from None
        finally:
            response.close()
        master = self._validate_master(payload)
        atomic_text(path, json.dumps(master, ensure_ascii=False))
        self.sources.add("instrument_master_download")
        return master

    @staticmethod
    def resolve_equity(master: Sequence[Mapping[str, Any]], symbol: str) -> str | None:
        """Resolve exactly one case-sensitive NSE EQ symbol; absence is reportable."""
        matches = [row["instrument_key"] for row in master
                   if row["segment"] == "NSE_EQ" and row["instrument_type"] == "EQ"
                   and row["trading_symbol"] == symbol]
        if len(matches) > 1:
            raise ScreenerError(f"Ambiguous instrument resolution for {symbol}; inspect the master.")
        return str(matches[0]) if matches else None

    @staticmethod
    def resolve_index(master: Sequence[Mapping[str, Any]]) -> str:
        """Resolve only the fixed benchmark by normalized name or symbol and key."""
        target = " ".join(BENCHMARK_NAME.casefold().split())
        matches = [row["instrument_key"] for row in master
                   if row["segment"] == "NSE_INDEX" and row["instrument_type"] == "INDEX"
                   and any(" ".join(str(row.get(key, "")).casefold().split()) == target
                           for key in ("name", "trading_symbol"))]
        if len(matches) != 1 or matches[0] != BENCHMARK_EXPECTED_KEY:
            raise ScreenerError("NIFTY 500 benchmark missing, ambiguous or unexpected key; aborting.")
        return str(matches[0])

    @staticmethod
    def _parse_candles_df(payload: Any) -> pd.DataFrame:
        """Parse aware timestamps as IST dates; return full OHLCV DataFrame."""
        try:
            if payload["status"] != "success":
                raise ValueError
            candles = payload["data"]["candles"]
            if not isinstance(candles, list):
                raise ValueError
            dates: list[pd.Timestamp] = []
            opens: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            closes: list[float] = []
            volumes: list[float] = []
            for row in candles:
                if not isinstance(row, list) or len(row) != 7:
                    raise ValueError
                timestamp = pd.Timestamp(row[0])
                if pd.isna(timestamp) or timestamp.tzinfo is None:
                    raise ValueError
                dates.append(timestamp.tz_convert(IST).tz_localize(None).normalize())
                opens.append(float(row[1]) if row[1] is not None else np.nan)
                highs.append(float(row[2]) if row[2] is not None else np.nan)
                lows.append(float(row[3]) if row[3] is not None else np.nan)
                closes.append(float(row[4]) if row[4] is not None else np.nan)
                volumes.append(float(row[5]) if row[5] is not None else 0.0)
            if not dates:
                return empty_ohlcv()
            df = pd.DataFrame(
                {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
                index=pd.DatetimeIndex(dates, name="date"),
            )
            return normalize_ohlcv(df)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise APIError("Unexpected candle schema; require seven fields and aware timestamps.") from None

    @staticmethod
    def _parse_candles(payload: Any) -> pd.Series:
        """Parse aware timestamps as IST dates; return close series."""
        df = UpstoxClient._parse_candles_df(payload)
        return df["close"] if not df.empty else empty_series()

    def _candles_df_at(self, path: str) -> pd.DataFrame:
        """Decode one sanitized API response as an OHLCV DataFrame."""
        response = self._get(BASE_URL + path)
        try:
            try:
                payload = response.json()
            except ValueError:
                raise APIError("Market-data response is not valid JSON.") from None
            return self._parse_candles_df(payload)
        finally:
            response.close()

    def _candles_at(self, path: str) -> pd.Series:
        """Decode one sanitized API response and return its close series."""
        df = self._candles_df_at(path)
        return df["close"] if not df.empty else empty_series()

    def daily_candles_df(self, key: str, from_date: date, to_date: date) -> pd.DataFrame:
        """Fetch V3 daily candles; fall back to deprecated V2 only on 404 or 410."""
        encoded = quote(key, safe="")
        try:
            df = self._candles_df_at(
                f"/v3/historical-candle/{encoded}/days/1/{to_date}/{from_date}"
            )
            self.sources.add("historical_v3")
        except APIError as error:
            if error.status not in {404, 410}:
                raise
            logging.warning("V3 historical endpoint unavailable; using documented V2 fallback.")
            df = self._candles_df_at(
                f"/v2/historical-candle/{encoded}/day/{to_date}/{from_date}"
            )
            self.sources.add("historical_v2_fallback")
        return df.loc[(df.index >= pd.Timestamp(from_date)) &
                      (df.index <= pd.Timestamp(to_date))].copy()

    def daily_candles(self, key: str, from_date: date, to_date: date) -> pd.Series:
        """Fetch V3 daily closes; fall back to deprecated V2 only on 404 or 410."""
        return self.daily_candles_df(key, from_date, to_date)["close"]

    def intraday_daily_df(self, key: str) -> pd.DataFrame:
        """Fetch the current-session V3 daily candle as OHLCV without historical fallback."""
        df = self._candles_df_at(
            f"/v3/historical-candle/intraday/{quote(key, safe='')}/days/1"
        )
        self.sources.add("intraday_v3")
        return df.loc[df.index == pd.Timestamp(self.today)].copy()

    def intraday_daily(self, key: str) -> pd.Series:
        """Fetch the current-session V3 daily candle without historical fallback."""
        return self.intraday_daily_df(key)["close"]

    def cached_ohlcv(self, symbol: str, key: str, from_date: date,
                     to_date: date, intraday: bool = False) -> pd.DataFrame:
        """Cache raw OHLCV data, isolating provisional observations from historical ones."""
        suffix = f"intraday__{to_date}" if intraday else str(to_date)
        path = self.cache_dir / f"{symbol}__{suffix}.csv"
        if path.exists() and not self.refresh:
            try:
                frame = pd.read_csv(path, dtype={"instrument_key": str}, float_precision="round_trip")
                expected_ohlcv = ["date", "open", "high", "low", "close", "volume", "instrument_key"]
                expected_close = ["date", "close", "instrument_key"]
                cols = list(frame.columns)
                if cols != expected_ohlcv and cols != expected_close:
                    raise ValueError
                if not frame.empty and not frame["instrument_key"].eq(key).all():
                    raise ValueError
                dates = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
                if cols == expected_ohlcv:
                    df = pd.DataFrame(
                        {
                            "open": pd.to_numeric(frame["open"], errors="raise").to_numpy(),
                            "high": pd.to_numeric(frame["high"], errors="raise").to_numpy(),
                            "low": pd.to_numeric(frame["low"], errors="raise").to_numpy(),
                            "close": pd.to_numeric(frame["close"], errors="raise").to_numpy(),
                            "volume": pd.to_numeric(frame["volume"], errors="raise").to_numpy(),
                        },
                        index=dates,
                    )
                    df = normalize_ohlcv(df)
                else:
                    # Legacy close-only cache
                    series = normalize_series(pd.Series(
                        pd.to_numeric(frame["close"], errors="raise").to_numpy(),
                        index=dates,
                    ))
                    df = pd.DataFrame({
                        "open": np.nan, "high": np.nan, "low": np.nan,
                        "close": series, "volume": np.nan,
                    }, index=series.index)
            except (OSError, ValueError, TypeError, UnicodeError, pd.errors.ParserError):
                raise CacheError(f"Invalid candle cache for {symbol}; refresh it online.") from None
            self.sources.add("intraday_cache" if intraday else "historical_cache")
        else:
            if self.offline:
                raise CacheError(f"Offline candle cache missing for {symbol} ({suffix}).")
            df = self.intraday_daily_df(key) if intraday else self.daily_candles_df(key, from_date, to_date)
            frame = df.reset_index()
            frame["instrument_key"] = key
            cols_to_save = ["date", "open", "high", "low", "close", "volume", "instrument_key"]
            atomic_text(path, frame.loc[:, cols_to_save].to_csv(index=False, date_format="%Y-%m-%d"))
        return df.loc[(df.index >= pd.Timestamp(from_date)) &
                      (df.index <= pd.Timestamp(to_date))].copy()

    def cached_candles(self, symbol: str, key: str, from_date: date,
                       to_date: date, intraday: bool = False) -> pd.Series:
        """Cache raw candle data, returning the series of daily closes."""
        return self.cached_ohlcv(symbol, key, from_date, to_date, intraday=intraday)["close"]


# =============================================================================
# Data assembly: calendar, alignment and ordered eligibility
# =============================================================================
def intraday_enabled(requested: date, now: datetime, include_intraday: bool) -> bool:
    """Guard optional current-session data; historical dates never use today's candle."""
    local_now = now.astimezone(IST)
    if not include_intraday:
        return False
    if requested != local_now.date():
        logging.warning("Ignoring --include-intraday: --as-of is not today's IST date.")
        return False
    if local_now.time() < wall_time(15, 45):
        logging.warning("Ignoring --include-intraday: the IST time is before 15:45.")
        return False
    logging.warning("PROVISIONAL=True: Upstox may still revise the current-session close.")
    return True


def fetch_ohlcv(client: UpstoxClient, symbol: str, key: str,
                requested: date, provisional: bool) -> pd.DataFrame:
    """Fetch OHLCV history; exclude today's row unless provisional intraday is enabled."""
    start = requested - timedelta(days=FETCH_CALENDAR_DAYS)
    historical = client.cached_ohlcv(symbol, key, start, requested)
    historical = historical.loc[historical.index < pd.Timestamp(client.today)]
    if provisional:
        current = client.cached_ohlcv(symbol, key, requested, requested, intraday=True)
        current = current.loc[current.index == pd.Timestamp(requested)]
        historical = pd.concat([historical, current])
    return normalize_ohlcv(historical.loc[historical.index <= pd.Timestamp(requested)])


def fetch_series(client: UpstoxClient, symbol: str, key: str,
                 requested: date, provisional: bool) -> pd.Series:
    """Exclude today's historical row; include it only via the guarded separate cache."""
    return fetch_ohlcv(client, symbol, key, requested, provisional)["close"]


def validate_benchmark(bench: pd.Series) -> pd.Series:
    """Require enough benchmark rows and valid closes in its evaluation window."""
    result = normalize_series(bench)
    if len(result) < MIN_HISTORY_DAYS:
        raise ScreenerError("Benchmark has fewer than MIN_HISTORY_DAYS daily rows; aborting.")
    window = result.iloc[-MIN_HISTORY_DAYS:]
    if not np.isfinite(window.to_numpy()).all() or (window <= 0).any():
        raise ScreenerError("Benchmark contains invalid closes in the evaluation window.")
    return result


def assemble_data(raw: Mapping[str, pd.Series], bench: pd.Series,
                  instrument_keys: Mapping[str, str | None],
                  fetch_failures: Mapping[str, str],
                  symbols: Sequence[str] = tuple(UNIVERSE)) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align to benchmark dates and return eligible windows plus all-symbol diagnostics.

    NaNs caused by absent sessions are gaps. Explicit invalid vendor closes are
    never repaired into eligibility. At most one leading-window gap is seeded
    from the latest earlier aligned observation, after checking total gap count.
    No value from after the benchmark's final date is consulted.
    """
    benchmark = validate_benchmark(bench)
    calendar = benchmark.index
    window_index = calendar[-MIN_HISTORY_DAYS:]
    effective = calendar[-1]
    cleaned: dict[str, pd.Series] = {}
    diagnostics: list[dict[str, Any]] = []
    for symbol in symbols:
        key = instrument_keys.get(symbol)
        source = normalize_series(raw.get(symbol, empty_series()))
        source = source.loc[source.index <= effective]
        aligned = source.reindex(calendar)
        window = aligned.iloc[-MIN_HISTORY_DAYS:].copy()
        missing = int(window.isna().sum())
        record: dict[str, Any] = {
            "Symbol": symbol, "ELIGIBLE": False, "INELIGIBLE_REASON": "",
            "MISSING_DAYS": missing if key and symbol in raw else None,
            "FIRST_DATE": source.index[0].date().isoformat() if len(source) else None,
            "LAST_DATE": source.index[-1].date().isoformat() if len(source) else None,
            "N_ROWS": len(source), "SUSPECT_CORP_ACTION": False,
            "instrument_key": key,
        }
        # Diagnostic only: use observed adjacent calendar prices for excluded rows.
        observed_returns = window.pct_change(fill_method=None).iloc[1:]
        record["SUSPECT_CORP_ACTION"] = bool((observed_returns.abs() > CORP_ACTION_JUMP).any())
        prior = aligned.iloc[:-MIN_HISTORY_DAYS].dropna()
        seed = prior.iloc[-1] if len(prior) else np.nan
        if not key:
            reason = "UNRESOLVED"
        elif symbol in fetch_failures:
            reason = "FETCH_FAILED"
        elif pd.isna(window.iloc[-1]):
            reason = "STALE"
        elif pd.isna(window.iloc[0]) and pd.isna(seed):
            reason = "INSUFFICIENT_HISTORY"
        elif missing > MAX_MISSING_DAYS:
            reason = "TOO_MANY_GAPS"
        else:
            explicit = source.reindex(window_index.intersection(source.index))
            invalid_observed = bool((~np.isfinite(explicit.to_numpy())).any() or (explicit <= 0).any())
            if pd.isna(window.iloc[0]):
                window.iloc[0] = seed
            window = window.ffill()
            bad = invalid_observed or not np.isfinite(window.to_numpy()).all() or bool((window <= 0).any())
            if not bad:
                with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                    vols = [realised_vol(window, n) for n in HORIZONS]
                    rets = [n_day_return(window, n) for n in HORIZONS]
                bad = any(not math.isfinite(v) or v <= 0 for v in vols)
                bad = bad or any(not math.isfinite(r) for r in rets)
                if not bad:
                    bad = any(not math.isfinite(r / v) for r, v in zip(rets, vols))
            reason = "BAD_DATA" if bad else ""
            if not reason:
                cleaned[symbol] = window
                record["SUSPECT_CORP_ACTION"] = bool(
                    (window.pct_change(fill_method=None).iloc[1:].abs() > CORP_ACTION_JUMP).any()
                )
        record["INELIGIBLE_REASON"] = reason
        record["ELIGIBLE"] = not reason
        diagnostics.append(record)
    return (pd.DataFrame(cleaned, index=window_index),
            pd.DataFrame(diagnostics).set_index("Symbol"))


def require_eligible_count(count: int) -> None:
    """Apply the fixed-universe denominator and absolute minimum cross-section."""
    if count < 2 or count / len(UNIVERSE) < MIN_ELIGIBLE_FRACTION:
        raise ScreenerError(
            f"Insufficient eligible universe: {count}/{len(UNIVERSE)}; "
            f"require at least {math.ceil(MIN_ELIGIBLE_FRACTION * len(UNIVERSE))} and at least 2."
        )


# =============================================================================
# Pure metrics, flags and cross-sectional composite (no I/O)
# =============================================================================
def n_day_return(closes: pd.Series, n: int) -> float:
    """Simple return using exactly n return intervals and n+1 terminal closes."""
    if n < 1 or len(closes) < n + 1:
        raise ValueError("Not enough closes for the requested return horizon.")
    return float(closes.iloc[-1] / closes.iloc[-n - 1] - 1.0)


def realised_vol(closes: pd.Series, n: int) -> float:
    """Sample standard deviation of exactly n simple daily returns, annualized if configured."""
    if n < 2 or len(closes) < n + 1:
        raise ValueError("Not enough closes for the requested volatility horizon.")
    values = closes.iloc[-n - 1:].to_numpy(dtype=float)
    daily = values[1:] / values[:-1] - 1.0
    scale = math.sqrt(252) if VOL_ANNUALISE else 1.0
    return float(np.std(daily, ddof=1) * scale)


def pct(values: pd.Series) -> pd.Series:
    """Average-rank percentiles on nonmissing values; fewer than two is undefined."""
    values = values.astype(float)
    count = int(values.notna().sum())
    if count < 2:
        return pd.Series(np.nan, index=values.index, name=values.name, dtype=float)
    return 100.0 * (values.rank(method="average", ascending=True) - 1.0) / (count - 1)


def apply_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Compute every frozen flag on a copy, retaining unrounded numerical boundaries."""
    result = df.copy(deep=True)
    result["MARKET_OUTPERFORMER"] = (result["MR126"] > 0) & (result["MR252"] > 0)
    result["STRONG_TREND"] = ((result["R126"] > 0) & (result["R252"] > 0) &
                              (result["POS_COUNT"] >= POS_COUNT_MIN))
    result["STRONG_XS_MOMENTUM"] = ((result["Rank126"] >= P_STRONG) &
                                    (result["Rank252"] >= P_STRONG))
    result["VERY_STRONG_MOMENTUM"] = ((result["Rank126"] >= P_VERY_STRONG) &
                                      (result["Rank252"] >= P_VERY_STRONG))
    common = result["MARKET_OUTPERFORMER"] & result["STRONG_TREND"]
    result["CORE_QUALIFIER"] = common & result["STRONG_XS_MOMENTUM"]
    result["FINAL_QUALIFIER"] = common & result["VERY_STRONG_MOMENTUM"]
    return result


def composite_score(df: pd.DataFrame) -> pd.DataFrame:
    """Score all eligible rows before filtering any qualifier; never mutate the input."""
    result = df.copy(deep=True)
    for name in ("MR252", "MR126", "RAM252"):
        result[f"pct_{name}"] = pct(result[name])
    total = pd.Series(0.0, index=result.index)
    for name, weight in COMPOSITE_WEIGHTS.items():
        total = total + weight * result[name]
    result["FINAL_SCREEN_RANK"] = total / sum(COMPOSITE_WEIGHTS.values())
    return result


def compute_metrics(closes: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    """Return all metrics/flags/scores from an already eligible, aligned close matrix.

    The caller selects the point-in-time window: closes.loc[:D] and bench.loc[:D].
    Both inputs must have exactly the same sorted unique daily calendar. Only
    their last 253 rows are used. Inputs are never filled, modified or fetched.
    At least two eligible columns are required; the CLI separately enforces 50%.
    """
    if closes.shape[1] < 2 or closes.columns.has_duplicates:
        raise ValueError("Metrics require at least two uniquely named eligible symbols.")
    if (not isinstance(closes.index, pd.DatetimeIndex) or
            not closes.index.equals(bench.index) or
            not closes.index.is_monotonic_increasing or closes.index.has_duplicates or
            closes.index.hasnans or len(closes) < MIN_HISTORY_DAYS):
        raise ValueError("Metrics require matching sorted unique calendars with at least 253 rows.")
    matrix = closes.iloc[-MIN_HISTORY_DAYS:]
    benchmark = bench.iloc[-MIN_HISTORY_DAYS:]
    for values in (matrix.to_numpy(dtype=float), benchmark.to_numpy(dtype=float)):
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError("Metrics require finite positive, gap-free aligned closes.")
    records: list[dict[str, Any]] = []
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        benchmark_returns = {n: n_day_return(benchmark, n) for n in HORIZONS}
    if not all(math.isfinite(value) for value in benchmark_returns.values()):
        raise ValueError("Metrics require finite benchmark returns.")
    for symbol in matrix.columns:
        series = matrix[symbol]
        row: dict[str, Any] = {"Symbol": symbol, "Close": float(series.iloc[-1])}
        for n in HORIZONS:
            r = n_day_return(series, n)
            vol = realised_vol(series, n)
            if not math.isfinite(vol) or vol <= 0 or not math.isfinite(r):
                raise ValueError("Metrics require finite returns and nonzero finite volatility.")
            ram = r / vol
            if not math.isfinite(ram):
                raise ValueError("Metrics require finite RAM values.")
            row.update({f"R{n}": r, f"Vol{n}": vol, f"RAM{n}": ram,
                        f"TS{n}": r, f"TS{n}_POS": r > 0,
                        f"B{n}": benchmark_returns[n], f"MR{n}": r - benchmark_returns[n]})
        row["POS_COUNT"] = sum(bool(row[f"TS{n}_POS"]) for n in HORIZONS)
        records.append(row)
    result = pd.DataFrame(records).set_index("Symbol")
    for n in HORIZONS:
        result[f"Rank{n}"] = pct(result[f"{RANK_BASIS}{n}"])
    return composite_score(apply_flags(result))


# =============================================================================
# Stage 2 Buying checks, metrics, percentiles and scoring
# =============================================================================
def compute_wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute Wilder Average True Range.

    TR[0] = High[0] - Low[0]
    TR[i] = max(High[i] - Low[i], abs(High[i] - Close[i-1]), abs(Low[i] - Close[i-1]))
    ATR is seeded as the arithmetic mean of the first `period` TRs (index period - 1).
    ATR[i] = (ATR[i-1] * (period - 1) + TR[i]) / period for i >= period.
    """
    n = len(high)
    if n < period or len(low) != n or len(close) != n:
        return np.full(n, np.nan, dtype=float)
    tr = np.zeros(n, dtype=float)
    tr[0] = float(high[0] - low[0])
    for i in range(1, n):
        tr[i] = float(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    atr = np.full(n, np.nan, dtype=float)
    atr[period - 1] = float(np.mean(tr[:period]))
    for i in range(period, n):
        atr[i] = float((atr[i - 1] * (period - 1) + tr[i]) / period)
    return atr


def buying_candidate_percentile(values: pd.Series) -> pd.Series:
    """Percentile rank in [0, 100] calculated ONLY among passing buying candidates."""
    values = values.astype(float)
    count = int(values.notna().sum())
    if count == 0:
        return pd.Series(dtype=float, index=values.index)
    if count == 1:
        # Neutral midpoint when a single candidate has no peers to rank against
        return pd.Series(50.0, index=values.index, dtype=float)
    if values.max() == values.min():
        return pd.Series(50.0, index=values.index, dtype=float)
    ranks = values.rank(method="average", ascending=True)
    return 100.0 * (ranks - 1.0) / (count - 1.0)


def evaluate_buying_checks(symbol: str, stock_ohlcv: pd.DataFrame,
                           benchmark_closes: pd.Series) -> dict[str, Any] | None:
    """Evaluate the 5 buying checks for one qualifier against the benchmark calendar.

    1. Close[T] > HH20[T]
    2. RS20[T] > 0
    3. RS60[T] > 0
    4. RVOL20[T] >= 1.5
    5. 0.0 <= BreakoutDistance20[T] <= 1.5 (Wilder ATR14 through T-1)
    """
    aligned = stock_ohlcv.reindex(benchmark_closes.index)
    min_required = max(RS60_PERIOD, HH_PERIOD, ATR_PERIOD) + 1  # 61 sessions
    if len(aligned) < min_required:
        return None

    close_t = float(aligned["close"].iloc[-1])
    high_t = float(aligned["high"].iloc[-1])
    low_t = float(aligned["low"].iloc[-1])
    vol_t = float(aligned["volume"].iloc[-1])

    if not (math.isfinite(close_t) and close_t > 0 and math.isfinite(high_t) and
            math.isfinite(low_t) and math.isfinite(vol_t)):
        return None

    # Prior 20 sessions (T-20 to T-1, index iloc[-21:-1])
    prior_highs = aligned["high"].iloc[-HH_PERIOD - 1:-1].to_numpy(dtype=float)
    prior_vols = aligned["volume"].iloc[-HH_PERIOD - 1:-1].to_numpy(dtype=float)
    if len(prior_highs) != HH_PERIOD or not np.isfinite(prior_highs).all():
        return None
    if len(prior_vols) != HH_PERIOD or not np.isfinite(prior_vols).all():
        return None

    hh20 = float(np.max(prior_highs))
    median_vol20 = float(np.median(prior_vols))
    rvol20 = float(vol_t / median_vol20) if median_vol20 > 0 else np.nan

    # RS20 and RS60
    stock_close_20 = float(aligned["close"].iloc[-RS20_PERIOD - 1])
    bench_close_20 = float(benchmark_closes.iloc[-RS20_PERIOD - 1])
    bench_close_t = float(benchmark_closes.iloc[-1])
    if stock_close_20 <= 0 or bench_close_20 <= 0 or bench_close_t <= 0:
        return None
    rs20 = float((close_t / stock_close_20 - 1.0) - (bench_close_t / bench_close_20 - 1.0))

    stock_close_60 = float(aligned["close"].iloc[-RS60_PERIOD - 1])
    bench_close_60 = float(benchmark_closes.iloc[-RS60_PERIOD - 1])
    if stock_close_60 <= 0 or bench_close_60 <= 0:
        return None
    rs60 = float((close_t / stock_close_60 - 1.0) - (bench_close_t / bench_close_60 - 1.0))

    # Wilder ATR14 through session T-1 (excludes session T)
    high_hist = aligned["high"].iloc[:-1].to_numpy(dtype=float)
    low_hist = aligned["low"].iloc[:-1].to_numpy(dtype=float)
    close_hist = aligned["close"].iloc[:-1].to_numpy(dtype=float)
    atr_series = compute_wilder_atr(high_hist, low_hist, close_hist, period=ATR_PERIOD)
    atr14_prev = float(atr_series[-1])

    breakout_dist = (
        float((close_t - hh20) / atr14_prev)
        if (math.isfinite(atr14_prev) and atr14_prev > 0)
        else np.nan
    )

    hl_range = high_t - low_t
    close_loc = 0.5 if hl_range == 0.0 or not math.isfinite(hl_range) else float((close_t - low_t) / hl_range)

    pass_breakout = bool(close_t > hh20)
    pass_rs20 = bool(rs20 > 0.0)
    pass_rs60 = bool(rs60 > 0.0)
    pass_rvol = bool(math.isfinite(rvol20) and rvol20 >= RVOL20_MIN)
    pass_dist = bool(
        math.isfinite(breakout_dist)
        and BREAKOUT_DISTANCE20_MIN <= breakout_dist <= BREAKOUT_DISTANCE20_MAX
    )
    passed_all = bool(pass_breakout and pass_rs20 and pass_rs60 and pass_rvol and pass_dist)

    return {
        "Symbol": symbol,
        "Close": close_t,
        "HH20": hh20,
        "RS20": rs20,
        "RS60": rs60,
        "RVOL20": rvol20,
        "ATR14": atr14_prev,
        "BreakoutDistance20": breakout_dist,
        "CloseLocation": close_loc,
        "PASS_BREAKOUT": pass_breakout,
        "PASS_RS20": pass_rs20,
        "PASS_RS60": pass_rs60,
        "PASS_RVOL20": pass_rvol,
        "PASS_DISTANCE": pass_dist,
        "PASSED_ALL": passed_all,
    }


def compute_buying_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Compute cross-sectional percentiles and ENTRY_QUALITY_SCORE for passing candidates."""
    if df.empty:
        return pd.DataFrame(columns=BUYING_REPORT_COLUMNS)
    result = df.copy(deep=True)
    result["PRS20"] = buying_candidate_percentile(result["RS20"])
    result["PRS60"] = buying_candidate_percentile(result["RS60"])
    result["PRVOL20"] = buying_candidate_percentile(result["RVOL20"])
    result["PQuality"] = buying_candidate_percentile(result["CloseLocation"])
    result["PExtension"] = buying_candidate_percentile(result["BreakoutDistance20"])
    result["ExtensionScore"] = 100.0 - result["PExtension"]

    w1 = BUYING_SCORE_WEIGHTS["RS20"]            # 0.20
    w2 = BUYING_SCORE_WEIGHTS["RS60"]            # 0.20
    w4 = BUYING_SCORE_WEIGHTS["RVOL20"]          # 0.15
    w5 = BUYING_SCORE_WEIGHTS["BreakoutQuality"] # 0.15
    w6 = BUYING_SCORE_WEIGHTS["Extension"]       # 0.15
    sum_active_weights = w1 + w2 + w4 + w5 + w6  # 0.85

    result["RAW_SCORE"] = (
        w1 * result["PRS20"]
        + w2 * result["PRS60"]
        + w4 * result["PRVOL20"]
        + w5 * result["PQuality"]
        - w6 * result["PExtension"]
    )

    unscaled_score = (
        w1 * result["PRS20"]
        + w2 * result["PRS60"]
        + w4 * result["PRVOL20"]
        + w5 * result["PQuality"]
        + w6 * result["ExtensionScore"]
    )
    result["ENTRY_QUALITY_SCORE"] = unscaled_score / sum_active_weights

    result = result.sort_values(
        ["ENTRY_QUALITY_SCORE", "RS20", "Symbol"],
        ascending=[False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)

    return result.loc[:, list(BUYING_REPORT_COLUMNS)].copy()


def evaluate_buying_candidates(metrics: pd.DataFrame,
                               raw_ohlcv: Mapping[str, pd.DataFrame],
                               aligned_bench: pd.Series) -> pd.DataFrame:
    """Run stage-2 buying checks on FINAL_QUALIFIER stocks only."""
    qualifiers = metrics.loc[metrics["FINAL_QUALIFIER"]]
    if qualifiers.empty:
        return pd.DataFrame(columns=BUYING_REPORT_COLUMNS)
    records: list[dict[str, Any]] = []
    for symbol in qualifiers.index:
        stock_ohlcv = raw_ohlcv.get(symbol)
        if stock_ohlcv is None or stock_ohlcv.empty:
            logging.warning("No OHLCV candle data available for qualifier %s; skipped.", symbol)
            continue
        eval_res = evaluate_buying_checks(symbol, stock_ohlcv, aligned_bench)
        if eval_res is not None and eval_res["PASSED_ALL"]:
            records.append(eval_res)
    if not records:
        return pd.DataFrame(columns=BUYING_REPORT_COLUMNS)
    passed_df = pd.DataFrame(records)
    return compute_buying_scores(passed_df)


def console_buying_tables(buying_results: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """Build readable display table for entry quality score components."""
    if buying_results.empty:
        return []
    display = buying_results.copy(deep=True)
    display.insert(0, "#", range(1, len(display) + 1))
    display["PRS20"] = display["PRS20"].map(lambda val: f"{val:.2f}")
    display["PRS60"] = display["PRS60"].map(lambda val: f"{val:.2f}")
    display["PRVOL20"] = display["PRVOL20"].map(lambda val: f"{val:.2f}")
    display["PQuality"] = display["PQuality"].map(lambda val: f"{val:.2f}")
    display["PExtension"] = display["PExtension"].map(lambda val: f"{val:.2f}")
    display["ExtScore"] = display["ExtensionScore"].map(lambda val: f"{val:.2f}")
    display["Score"] = display["ENTRY_QUALITY_SCORE"].map(lambda val: f"{val:.2f}")

    table2_cols = ["#", "Symbol", "PRS20", "PRS60", "PRVOL20", "PQuality", "PExtension", "ExtScore", "Score"]

    return [
        ("BUYING CHECKS | ENTRY QUALITY SCORE COMPONENTS (Percentiles among passing candidates)",
         display.loc[:, table2_cols].copy()),
    ]


# =============================================================================
# Output writers and final console report
# =============================================================================
def frozen_parameters() -> dict[str, Any]:
    """Return a serializable copy of the entire frozen parameter block."""
    return {
        "HORIZONS": list(HORIZONS), "MIN_HISTORY_DAYS": MIN_HISTORY_DAYS,
        "MAX_MISSING_DAYS": MAX_MISSING_DAYS, "MIN_ELIGIBLE_FRACTION": MIN_ELIGIBLE_FRACTION,
        "FETCH_CALENDAR_DAYS": FETCH_CALENDAR_DAYS, "VOL_ANNUALISE": VOL_ANNUALISE,
        "RANK_BASIS": RANK_BASIS, "P_STRONG": P_STRONG, "P_VERY_STRONG": P_VERY_STRONG,
        "POS_COUNT_MIN": POS_COUNT_MIN, "COMPOSITE_WEIGHTS": dict(COMPOSITE_WEIGHTS),
        "CORP_ACTION_JUMP": CORP_ACTION_JUMP, "BENCHMARK_NAME": BENCHMARK_NAME,
        "BENCHMARK_EXPECTED_KEY": BENCHMARK_EXPECTED_KEY,
        "RVOL20_MIN": RVOL20_MIN,
        "BREAKOUT_DISTANCE20_MIN": BREAKOUT_DISTANCE20_MIN,
        "BREAKOUT_DISTANCE20_MAX": BREAKOUT_DISTANCE20_MAX,
        "ATR_PERIOD": ATR_PERIOD,
        "HH_PERIOD": HH_PERIOD,
        "RS20_PERIOD": RS20_PERIOD,
        "RS60_PERIOD": RS60_PERIOD,
        "BUYING_SCORE_WEIGHTS": dict(BUYING_SCORE_WEIGHTS),
    }


def final_screen(metrics: pd.DataFrame) -> pd.DataFrame:
    """Select qualifiers, sort using full precision, and enforce the exact CSV schema."""
    rows = metrics.loc[metrics["FINAL_QUALIFIER"]].reset_index()
    rows = rows.sort_values(["FINAL_SCREEN_RANK", "Rank252", "Symbol"],
                            ascending=[False, False, True], kind="mergesort")
    return rows.loc[:, list(SCREEN_COLUMNS)].reset_index(drop=True)


def format_for_file(frame: pd.DataFrame) -> pd.DataFrame:
    """Render floats at declared precision without changing computations or flags."""
    result = frame.copy(deep=True)
    for column in result.columns:
        if column in {"Close", "HH20", "ATR14"}:
            digits = 2
        elif column.startswith(("Rank", "pct_", "P", "Ext")) or column in {
            "FINAL_SCREEN_RANK", "ENTRY_QUALITY_SCORE", "RAW_SCORE",
            "CloseLocation", "BreakoutDistance20", "RVOL20"
        }:
            digits = 2
        elif pd.api.types.is_float_dtype(result[column]):
            digits = 6
        else:
            continue
        result[column] = result[column].map(
            lambda value, d=digits: "" if pd.isna(value) else f"{value:.{d}f}"
        )
    return result


def build_universe(metrics: pd.DataFrame, diagnostics: pd.DataFrame,
                   bench: pd.Series, provisional: bool) -> pd.DataFrame:
    """Retain every universe member; excluded metrics/flags remain explicitly unavailable."""
    result = diagnostics.join(metrics, how="left")
    # Benchmark returns exist independently of stock eligibility.
    for n in HORIZONS:
        result[f"B{n}"] = n_day_return(bench, n)
        result[f"TS{n}_POS"] = result[f"TS{n}_POS"].astype("boolean")
    for name in FLAG_COLUMNS:
        result[name] = result[name].astype("boolean")
    for name in ("POS_COUNT", "MISSING_DAYS", "N_ROWS"):
        result[name] = result[name].astype("Int64")
    result["PROVISIONAL"] = provisional
    result = result.reset_index()
    front = list(SCREEN_COLUMNS)
    remaining = [name for name in result.columns if name not in front]
    return result.loc[:, front + remaining]


def build_run_metadata(metrics: pd.DataFrame, diagnostics: pd.DataFrame,
                       bench: pd.Series, requested: date, provisional: bool,
                       sources: Sequence[str], timestamp: datetime,
                       buying_results: pd.DataFrame | None = None) -> dict[str, Any]:
    """Build an auditable run record with no credentials and no nonstandard JSON values."""
    effective = bench.index[-1].date().isoformat()
    exclusions = [{"Symbol": str(symbol), "reason": str(row["INELIGIBLE_REASON"])}
                  for symbol, row in diagnostics.iterrows() if not row["ELIGIBLE"]]
    flag_counts = (
        {name: int(metrics[name].sum()) if name in metrics else None for name in FLAG_COLUMNS}
        if not metrics.empty
        else {name: None for name in FLAG_COLUMNS}
    )
    output_labels: dict[str, Any] = {
        f"screen_{effective}.csv": {"PROVISIONAL": provisional},
        f"universe_{effective}.csv": {"PROVISIONAL": provisional},
        f"run_{effective}.json": {"PROVISIONAL": provisional},
    }
    if buying_results is not None:
        output_labels[f"buying_checks_{effective}.csv"] = {"PROVISIONAL": provisional}

    meta: dict[str, Any] = {
        "as_of_requested": requested.isoformat(), "as_of_effective": effective,
        "provisional": provisional,
        "benchmark": {"name": BENCHMARK_NAME, "instrument_key": BENCHMARK_EXPECTED_KEY,
                      **{f"B{n}": round(n_day_return(bench, n), 6) for n in HORIZONS}},
        "frozen_parameters": frozen_parameters(), "rank_basis": RANK_BASIS,
        "counts": {"universe": len(diagnostics),
                   "resolved": int(diagnostics["instrument_key"].notna().sum()),
                   "eligible": int(diagnostics["ELIGIBLE"].sum()),
                   "per_flag": flag_counts,
                   "core": flag_counts["CORE_QUALIFIER"], "final": flag_counts["FINAL_QUALIFIER"]},
        "unresolved": [item["Symbol"] for item in exclusions if item["reason"] == "UNRESOLVED"],
        "ineligible": exclusions,
        "data_source": {"provider": "Upstox", "base_url": BASE_URL,
                        "instrument_master_url": MASTER_URL, "access_paths": sorted(sources)},
        "adjustment_note": ADJUSTMENT_NOTE,
        "timestamp_utc": timestamp.astimezone(timezone.utc).isoformat(),
        "data_window": {"first_date": bench.index[-MIN_HISTORY_DAYS].date().isoformat(),
                        "last_date": effective, "rows": MIN_HISTORY_DAYS,
                        "master_first_date": bench.index[0].date().isoformat(),
                        "master_rows": len(bench),
                        "fetch_from": (requested - timedelta(days=FETCH_CALENDAR_DAYS)).isoformat(),
                        "fetch_to": requested.isoformat()},
        "output_labels": output_labels,
    }
    if buying_results is not None:
        meta["buying_checks"] = {
            "qualifiers_count": flag_counts["FINAL_QUALIFIER"] if flag_counts["FINAL_QUALIFIER"] is not None else 0,
            "passed_count": len(buying_results),
            "parameters": {
                "RVOL20_MIN": RVOL20_MIN,
                "BREAKOUT_DISTANCE20_MIN": BREAKOUT_DISTANCE20_MIN,
                "BREAKOUT_DISTANCE20_MAX": BREAKOUT_DISTANCE20_MAX,
                "ATR_PERIOD": ATR_PERIOD,
                "HH_PERIOD": HH_PERIOD,
                "RS20_PERIOD": RS20_PERIOD,
                "RS60_PERIOD": RS60_PERIOD,
                "BUYING_SCORE_WEIGHTS": dict(BUYING_SCORE_WEIGHTS),
            },
            "candidates": [
                {
                    "Symbol": str(row["Symbol"]),
                    "ENTRY_QUALITY_SCORE": round(float(row["ENTRY_QUALITY_SCORE"]), 2),
                    "RAW_SCORE": round(float(row["RAW_SCORE"]), 2),
                    "Close": round(float(row["Close"]), 2),
                    "HH20": round(float(row["HH20"]), 2),
                    "RS20": round(float(row["RS20"]), 6),
                    "RS60": round(float(row["RS60"]), 6),
                    "RVOL20": round(float(row["RVOL20"]), 2),
                    "ATR14": round(float(row["ATR14"]), 2),
                    "BreakoutDistance20": round(float(row["BreakoutDistance20"]), 2),
                    "CloseLocation": round(float(row["CloseLocation"]), 2),
                }
                for _, row in buying_results.iterrows()
            ] if not buying_results.empty else [],
        }
    return meta


def write_outputs(out_dir: Path, metrics: pd.DataFrame, diagnostics: pd.DataFrame,
                  bench: pd.Series, metadata: Mapping[str, Any],
                  buying_results: pd.DataFrame | None = None) -> tuple[Path, ...]:
    """Write screen, universe, optional buying checks CSVs, then the run manifest."""
    effective = str(metadata["as_of_effective"])
    screen_path = out_dir / f"screen_{effective}.csv"
    universe_path = out_dir / f"universe_{effective}.csv"
    run_path = out_dir / f"run_{effective}.json"
    buying_path = out_dir / f"buying_checks_{effective}.csv"

    screen = format_for_file(final_screen(metrics))
    universe = format_for_file(build_universe(metrics, diagnostics, bench, bool(metadata["provisional"])))

    # Serialize before changing any existing report.
    screen_text = screen.to_csv(index=False)
    universe_text = universe.to_csv(index=False, na_rep="")
    atomic_text(screen_path, screen_text)
    atomic_text(universe_path, universe_text)

    paths = [screen_path, universe_path]
    if buying_results is not None:
        buying_fmt = format_for_file(buying_results)
        atomic_text(buying_path, buying_fmt.to_csv(index=False))
        paths.append(buying_path)

    run_text = json.dumps(dict(metadata), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    atomic_text(run_path, run_text)
    paths.append(run_path)
    return tuple(paths)


def console_tables(metrics: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """Build readable display-only table for final qualifiers overall strength."""
    if metrics.empty or "FINAL_QUALIFIER" not in metrics.columns:
        return []
    rows = final_screen(metrics)
    if rows.empty:
        return []
    display = rows.copy(deep=True)
    display.insert(0, "#", range(1, len(display) + 1))
    display["Close"] = display["Close"].map(lambda value: f"{value:,.2f}")
    display["POS_COUNT"] = display["POS_COUNT"].map(lambda value: f"{int(value)}/4")
    display["FINAL_SCREEN_RANK"] = display["FINAL_SCREEN_RANK"].map(lambda value: f"{value:.2f}")
    groups = (
        ("1. OVERALL STRENGTH | Close in INR; score out of 100",
         ["Close", "POS_COUNT", "FINAL_SCREEN_RANK"]),
    )
    return [(title, display.loc[:, ["#", "Symbol", *columns]].copy())
            for title, columns in groups]


def console_legend() -> str:
    """Give every screen column a brief plain-language explanation, without I/O."""
    return "\n".join((
        "QUICK COLUMN GUIDE",
        "  Symbol : Stock code.       Close : Latest closing price in INR.",
        "  21 / 63 / 126 / 252 : Trading days; about 1 / 3 / 6 / 12 months.",
        "  R21/R63/R126/R252 : Price gain or loss over each period (shown as %).",
        "  RAM21/RAM63/RAM126/RAM252 : Return divided by price swings (volatility).",
        "  Rank21/Rank63/Rank126/Rank252 : Rank among eligible stocks; higher is stronger.",
        "  MR21/MR63/MR126/MR252 : Stock return minus NIFTY 500 return (shown as pp).",
        "  POS_COUNT : How many of the four periods have a positive return (out of 4).",
        "  FINAL_SCREEN_RANK : Combined strength score, 0-100; higher appears first.",
        "  STAGE 2 BUYING CHECKS (only evaluated on FINAL_QUALIFIER stocks):",
        "    HH20 : Highest high of the previous 20 completed sessions (excludes today).",
        "    RS20 / RS60 : 20-day / 60-day relative strength vs NIFTY 500 (requires > 0).",
        "    RVOL20 : Today volume vs 20-day median volume (requires >= 1.5x).",
        "    ATR14 : Wilder 14-day ATR calculated through session T-1.",
        "    BreakoutDist : (Close - HH20) / ATR14 (requires 0.0 to 1.5 ATRs).",
        "    CloseLoc : (Close - Low) / (High - Low) on session T (breakout quality).",
        "    ENTRY_QUALITY_SCORE : Normalized 0-100 score among passing buying candidates.",
        "  Example: +5.00 pp means 5 percentage points ahead of NIFTY 500.",
        "  Scores are comparisons, not probabilities or promises of future gains.",
        "  Console: % / pp. CSV and JSON: returns remain decimal fractions.",
    ))


def console_report(metadata: Mapping[str, Any], metrics: pd.DataFrame | None = None,
                   aborted: str | None = None, out_dir: Path | None = None,
                   buying_results: pd.DataFrame | None = None) -> None:
    """The only application print site: displays only the two requested stock selection tables."""
    divider = "-" * 88
    if aborted:
        print("\n" + divider)
        print("SCREEN NOT COMPLETED")
        print(textwrap.fill(aborted, width=88))
        print(divider + "\n")
        return

    if metrics is not None:
        tables = console_tables(metrics)
        print("\n" + divider)
        print("FINAL QUALIFIERS | STRONGEST FIRST")
        print("Sorted by FINAL_SCREEN_RANK, then Rank252, then stock code for ties.")
        print("Only stocks passing the final rules are shown; #1 has the highest composite score.")
        if not tables:
            print("\nNo stocks passed the final screening rules.")
        else:
            for title, table in tables:
                print("\n" + title)
                print(divider)
                print(table.to_string(index=False, justify="right"))

    if buying_results is not None:
        print("\n" + divider)
        print("BUYING CHECKS (CONFIRMED BREAKOUTS) | STAGE 2")
        print("Filters: Close[T] > HH20[T], RS20 > 0, RS60 > 0, RVOL20 >= 1.5, 0.0 <= BreakoutDist <= 1.5")
        print("Score: 20% RS20 + 20% RS60 + 15% RVOL20 + 15% CloseLocation + 15% (100 - BreakoutDist)")
        print(divider)
        if buying_results.empty:
            print("\nNone of the stocks passes the buying checks.")
        else:
            buying_tables = console_buying_tables(buying_results)
            for title, table in buying_tables:
                print("\n" + title)
                print(divider)
                print(table.to_string(index=False, justify="right"))
    print()


# =============================================================================
# Telegram notifications
# =============================================================================
def strip_html_tags(text: str) -> str:
    """Fallback plain text stripper if Telegram HTML parsing fails."""
    clean = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean)


def build_telegram_messages(
    metadata: Mapping[str, Any],
    metrics: pd.DataFrame | None = None,
    aborted: str | None = None,
    buying_results: pd.DataFrame | None = None,
    max_chars: int = 3900,
) -> list[str]:
    """Compose structured, formatted Telegram messages containing readable tables."""
    effective = metadata.get("as_of_effective", metadata.get("as_of_requested", ""))
    provisional = " (Provisional)" if metadata.get("provisional") else ""
    header = f"📈 <b>MOMENTUM SCREEN | NIFTY 500</b>\n📅 Date: <b>{effective}</b>{provisional}"

    if aborted:
        return [f"{header}\n\n⚠️ <b>SCREEN NOT COMPLETED</b>\n<pre>{html.escape(aborted)}</pre>"]

    sections: list[str] = []

    # 1. Final Qualifiers
    if metrics is not None:
        tables = console_tables(metrics)
        if not tables:
            sections.append(
                "<b>1. OVERALL STRENGTH (Final Qualifiers)</b>\n"
                "<i>No stocks passed the final screening rules.</i>"
            )
        else:
            df = tables[0][1]
            total_rows = len(df)
            chunk_size = 35
            for start in range(0, total_rows, chunk_size):
                sub = df.iloc[start : start + chunk_size]
                part = f" (Part {start // chunk_size + 1}/{(total_rows - 1) // chunk_size + 1})" if total_rows > chunk_size else ""
                table_str = sub.to_string(index=False, justify="right")
                txt = (
                    f"<b>1. OVERALL STRENGTH (Final Qualifiers)</b>{part}\n"
                    f"<pre>{html.escape(table_str)}</pre>"
                )
                sections.append(txt)

    # 2. Stage 2 Buying Checks
    if buying_results is not None:
        if buying_results.empty:
            sections.append(
                "<b>2. BUYING CHECKS | STAGE 2 (Confirmed Breakouts)</b>\n"
                "<i>None of the stocks passes the buying checks.</i>"
            )
        else:
            buying_tables = console_buying_tables(buying_results)
            for title, df in buying_tables:
                total_rows = len(df)
                chunk_size = 35
                for start in range(0, total_rows, chunk_size):
                    sub = df.iloc[start : start + chunk_size]
                    part = f" (Part {start // chunk_size + 1}/{(total_rows - 1) // chunk_size + 1})" if total_rows > chunk_size else ""
                    table_str = sub.to_string(index=False, justify="right")
                    txt = (
                        f"<b>2. BUYING CHECKS | STAGE 2 (Confirmed Breakouts)</b>{part}\n"
                        f"<i>ENTRY QUALITY SCORE COMPONENTS</i>\n"
                        f"<pre>{html.escape(table_str)}</pre>"
                    )
                    sections.append(txt)

    # Pack sections into messages up to max_chars
    messages: list[str] = []
    current_msg = header
    for sec in sections:
        candidate = f"{current_msg}\n\n{sec}" if current_msg else sec
        if len(candidate) <= max_chars:
            current_msg = candidate
        else:
            if current_msg:
                messages.append(current_msg)
            current_msg = sec
    if current_msg:
        messages.append(current_msg)
    return messages


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str | None = "HTML",
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> bool:
    """Send one message via Telegram Bot API with HTML fallback and error redaction."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    http = session or requests.Session()
    try:
        response = http.post(url, json=payload, timeout=timeout)
        # If HTML parse error (HTTP 400 with entity parsing failure), retry as plain text
        if response.status_code == 400 and parse_mode:
            logging.warning("Telegram HTML parsing error; retrying message as plain text.")
            plain_payload = {
                "chat_id": chat_id,
                "text": strip_html_tags(text),
                "disable_web_page_preview": True,
            }
            response = http.post(url, json=plain_payload, timeout=timeout)

        if response.ok:
            logging.info("Telegram message sent successfully.")
            return True
        elif response.status_code == 404:
            logging.error(
                "Telegram API error (HTTP 404 Not Found): The bot token is invalid or incomplete. "
                "Telegram bot tokens must follow the format '<bot_id>:<token_secret>' from @BotFather."
            )
            return False
        elif response.status_code == 401:
            logging.error(
                "Telegram API error (HTTP 401 Unauthorized): The bot token was rejected by Telegram."
            )
            return False
        elif response.status_code == 400:
            logging.error(
                "Telegram API error (HTTP 400 Bad Request): %s. "
                "Verify that chat ID '%s' is correct and that the bot has been added to the chat.",
                response.text, chat_id
            )
            return False
        else:
            logging.error("Telegram API error (HTTP %d): %s", response.status_code, response.text)
            return False
    except requests.exceptions.RequestException as exc:
        safe_msg = str(exc).replace(bot_token, "<REDACTED>")
        logging.error("Telegram network request failed: %s", safe_msg)
        return False


def send_telegram_report(
    credentials: Credentials,
    metadata: Mapping[str, Any],
    metrics: pd.DataFrame | None = None,
    aborted: str | None = None,
    buying_results: pd.DataFrame | None = None,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> bool:
    """Format and send the readable screener report to Telegram if credentials are set."""
    token = credentials.telegram_bot_token
    chat_id = credentials.telegram_chat_id
    if not token or not chat_id:
        logging.info("Telegram notification skipped: credentials not configured.")
        return False

    messages = build_telegram_messages(metadata, metrics=metrics, aborted=aborted,
                                       buying_results=buying_results)
    if not messages:
        return False

    logging.info("Sending %d Telegram message(s)...", len(messages))
    all_ok = True
    for i, msg in enumerate(messages):
        if i > 0:
            time.sleep(0.5)
        ok = send_telegram_message(token, chat_id, msg, session=session, timeout=timeout)
        if not ok:
            all_ok = False
    return all_ok


# =============================================================================
# CLI orchestration
# =============================================================================
def parse_date(value: str) -> date:
    """Require exactly YYYY-MM-DD in argparse rather than permissive date inference."""
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("Use a date in YYYY-MM-DD format.") from None


def make_parser() -> argparse.ArgumentParser:
    """Expose operational options only, never frozen research parameters."""
    parser = argparse.ArgumentParser(description="SCREENING ONLY — fixed-universe momentum metrics.")
    parser.add_argument(
        "--env", type=Path, default=Path(__file__).resolve().with_name("env.txt"),
        help="Credential file (default: env.txt beside this Python file).",
    )
    parser.add_argument("--as-of", type=parse_date)
    parser.add_argument(
        "--out", type=Path, default=STOCK_DATA_ROOT,
        help=f"Report/output directory (default: {STOCK_DATA_ROOT}).",
    )
    parser.add_argument(
        "--cache", type=Path, default=STOCK_CACHE_DIR,
        help=f"OHLCV cache directory (default: {STOCK_CACHE_DIR}).",
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--include-intraday", action="store_true")
    parser.add_argument("--no-telegram", action="store_true", default=False,
                        help="Disable sending Telegram notifications even if credentials are configured.")
    parser.add_argument("--log-level", type=str.upper, default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    return parser


def configure_logging(level: str) -> Path:
    """Log to console and to stock-data/logs — never to the repo logs/ tree."""
    STOCK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STOCK_LOG_DIR / "stock-screener.log"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level))
    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s",
                                  datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    # Do not expose HTTP internals even when application-level DEBUG is selected.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_path


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch, align, screen, report, and stop; return nonzero on every abort condition."""
    args = make_parser().parse_args(argv)
    # Resolve relative --out/--cache against the process CWD, then keep everything
    # under stock-data by default (absolute paths rooted next to this script).
    out_dir = Path(args.out).expanduser()
    cache_dir = Path(args.cache).expanduser()
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()
    if not cache_dir.is_absolute():
        cache_dir = (Path.cwd() / cache_dir).resolve()
    args.out = out_dir
    args.cache = cache_dir

    log_path = configure_logging(args.log_level)
    args.out.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    logging.info("Screener data root: %s", STOCK_DATA_ROOT)
    logging.info("Reports -> %s | cache -> %s | log -> %s",
                 args.out, args.cache, log_path)

    client: UpstoxClient | None = None
    try:
        validate_config()
        now = datetime.now(IST)
        requested = args.as_of or now.date()
        if requested > now.date():
            raise ScreenerError("--as-of cannot be later than today's IST date.")
        if args.refresh and args.offline:
            raise ScreenerError("--refresh and --offline cannot be combined.")
        credentials = load_credentials(args.env)
        provisional = intraday_enabled(requested, now, args.include_intraday)
        client = UpstoxClient(
            credentials, args.cache, args.refresh, args.offline, now.date(),
            master_dir=STOCK_DATA_ROOT,
        )
        master = client.instrument_master()
        benchmark_key = client.resolve_index(master)
        benchmark_ohlcv = fetch_ohlcv(client, "NIFTY500", benchmark_key, requested, provisional)
        benchmark = validate_benchmark(benchmark_ohlcv["close"])
        keys = {symbol: client.resolve_equity(master, symbol) for symbol in UNIVERSE}
        raw: dict[str, pd.Series] = {}
        raw_ohlcv: dict[str, pd.DataFrame] = {}
        failures: dict[str, str] = {}
        for symbol, key in keys.items():
            if key is None:
                continue
            try:
                ohlcv = fetch_ohlcv(client, symbol, key, requested, provisional)
                raw_ohlcv[symbol] = ohlcv
                raw[symbol] = ohlcv["close"]
            except APIError:
                failures[symbol] = "FETCH_FAILED"
                logging.warning("Candle fetch failed for %s; recorded as FETCH_FAILED.", symbol)
        closes, diagnostics = assemble_data(raw, benchmark, keys, failures)
        try:
            require_eligible_count(closes.shape[1])
        except ScreenerError as error:
            # Emit an explicit unsuccessful run manifest, not success-like CSVs.
            empty_metrics = pd.DataFrame(columns=list(FLAG_COLUMNS))
            metadata = build_run_metadata(empty_metrics, diagnostics, benchmark, requested,
                                          provisional, sorted(client.sources), datetime.now(timezone.utc))
            metadata["status"] = "aborted"
            metadata["error"] = str(error)
            metadata["counts"]["per_flag"] = {name: None for name in FLAG_COLUMNS}
            metadata["counts"]["core"] = None
            metadata["counts"]["final"] = None
            metadata["output_labels"] = {
                f"run_{metadata['as_of_effective']}.json": {"PROVISIONAL": provisional}
            }
            atomic_text(args.out / f"run_{metadata['as_of_effective']}.json",
                        json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            console_report(metadata, aborted=str(error), out_dir=args.out)
            if not args.no_telegram:
                send_telegram_report(credentials, metadata, aborted=str(error))
            return 1
        aligned_bench = benchmark.reindex(closes.index)
        metrics = compute_metrics(closes, aligned_bench)

        # Stage 2: Buying checks for FINAL_QUALIFIER stocks
        buying_results = evaluate_buying_candidates(metrics, raw_ohlcv, aligned_bench)

        metadata = build_run_metadata(metrics, diagnostics, benchmark, requested,
                                      provisional, sorted(client.sources), datetime.now(timezone.utc),
                                      buying_results=buying_results)
        metadata["status"] = "success"
        write_outputs(args.out, metrics, diagnostics, benchmark, metadata, buying_results=buying_results)
        console_report(metadata, metrics, out_dir=args.out, buying_results=buying_results)
        if not args.no_telegram:
            send_telegram_report(credentials, metadata, metrics=metrics, buying_results=buying_results)
        return 0
    except ScreenerError as error:
        logging.error("%s", error)
        return 1
    except (OSError, ValueError, TypeError) as exc:
        logging.error("Local file/configuration/data failure (%s: %s); check cache schema, permissions and frozen parameters.", type(exc).__name__, exc)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
