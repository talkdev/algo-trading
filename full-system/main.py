#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 NIFTY REGIME-BASED OPTIONS TRADING ENGINE — merged entrypoint (main.py)
================================================================================
 This module merges the original single files  config.py + clock.py + main.py
 (see README for the full merge map):

   * SECTION 1 — CONFIG   (originally config.py): every hardcoded parameter —
     flags, paths, thresholds, NSE holidays, session times, Upstox endpoints,
     rate limits, transaction-cost model.
   * SECTION 2 — CLOCK    (originally clock.py): IST timezone, Clock,
     VirtualClock (offline accelerated demo clock).
   * SECTION 3 — ORCHESTRATOR (originally main.py): scheduler loop, entry
     window, regime-change handling, position management, console report,
     graceful shutdown.

 Run:   python3 main.py                   # offline demo, paper trading
        python3 main.py --once            # single detection cycle
        python3 main.py --data upstox     # real Upstox quotes + paper fills
================================================================================
"""
import argparse
import json
import signal
import sys
import time
import types
from datetime import date, datetime, timedelta, time as dtime

# The sibling modules do `import main as C` / `from main import ...` for the
# config + clock symbols.  When this file is executed directly it lives in
# sys.modules as "__main__", so alias it so those imports resolve to this module.
import sys as _sys
_sys.modules.setdefault("main", _sys.modules[__name__])

# The merged config lives in THIS module — keep the familiar `C.` alias.
C = _sys.modules[__name__]

# =====================================================================
# SECTION 1 — HARDCODED CONFIGURATION (merged from config.py)
# =====================================================================



# ----------------------------------------------------------------------------
# 1. FLAGS & PATHS
# ----------------------------------------------------------------------------
PAPER_TRADING_MODE = True          # True = simulated fills (never touches a real
                                   #         brokerage account).
                                   # False = real orders via Upstox (LIVE).
DATA_SOURCE = "simulated"          # "simulated" -> offline DemoProvider feed
                                   # "upstox"    -> real Upstox REST + WebSocket
                                   # Use "upstox" only if env.txt is present with
                                   # a valid UPSTOX_ACCESS_TOKEN.

ALLOW_NON_TRADING_DAY_RUN = False  # For testing only (demo mode forces True with a warning)

ENV_FILE = "env.txt"               # UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_ACCESS_TOKEN

LOG_DIR = "./data"                 # relative to script root
STATE_DB = "state.db"              # SQLite checkpoint / recovery store
TRADE_ANALYSIS_CSV = "trade_analysis.csv"   # one row per CLOSED trade
AUDIT_LOG_CSV = "audit_log.csv"             # daily-rotated (gzip), JSONL
REGIME_LOG_CSV = "regime_log.csv"           # one row per detection cycle
REGIME_STATE_JSON = "regime_state.json"     # backward-compatible JSON snapshot
EVENTS_FILE = "events.json"                # macro calendar for Step-6 override
SEED_STATE_JSON = "regime_state_seed.json" # optional skew-history seed (ignored if absent)

# ----------------------------------------------------------------------------
# 2. VIX & DELTA BANDS
# ----------------------------------------------------------------------------
LOW_VIX = 12.0
HIGH_VIX = 18.0

MIN_DELTA_LOWVIX, MAX_DELTA_LOWVIX = 0.22, 0.28
MIN_DELTA, MAX_DELTA = 0.20, 0.25
MIN_DELTA_HIVIX, MAX_DELTA_HIVIX = 0.15, 0.20
MIN_PREMIUM, MAX_PREMIUM = 75.0, 130.0

# ----------------------------------------------------------------------------
# 3. EDGE & TREND THRESHOLDS
# ----------------------------------------------------------------------------
EDGE_RICH = 5.0          # IV_ATM - RV > 5%  -> seller edge (+1)
EDGE_CHEAP = 0.0         # IV_ATM - RV < 0%  -> buyer edge (-1)
TREND_ADX = 25.0         # ADX above this + EMA slope -> trending
RANGE_ADX = 22.0         # ADX below this -> range-bound
EMA_SLOPE_MIN_PCT = 0.05 # |EMA50 - EMA50(20 bars ago)| > 0.05% of spot

# regime detector plumbing
RV_WINDOW = 20                    # trading days for realised vol
RV_ANNUALISE = 252
SKEW_HISTORY_DAYS = 30            # z-score lookback for 25-delta skew
SKEW_MIN_DAYS = 10                # min history before skew z is trusted
SKEW_Z_STEEP = 1.5                # z >  1.5 -> fear      -> Skew_Score -1
SKEW_Z_FLAT = -1.0                # z < -1.0 -> complacent -> Skew_Score +1
TERM_THRESHOLD = 0.5              # V_fwd - V_spot > 0.5 contango / < -0.5 backwardation
TREND_BARS_REQUIRED = 75          # EMA50 + slope window + ADX warmup
SPREAD_AVG_MIN = 60               # minutes of spread history for baseline
SPREAD_SPAN_MIN = 20              # min span (min) before baseline is trusted
FLOW_MIN_AGE = 600                # reference OI snapshot min age (s)  ~10 min
FLOW_TARGET_AGE = 900             # target age (s)                    ~15 min
FLOW_MAX_AGE = 1800               # max age (s)                       ~30 min
MIN_OI = 50.0                     # quality filter: min OI (lots) / bid > 0
RISK_FREE = 6.5 / 100.0           # risk-free rate for BS fallback greeks
HIST_DAYS_5M = 8                  # calendar days of 5-min candles to fetch

WEIGHTS = {"vol": 0.30, "edge": 0.30, "trend": 0.25, "flow": 0.15}

REGIME_STRONG_SELL, REGIME_MILD_SELL, REGIME_NEUTRAL, REGIME_BUY, REGIME_STRONG_BUY = (
    "STRONG_SELL_VOL", "MILD_SELL_VOL", "NEUTRAL", "BUY_VOL", "STRONG_BUY_VOL")
REGIME_EVENT_HEDGE = "EVENT_HEDGE"

REGIME_ACTION = {
    REGIME_STRONG_SELL: "Deploy max size on Short Straddles / Iron Condors.",
    REGIME_MILD_SELL: "Deploy moderate size; prefer credit spreads over naked straddles.",
    REGIME_NEUTRAL: "Hold current positions; do not initiate new entries.",
    REGIME_BUY: "Reduce short size by 60%; consider long Put hedges.",
    REGIME_STRONG_BUY: "Flatten all short positions; deploy Long Straddles / Strangles.",
    REGIME_EVENT_HEDGE: "MACRO OVERRIDE: flatten shorts, switch to long-gamma or sit flat.",
}

# ----------------------------------------------------------------------------
# 4. POSITION SIZING & RISK
# ----------------------------------------------------------------------------
INITIAL_CAPITAL = 2_000_000.0      # paper starting capital (₹) for % sizing
MAX_RISK_PER_TRADE = 0.02          # 2% of capital
MAX_COMBINED_RISK = 0.04           # 4% of capital
MAX_DAILY_LOSS = -3000             # points (circuit breaker)
TRAIL_START_PROFIT = 2000          # points
TRAIL_RETAIN_PCT = 0.65
SL_BASE_PERCENT = 0.30             # base stop as % of premium
SL_REFERENCE_VIX = 14.0
SL_MIN_PERCENT = 0.18
SL_MAX_PERCENT = 0.40

# ----------------------------------------------------------------------------
# 5. STATIC STOP & PROFIT TARGETS
# ----------------------------------------------------------------------------
STATIC_STOP_PCT = 0.10             # 10% spot stop for short straddle
PROFIT_TARGET_PCT = 0.50           # close at 50% of max credit
IRON_CONDOR_WING_WIDTH = 300       # points
MIN_CREDIT = 100                   # min credit for Iron Condor / Credit Spreads
STRADDLE_DAY_HOLD = 3              # long ATM straddle hold (calendar days)
TIME_EXIT_DAYS_STRADDLE = 21       # exit short straddle 21 days to expiry
TIME_EXIT_DAYS_CONDOR = 7          # exit condor / credit spreads 7 days to expiry
RATIO_TIME_EXIT_DAYS = 14          # exit ratio spreads 14 days to expiry
LONG_STRADDLE_MAX_DEBIT_PCT = 0.025  # max debit <= 2.5% of spot
LONG_STRADDLE_TIME_EXIT_DAY = 3    # 3rd calendar day close at 15:15
BACKSPREAD_MAX_DEBIT = 30.0        # net debit <= 30 points
BACKSPREAD_MIN_WIDTH = 100         # 25d-10d strike width >= 100 pts
BUTTERFLY_MAX_DEBIT = 20.0         # net debit <= 20 pts
BUTTERFLY_MIN_RR = 4.0             # max profit / max loss >= 4:1
HEDGE_REDUCE_PCT = 0.60            # reduce shorts by 60% on BUY_VOL
GAMMA_LIMIT_PCT = 0.50             # gamma exposure trigger (>50% of limit -> F)
SPOT_200_EMA_DAYS = 200            # days for 200-EMA filter
VIX_SMA_FAST = 5                   # 5-period VIX SMA
VIX_SMA_SLOW = 10                  # 10-period VIX SMA

# ----------------------------------------------------------------------------
# 6. ORDER TIMEOUTS (seconds)
# ----------------------------------------------------------------------------
ORDER_FILL_TIMEOUT = 60            # total for all legs (multi-leg)
HEDGE_FILL_TIMEOUT = 30
CORE_FILL_TIMEOUT = 30
SL_FILL_TIMEOUT = 30
PARTIAL_FILL_CANCEL = True         # cancel remaining on partial fill
STAGGER_MS = 200                   # delay between order placements / status polls

# ----------------------------------------------------------------------------
# 7. NSE HOLIDAYS  (reviewed as of 2026-08-30 per spec)
# ----------------------------------------------------------------------------
NSE_MARKET_HOLIDAYS = frozenset({
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",
})
NSE_SPECIAL_TRADING_DAYS = frozenset({"2026-02-01"})
HOLIDAY_CALENDAR_REVIEWED_ON = date(2026, 8, 30)

# ----------------------------------------------------------------------------
# 8. SESSION TIMING (Asia/Kolkata)
# ----------------------------------------------------------------------------
ENTRY_VIX_TIME = dtime(9, 15)
STRIKE_SELECT_TIME = dtime(9, 19)
EXEC_START_TIME = dtime(9, 19, 30)
EXEC_END_TIME = dtime(9, 20, 30)
TIME_EXIT_NORMAL = dtime(15, 15)
TIME_EXIT_EXPIRY = dtime(15, 0)
TIME_LAST_IGNORE = dtime(14, 45)    # ignore regime changes after this
EXPIRY_SQUARE_OFF = dtime(14, 45)   # force square-off on expiry day

# ----------------------------------------------------------------------------
# 9. UPSTOX API & RATE LIMITS
# ----------------------------------------------------------------------------
UPSTOX_BASE_V2 = "https://api.upstox.com/v2"
UPSTOX_BASE_V3 = "https://api.upstox.com/v3"
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
RATE_LIMIT_PER_SEC = 50
RATE_LIMIT_BURST = 10
RETRY_BACKOFF_BASE = 1.0
RETRY_MAX_BACKOFF = 60.0
WS_URL_V3 = "wss://api.upstox.com/v3/feed/market-data-feed"
WS_FEED_STALE_SEC = 30             # kill-switch if feed silent this long

KEY_NIFTY = "NSE_INDEX|Nifty 50"
KEY_VIX = "NSE_INDEX|India VIX"
NIFTY_LOT_SIZE = 65                # fallback; real value read from instrument master

# ----------------------------------------------------------------------------
# 10. MACRO OVERRIDE WINDOW (Step 6)
# ----------------------------------------------------------------------------
EVENT_PRE_HOURS = 6                # 6h before ...
EVENT_POST_HOURS = 2               # ... 2h after a high-impact event

# ----------------------------------------------------------------------------
# 11. DEMO / TEST MODE
# ----------------------------------------------------------------------------
DEMO_CYCLE_SECONDS = 4             # wall-clock sleep between demo cycles
DEMO_VIRTUAL_STEP_MIN = 5          # each demo cycle advances virtual clock 5 min
DEMO_ENTRY_ANYTIME = True          # demo: allow entries outside 09:19:30-09:20:30
DEMO_STRICT_VALIDATION = False     # demo: lenient strategy validations
DEMO_ACCOUNT_CAPITAL = 2_000_000.0

# ----------------------------------------------------------------------------
# 12. TRANSACTION COST ESTIMATOR (paper accounting)
# ----------------------------------------------------------------------------
COST_BROKERAGE_OPTION = 20.0       # ₹ per order (Upstox flat)
COST_STT_OPTION_SELL_PCT = 0.001   # 0.1% of premium on sell side (post Oct-2024)
COST_STT_EXERCISE_PCT = 0.00125
COST_EXCHANGE_PCT = 0.0005         # NSE options transaction charge ~0.05%
COST_SEBI_PCT = 0.000001           # ₹10/crore
COST_STAMP_PCT = 0.00003           # 0.003% on buy side
COST_GST_PCT = 0.18                # 18% GST on (brokerage + exchange + sebi)

# ----------------------------------------------------------------------------
# derived helpers
# ----------------------------------------------------------------------------
DAILY_LOG_MAX_BYTES = 25 * 1024 * 1024   # rotate audit log above this
CHECKPOINT_HEARTBEAT_S = 10              # state checkpoint every 10 s

def is_trading_day(d: date) -> bool:
    """Weekday, not a hardcoded NSE holiday (special trading days allowed)."""
    if d.weekday() >= 5:
        return False
    iso = d.isoformat()
    if iso in NSE_MARKET_HOLIDAYS:
        return False
    return True

def is_expiry_day(d: date) -> bool:
    """Weekly NIFTY expiry = Thursday, unless a holiday shifts it (approx)."""
    return d.weekday() == 3

# =====================================================================
# SECTION 2 — TIME SOURCES (merged from clock.py)
# =====================================================================

from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    _HAS_ZONEINFO = True
except Exception:                      # pragma: no cover - py<3.9 fallback
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")
    _HAS_ZONEINFO = False

def now_ist() -> datetime:
    return datetime.now(IST)

class Clock:
    """Base clock — real wall time in IST."""
    def now(self) -> datetime:
        return now_ist()

    def sleep_seconds(self, s: float):
        import time
        time.sleep(s)

class VirtualClock(Clock):
    """Deterministic clock for offline demo/testing. Starts at the configured
    IST datetime and advances by `step` every `advance()` call."""
    def __init__(self, start: datetime, step: timedelta = timedelta(minutes=5)):
        self._t = start
        self._step = step

    def now(self) -> datetime:
        return self._t

    def advance(self, minutes: int = None) -> datetime:
        self._t += (timedelta(minutes=minutes) if minutes is not None else self._step)
        return self._t

    def set(self, dt: datetime):
        self._t = dt

    def sleep_seconds(self, s: float):
        # in virtual mode wall-clock sleeps are handled by the orchestrator;
        # expose a tiny real sleep so polling loops still yield
        import time
        time.sleep(min(s, 0.05))

# ---- sibling modules (they import config/clock symbols from this module) ----
from storage import (Loggers, StateStore, enable_color, g, r, y, cy, mg, bo, dim, fx)
from execution import (Feed, UpstoxClient, PaperBroker, LiveBroker, DemoProvider,
                       MarketDataStreamerV3, OrderExecutor, RiskManager,
                       pick_expiries, load_env_file)
from regime_engine import RegimeDetector, StrategySelector, load_events

# =====================================================================
# SECTION 3 — ORCHESTRATOR (merged from main.py)
# =====================================================================

# =====================================================================

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 NIFTY REGIME-BASED OPTIONS TRADING ENGINE  —  main.py (orchestrator)
================================================================================
 Per the system spec:

  * PAPER_TRADING_MODE = True by default (config.py). No real orders unless
    you explicitly flip it to False AND supply a valid UPSTOX_ACCESS_TOKEN.
  * No runtime input: everything is hardcoded in config.py.
  * State is checkpointed to SQLite (state.db) on every fill, every regime
    change and every 10 s heartbeat -> crash recovery guaranteed.
  * All closed trades are logged to trade_analysis.csv (one row per strategy
    execution).
  * Regime detection runs every 5 minutes (Steps 0-8); strategy entry is
    gated to the 09:19:30-09:20:30 IST window; positions are managed every
    cycle; regime transitions A-E are applied on confirmed regime changes.

 USAGE
 -----
   python main.py                 # demo/simulated feed, paper trading
   python main.py --demo          # same as default
   python main.py --once          # single cycle then exit
   python main.py --data upstox   # real Upstox data + paper fills (needs env.txt)
   python main.py --no-color      # plain console output
================================================================================
"""
import argparse
import json
import signal
import sys
import time
import types
from datetime import datetime, timedelta

W = 88

def rule(ch="─"):
    return dim(ch * W)

def header_line(txt):
    return cy(" " + txt) + " " + dim("─" * max(0, W - 2 - len(txt)))

def parse_args():
    p = argparse.ArgumentParser(description="NIFTY regime-based options trading engine")
    p.add_argument("--demo", action="store_true", help="offline simulated feed (default when --data is omitted)")
    p.add_argument("--data", choices=["simulated", "upstox"], default=C.DATA_SOURCE,
                   help="market data source (default from config.py)")
    p.add_argument("--env", default=C.ENV_FILE, help="env file with UPSTOX_* keys")
    p.add_argument("--interval", type=int, default=None,
                   help="override detection interval in seconds (default 300)")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--virtual-step", type=int, default=C.DEMO_VIRTUAL_STEP_MIN,
                   help="demo: virtual minutes per cycle")
    p.add_argument("--demo-start", default=None,
                   help="demo: virtual start 'YYYY-MM-DD HH:MM' (IST), default next trading day 09:14")
    return p.parse_args()

# ---------------------------------------------------------------------------
# console report
# ---------------------------------------------------------------------------
def market_phase(now):
    if now.weekday() >= 5:
        return "CLOSED (weekend)", y
    if not C.is_trading_day(now.date()):
        return "CLOSED (NSE holiday)", y
    t = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= t <= 15 * 60 + 30:
        return "OPEN", g
    if t < 9 * 60 + 15:
        return "PRE-OPEN", y
    return "CLOSED (after hours)", y

def build_report(cyc, res, selector, executor, clock, phase, entry_state, meta):
    L = []
    now = res.ts
    L.append(bo(cy("╔" + "═" * (W - 2) + "╗")))
    title = "NIFTY REGIME OPTIONS ENGINE  ·  " + now.strftime("%a %d-%b-%Y %H:%M:%S IST")
    L.append(bo(cy("║" + title.center(W - 2) + "║")))
    L.append(bo(cy("╚" + "═" * (W - 2) + "╝")))
    phase_txt, phase_c = phase
    L.append(f"  Cycle #{cyc}  ·  {phase_c(phase_txt)}  ·  data: {meta['data_source']}  ·  "
             f"paper: {bo(str(meta['paper']))}  ·  Ctrl+C to stop")
    L.append(rule())

    L.append(header_line("MARKET SNAPSHOT"))
    L.append(f"   NIFTY 50   {bo(fx(res.spot))}   VIX {fx(res.vix)}   "
             f"RV{C.RV_WINDOW} {fx(res.rv)}%  IV_atm {fx(res.iv_atm)}%")
    L.append(f"   near expiry {res.near_expiry} · monthly {res.monthly_expiry} · "
             f"PCR {fx(res.pcr, 2)} · fut basis {fx(res.fut_basis, 2)}")
    L.append(rule())

    L.append(header_line("MODULE SCORES  (raw -> confirmed)"))
    labels = {"vol": "[1] VOL SURFACE ", "edge": "[2] VOL EDGE    ",
              "trend": "[3] TREND       ", "flow": "[4] ORDER FLOW  "}
    for m in ("vol", "edge", "trend", "flow"):
        mr = getattr(res, m)
        conf_s = _sc(mr.confirmed)
        raw_s = _sc(mr.raw)
        note = getattr(mr, "notes", [])
        L.append(f"   {labels[m]} {mr.detail}")
        L.append(f"   {' ' * len(labels[m])} score: {raw_s} -> {conf_s}   {dim('')}")
    L.append(rule())

    L.append(header_line("AGGREGATION (Steps 5-8)"))
    terms = " + ".join(f"{w}·({res.confirmed_scores[k]:+g})" for k, w in C.WEIGHTS.items())
    L.append(f"   Composite = {terms} = {bo(f'{res.composite:+.3f}')}")
    if res.override:
        L.append("   Macro: " + r(bo(res.macro_text)))
    else:
        L.append("   Macro override: OFF  (" + res.macro_text + ")")
    L.append(rule())

    col = g if res.composite > 0.15 else (r if res.composite < -0.15 else y)
    reg = bo(mg("EVENT_HEDGE")) if res.regime == C.REGIME_EVENT_HEDGE else bo(col(res.regime))
    L.append("  " + bo("★ CONFIRMED REGIME: ") + reg)
    L.append("    Action : " + C.REGIME_ACTION.get(res.regime, ""))
    if entry_state.get("msg"):
        L.append("    " + entry_state["msg"])
    L.append(rule())

    L.append(header_line("POSITIONS"))
    if not executor.open_trades:
        L.append("   no open trades")
    for tid, t in executor.open_trades.items():
        status = t.get("status", "?")
        nlegs = len(t["legs"])
        L.append(f"   {t['strategy_name']:<22} {status:<8} legs {nlegs}  "
                 f"entry {t['entry_ts'][:16]}  spot {fx(t.get('entry_spot'))}")
    L.append(rule())

    if res.notes:
        L.append(dim("   notes: " + " | ".join(res.notes)))
    if selector.last and selector.last.rationale:
        L.append(dim("   selection: " + selector.last.rationale))
    if meta.get("last_cycle_seconds") is not None:
        L.append(dim(f"   cycle time {meta['last_cycle_seconds']*1000:.0f} ms · "
                     f"day pnl {meta.get('day_pnl', 0.0):+.0f} pts"))
    return "\n".join(L)

def _sc(v):
    if v is None:
        return dim("n/a")
    s = f"{v:+g}"
    return g(s) if v > 0 else (r(s) if v < 0 else y(s))

# ---------------------------------------------------------------------------
# bootstrap helpers
# ---------------------------------------------------------------------------
def resolve_data_source(args):
    """-> (client, master, streamer_or_None, note)"""
    if args.data == "upstox":
        env = load_env_file(args.env)
        token = env.get("UPSTOX_ACCESS_TOKEN", "")
        if not token:
            print(r("ERROR: --data upstox requires UPSTOX_ACCESS_TOKEN in " + args.env))
            sys.exit(2)
        client = UpstoxClient(token)
        master = client.instrument_master(cache_dir=C.LOG_DIR)
        streamer = None
        if C.PAPER_TRADING_MODE:
            note = "upstox data + PAPER fills"
        else:
            note = "LIVE upstox data + LIVE orders"
        return client, master, streamer, note
    # simulated
    demo = DemoProvider(clock=_global_clock)
    master = demo.instruments()
    return demo, master, None, "simulated feed (offline)"

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()
    enable_color(not args.no_color)
    if args.demo:
        args.data = "simulated"

    print(bo(cy("═" * W)))
    print(bo(cy(" NIFTY REGIME-BASED OPTIONS TRADING ENGINE — starting up")))
    print(bo(cy("═" * W)))
    print(f"  paper trading : {bo('TRUE') if C.PAPER_TRADING_MODE else bo(r('FALSE — LIVE'))}")
    print(f"  data source   : {args.data}")
    if C.PAPER_TRADING_MODE and args.data == "upstox":
        print(y("  NOTE: paper fills with real Upstox quotes — no real orders."))
    if not C.PAPER_TRADING_MODE and args.data == "simulated":
        print(r("  FATAL: LIVE mode requires --data upstox. Aborting."))
        sys.exit(2)

    # --------------------------------------------------------------- clock
    use_virtual = args.data == "simulated"
    if use_virtual:
        start = None
        if args.demo_start:
            try:
                start = datetime.strptime(args.demo_start, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            except ValueError:
                print(r(f"bad --demo-start {args.demo_start} (use YYYY-MM-DD HH:MM)"))
                sys.exit(2)
        if start is None:
            d = now_ist().date()
            while not C.is_trading_day(d):
                d += timedelta(days=1)
            start = datetime(d.year, d.month, d.day, 9, 14, 0).replace(tzinfo=now_ist().tzinfo)
        clock = VirtualClock(start, timedelta(minutes=args.virtual_step))
        print(y(f"  virtual clock starts {start:%a %d-%b-%Y %H:%M} IST "
                f"(+{args.virtual_step} min/cycle)"))
    else:
        clock = Clock()
    global _global_clock
    _global_clock = clock

    # ------------------------------------------------------- trading-day gate
    today = clock.now().date()
    if not C.is_trading_day(today):
        if C.ALLOW_NON_TRADING_DAY_RUN or use_virtual:
            print(y(f"  WARNING: {today} is not an NSE trading day — continuing "
                    f"({('test override' if C.ALLOW_NON_TRADING_DAY_RUN else 'virtual clock')})."))
        else:
            print(r(f"  {today} is not an NSE trading day (holiday/weekend). Refusing to run."))
            sys.exit(0)

    # ------------------------------------------------------------- plumbing
    loggers = Loggers()
    state = StateStore(C.STATE_DB)
    events, ev_err = load_events(C.EVENTS_FILE)

    client, master, streamer, ds_note = resolve_data_source(args)
    feed = Feed(client, streamer)
    if use_virtual:
        broker = PaperBroker(feed, capital=C.DEMO_ACCOUNT_CAPITAL)
    else:
        broker = PaperBroker(feed) if C.PAPER_TRADING_MODE else LiveBroker(client)

    ctx = types.SimpleNamespace(
        feed=feed, broker=broker, clock=clock, state=state, loggers=loggers,
        risk=None, lot_size=master.get("lot_size", C.NIFTY_LOT_SIZE),
        last_spot=None, last_vix=None, last_regime=None,
        demo=use_virtual,
    )
    risk = RiskManager(broker, state, loggers, capital=broker.cash)
    ctx.risk = risk

    detector = RegimeDetector(feed, state, clock, events=events, loggers=loggers)
    detector.futs = master.get("futs", [])
    selector = StrategySelector(ctx, detector)
    executor = OrderExecutor(ctx)

    # ---- demo warm-up: pre-seed skew history + relax flow warm-up ------------
    if use_virtual:
        import random
        if not state.skew_series(limit=1):
            rng = random.Random(9)
            base = clock.now().date()
            for i in range(25, 0, -1):
                state.record_skew(round(rng.gauss(1.6, 0.8), 3),
                                  (base - timedelta(days=i)).isoformat())
            print(y(f"  demo warm-up: seeded 25 days of skew history"))
        C.FLOW_MIN_AGE = max(2 * args.virtual_step * 60, 4.0)
        C.FLOW_TARGET_AGE = max(3 * args.virtual_step * 60, 6.0)
        C.FLOW_MAX_AGE = max(12 * args.virtual_step * 60, 30.0)
        C.SPREAD_SPAN_MIN = max(2 * args.virtual_step, 0.05)
        C.SPREAD_AVG_MIN = max(12 * args.virtual_step, 1.0)

    if args.interval is not None:
        interval = args.interval
    elif use_virtual:
        interval = C.DEMO_CYCLE_SECONDS
    else:
        interval = 300

    near_exp, monthly_exp = pick_expiries(master, clock.now().date())
    if use_virtual:
        near_exp = master["expiries"][0]["date"]
        monthlies = [e["date"] for e in master["expiries"] if not e["weekly"]]
        monthly_exp = monthlies[0] if monthlies else None
    if not near_exp:
        print(r("ERROR: could not determine any NIFTY option expiry."))
        sys.exit(2)

    print(f"  near expiry : {near_exp}   monthly (V_fwd): {monthly_exp or 'n/a'}"
          f"   [{master.get('source', '')}]  lot {ctx.lot_size}")
    print(f"  interval    : {interval}s   state: {C.STATE_DB}   log dir: {C.LOG_DIR}")
    n_high = sum(1 for e in events if e["high"])
    print(f"  calendar    : {len(events)} events ({n_high} high-impact)"
          + (y(f"  [{ev_err}]") if ev_err else ""))
    print(dim("  algo: Vol(0.30)+Edge(0.30)+Trend(0.25)+Flow(0.15) · persistence 3x · "
              "macro override 6h/-2h · entry window 09:19:30-09:20:30"))

    # ------------------------------------------------------- graceful shutdown
    stopping = {"flag": False}

    def _shutdown(sig, frame):
        if stopping["flag"]:
            sys.exit(1)
        stopping["flag"] = True
        print("\n" + r(bo("  SHUTDOWN REQUESTED — squaring off all positions (live-safe).")))
        try:
            executor.square_off_all("MANUAL")
        except Exception as e:
            print(r(f"  square-off error: {e}"))
        state.close()
        loggers.audit("SHUTDOWN", signal=sig)
        print(bo(cy("═" * W)))
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ------------------------------------------------------------ main loop
    cyc = 0
    entered_today = False
    entry_msg = ""
    last_heartbeat = time.monotonic()
    last_cycle_t = time.monotonic()
    try:
        while True:
            now = clock.now()
            cyc += 1
            cycle_start = time.monotonic()
            risk._roll_day(clock)

            # ---- run detection ------------------------------------------
            try:
                res = detector.run_cycle(near_exp, monthly_exp)
            except Exception as e:
                print(r(f"  cycle {cyc} failed: {e} — retrying next interval"))
                res = detector.last_result
                if res is None:
                    if args.once:
                        break
                    clock.sleep_seconds(interval)
                    continue

            ctx.last_spot, ctx.last_vix = res.spot, res.vix
            ctx.last_regime = res.regime

            # ---- regime change -> transitions A-E -------------------------
            confirmed = detector.prev_confirmed_regime
            if confirmed and confirmed != getattr(ctx, "_applied_regime", None):
                old = getattr(ctx, "_applied_regime", None)
                if old:
                    executor.apply_transitions(old, confirmed, selector.build_env(res))
                    loggers.audit("REGIME_CHANGE", from_regime=old, to_regime=confirmed,
                                  composite=res.composite)
                ctx._applied_regime = confirmed

            # ---- entry window gate ----------------------------------------
            tnow = now.time()
            entry_state = {"msg": ""}
            if use_virtual and C.DEMO_ENTRY_ANYTIME:
                # demo: enter on the first eligible cycle of the day after the
                # strike-selection time (the 30-s window cannot be sampled by
                # 5-minute virtual steps)
                allow_entry = tnow >= C.EXEC_START_TIME and tnow <= C.TIME_EXIT_NORMAL
            else:
                allow_entry = C.EXEC_START_TIME <= tnow <= C.EXEC_END_TIME
            if not entered_today and allow_entry:
                sel = selector.evaluate(res)
                if sel.plan is not None:
                    scores = {"composite": res.composite, **res.confirmed_scores}
                    tid = executor.execute_entry(sel.plan, sel.env, res.regime, scores=scores)
                    if tid:
                        entry_msg = (f"ENTRY: {sel.strategy_name} (tid {tid}) — "
                                     f"entered today, no re-entry until next session")
                        entered_today = True
                    else:
                        entry_msg = f"ENTRY ABORTED — {sel.rationale}"
                else:
                    entry_msg = f"NO ENTRY: {sel.rationale}"
            elif entered_today:
                entry_msg = "entry already executed today — no new entries until next session"
            elif not allow_entry and tnow >= C.EXEC_END_TIME:
                entry_msg = "ENTRY WINDOW CLOSED (after 09:20:30 IST)"
            elif tnow < C.EXEC_START_TIME:
                entry_msg = "awaiting entry window (09:19:30-09:20:30 IST)"

            # ---- manage open positions ------------------------------------
            env = selector.build_env(res)
            executor.manage_positions(res, env, now)

            # ---- heartbeat checkpoint (every 10 s) ------------------------
            if time.monotonic() - last_heartbeat >= C.CHECKPOINT_HEARTBEAT_S:
                state.checkpoint({"spot": res.spot, "vix": res.vix,
                                  "scores": res.confirmed_scores,
                                  "composite": res.composite,
                                  "regime": res.regime}, reason="heartbeat")
                last_heartbeat = time.monotonic()

            # ---- console + csv log -----------------------------------------
            phase = market_phase(now)
            meta = {"data_source": args.data, "paper": C.PAPER_TRADING_MODE,
                    "last_cycle_seconds": time.monotonic() - cycle_start,
                    "day_pnl": risk.day_pnl_points}
            entry_state["msg"] = entry_msg
            print("\n" + build_report(cyc, res, selector, executor, clock, phase,
                                      entry_state, meta))
            loggers.regime_csv.append([now.isoformat(timespec="seconds"),
                                       f"{res.composite:+.3f}", res.regime,
                                       res.vol.raw, res.vol.confirmed,
                                       res.edge.raw, res.edge.confirmed,
                                       res.trend.raw, res.trend.confirmed,
                                       res.flow.raw, res.flow.confirmed,
                                       res.spot, res.vix, int(res.override)])

            if args.once:
                break

            # ---- sleep (real) or advance (virtual) --------------------------
            if use_virtual:
                clock.advance(args.virtual_step)
                n2 = clock.now()
                # roll to fresh expiries when the current one has passed
                last_exp = monthly_exp or near_exp
                if last_exp:
                    from datetime import datetime as _dt
                    try:
                        exp_d = _dt.strptime(last_exp, "%Y-%m-%d").date()
                        if n2.date() > exp_d:
                            master = client.instruments()      # regenerate expiries
                            near_exp = master["expiries"][0]["date"]
                            monthlies = [e["date"] for e in master["expiries"] if not e["weekly"]]
                            monthly_exp = monthlies[0] if monthlies else None
                            detector.futs = master.get("futs", [])
                            print(y(f"  --- rolled to new expiry set: near {near_exp} "
                                    f"monthly {monthly_exp} ---"))
                    except (ValueError, TypeError):
                        pass
                # after the session, jump straight to the next trading day 09:14
                if n2.time() >= C.TIME_EXIT_NORMAL or n2.time() < C.ENTRY_VIX_TIME:
                    d = n2.date() + timedelta(days=1)
                    while not C.is_trading_day(d):
                        d += timedelta(days=1)
                    n2 = datetime(d.year, d.month, d.day, 9, 14, 0).replace(tzinfo=n2.tzinfo)
                    clock.set(n2)
                    print(y(f"  --- next trading day {d} ({n2:%a}) ---"))
                if clock.now().date() != now.date():
                    entered_today = False
                    entry_msg = ""
                clock.sleep_seconds(0.15)
            else:
                clock.sleep_seconds(interval)

    except KeyboardInterrupt:
        pass

    # --------------------------------------------------------------- teardown
    print("\n" + bo(cy("═" * W)))
    print(bo(cy(" Engine stopped.")))
    print(f"  cycles run : {cyc}")
    print(f"  open trades: {len(executor.open_trades)}")
    if executor.open_trades:
        print(y("  NOTE: open trades are persisted in state.db — re-run to resume management."))
    if args.once or not executor.open_trades:
        try:
            executor.square_off_all("MANUAL")
        except Exception:
            pass
    loggers.audit("STOPPED", cycles=cyc)
    state.close()
    print(bo(cy("═" * W)))
    return 0

_global_clock = Clock()

if __name__ == "__main__":
    sys.exit(main())



