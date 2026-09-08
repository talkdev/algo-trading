#!/usr/bin/env python3
# ============================================================================
#  backtest_engine.py
#  Event-driven replay simulator for the NIFTY intraday options engine
# ============================================================================
#
#  WHY THIS EXISTS
#  ---------------
#  The repository already contains backtest.py, but that is a REPORTER: it
#  reads trades that were actually executed and summarises them. It cannot
#  answer "what would this change have done", because it has no counterfactual
#  price path — the trades in the table are the only trades it knows about.
#
#  That gap is why every audit of this engine ends in an argument rather than a
#  measurement. Thresholds get changed on the strength of reasoning, and the
#  next audit produces different reasoning. Nothing converges, because nothing
#  is ever scored.
#
#  This file closes the gap. It replays the option_chain_snapshot and
#  intraday_candles tables the live engine has already been writing on every
#  cycle, drives the REAL decision code with them, simulates fills from the
#  recorded bid/ask, and reports what actually happened.
#
#  WHAT IS REAL AND WHAT IS SIMULATED
#  ----------------------------------
#  REAL — imported and executed, not reimplemented:
#     MarketDataEngine.run_cycle()      signal construction, every indicator
#     RegimeClassifier / regime engine  regime + confidence + sizing multiplier
#     StrategyEngine.decide()           gates, strike selection, EV, sizing
#     ExecutionEngine.monitor_position()the full 7-priority exit ladder
#     StrategyEngine._compute_costs()   brokerage, STT, exchange, GST, stamp
#  If you change any of those, the backtest changes with them. That is the
#  entire point: it measures the engine, not a model of the engine.
#
#  SIMULATED — and therefore assumptions you can and should argue with:
#     * FILLS. A limit order is assumed to fill at a configurable point
#       between the touch and the mid (--fill-edge), inside the recorded
#       bid/ask of that snapshot. Real fills depend on queue position and
#       on size; this does not model either.
#     * EXIT URGENCY. Stop-driven exits pay a wider crossing than target
#       exits (--stress-exit), because they do in life.
#     * NO PARTIAL FILLS, NO REJECTIONS, NO LATENCY. Every order is assumed
#       to fill completely at the snapshot price. Real execution loses money
#       here that this harness will not show you.
#     * SNAPSHOT GRANULARITY. The engine is only as fast as the recorded
#       cadence. If snapshots are 45 seconds apart, so is the simulated
#       monitoring loop, and intra-snapshot excursions are invisible. A stop
#       that would have been hit and recovered between two snapshots is
#       never seen. This biases results OPTIMISTIC.
#
#  READ THIS BEFORE BELIEVING ANY NUMBER IT PRINTS
#  -----------------------------------------------
#  1. A backtest over the same data used to choose the thresholds is not
#     evidence. It is a restatement of the choice. Hold out dates, or fit on
#     one period and measure on another. The tool prints a warning whenever
#     the sample is too small for the distinction to matter.
#  2. Snapshot data only exists for sessions the engine was actually running.
#     Run --audit first; it will tell you exactly what you have.
#  3. The most valuable output here is NOT the P&L. It is the REJECTION
#     CENSUS: which gate blocked entry, how often, and with what margin. That
#     is what tells you whether the engine is mis-calibrated or the market
#     simply was not offering the trade.
#
#  USAGE
#  -----
#     python backtest_engine.py --audit
#     python backtest_engine.py --from 2026-08-01 --to 2026-09-05
#     python backtest_engine.py --from 2026-08-01 --to 2026-09-05 \
#            --capital 1000000 --fill-edge 0.25 --csv runs/aug.csv
#     python backtest_engine.py --test          # harness self-test, synthetic
#
# ============================================================================

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
import tempfile
import uuid
import warnings
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import core  # noqa: E402
from core import (  # noqa: E402
    Config,
    Database,
    RateLimiter,
    load_config,
)

# The engine modules are imported lazily inside build_engines() so that the
# simulated clock is installed before any of them capture a timestamp.
INSTRUMENT_NIFTY = "NSE_INDEX|Nifty 50"
INSTRUMENT_VIX = "NSE_INDEX|India VIX"


# ═══════════════════════════════════════════════════════════════════════════
#  SIMULATED CLOCK
# ═══════════════════════════════════════════════════════════════════════════

class SimClock:
    """
    A settable stand-in for core.now_ist().

    Every engine module does `from core import now_ist, today_ist`, which binds
    the function into that module's namespace. Patching core alone is therefore
    not enough — each module's own binding has to be replaced.

    Returns NAIVE datetimes. The live engine's now_ist() is timezone-aware, but
    inside a replay there is exactly one timezone and mixing aware and naive
    values across the ~40 places that do datetime arithmetic is a much larger
    hazard than dropping the tzinfo.
    """

    MODULES = (
        "core", "data_engine", "regime_engine", "strategy_engine",
        "execution_engine", "calibration_engine",
    )

    def __init__(self) -> None:
        self._dt: datetime = datetime(2026, 1, 1, 9, 15)
        self._installed: List[Tuple[object, str, object]] = []

    # -- clock ------------------------------------------------------------
    def set(self, dt: datetime) -> None:
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        self._dt = dt

    def now(self) -> datetime:
        return self._dt

    def today(self) -> date:
        return self._dt.date()

    # -- installation -----------------------------------------------------
    def install(self) -> None:
        for name in self.MODULES:
            mod = sys.modules.get(name)
            if mod is None:
                continue
            for attr, fn in (("now_ist", self.now), ("today_ist", self.today)):
                if hasattr(mod, attr):
                    self._installed.append((mod, attr, getattr(mod, attr)))
                    setattr(mod, attr, fn)

    def uninstall(self) -> None:
        for mod, attr, original in reversed(self._installed):
            setattr(mod, attr, original)
        self._installed.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  HISTORICAL DATA
# ═══════════════════════════════════════════════════════════════════════════

class HistoricalStore:
    """Read-only access to the recorded snapshots in the live database."""

    def __init__(self, db_path: str):
        self.path = db_path
        if not Path(db_path).exists():
            raise FileNotFoundError(db_path)
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def _q(self, sql: str, params: tuple = ()) -> List[dict]:
        try:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    # -- audit ------------------------------------------------------------
    def audit(self) -> dict:
        chain_days = self._q(
            "SELECT trading_date, COUNT(DISTINCT capture_time) AS cycles, "
            "COUNT(*) AS rows, COUNT(DISTINCT strike) AS strikes "
            "FROM option_chain_snapshot GROUP BY trading_date "
            "ORDER BY trading_date"
        )
        candle_days = {
            r["trading_date"]: r["bars"]
            for r in self._q(
                "SELECT trading_date, COUNT(*) AS bars FROM intraday_candles "
                "WHERE interval_min=1 GROUP BY trading_date"
            )
        }
        for d in chain_days:
            d["bars"] = candle_days.get(d["trading_date"], 0)
        return {"days": chain_days, "candle_days": candle_days}

    def tradable_dates(self, d_from: Optional[str], d_to: Optional[str]) -> List[str]:
        sql = (
            "SELECT DISTINCT trading_date FROM option_chain_snapshot "
            "WHERE 1=1"
        )
        params: list = []
        if d_from:
            sql += " AND trading_date >= ?"
            params.append(d_from)
        if d_to:
            sql += " AND trading_date <= ?"
            params.append(d_to)
        sql += " ORDER BY trading_date"
        return [r["trading_date"] for r in self._q(sql, tuple(params))]

    # -- per-day payload --------------------------------------------------
    def load_day(self, trading_date: str) -> "DaySlice":
        rows = self._q(
            "SELECT * FROM option_chain_snapshot WHERE trading_date=? "
            "ORDER BY capture_time",
            (trading_date,),
        )
        candles = self._q(
            "SELECT candle_time, open, high, low, close, volume "
            "FROM intraday_candles WHERE trading_date=? AND interval_min=1 "
            "ORDER BY candle_time",
            (trading_date,),
        )
        prev = self._q(
            "SELECT trading_date, close FROM intraday_candles "
            "WHERE trading_date < ? AND interval_min=1 "
            "ORDER BY trading_date DESC, candle_time DESC LIMIT 1",
            (trading_date,),
        )
        prev_close = float(prev[0]["close"]) if prev else None
        return DaySlice(trading_date, rows, candles, prev_close)


class DaySlice:
    """One session's recorded chain snapshots and 1-minute bars."""

    def __init__(
        self,
        trading_date: str,
        chain_rows: List[dict],
        candles: List[dict],
        prev_close: Optional[float],
    ):
        self.trading_date = trading_date
        self.prev_close = prev_close
        self.candles = candles

        self.cycles: List[str] = []
        self.by_time: Dict[str, List[dict]] = defaultdict(list)
        for r in chain_rows:
            ct = str(r["capture_time"])
            if ct not in self.by_time:
                self.cycles.append(ct)
            self.by_time[ct].append(r)
        self.cycles.sort()

    def cycle_dt(self, capture_time: str) -> datetime:
        try:
            return datetime.fromisoformat(capture_time).replace(tzinfo=None)
        except ValueError:
            return datetime.combine(
                date.fromisoformat(self.trading_date), dtime(9, 15)
            )

    def snapshot(self, capture_time: str) -> List[dict]:
        return self.by_time.get(capture_time, [])

    def spot_vix(self, capture_time: str) -> Tuple[Optional[float], Optional[float]]:
        rows = self.by_time.get(capture_time) or []
        if not rows:
            return None, None
        spot = rows[0].get("spot_at_capture")
        vix = rows[0].get("vix_at_capture")
        return (
            float(spot) if spot else None,
            float(vix) if vix else None,
        )

    def expiry(self, capture_time: str) -> Optional[str]:
        rows = self.by_time.get(capture_time) or []
        return str(rows[0]["expiry"]) if rows else None

    def bars_until(self, cutoff: datetime) -> List[list]:
        """1-minute candles up to the simulated clock, in Upstox list shape."""
        out = []
        cut = cutoff.strftime("%H:%M:%S")
        for c in self.candles:
            ct = str(c["candle_time"])
            if len(ct) == 5:
                ct += ":00"
            if ct > cut:
                break
            out.append([
                f"{self.trading_date}T{ct}+05:30",
                float(c["open"]), float(c["high"]),
                float(c["low"]), float(c["close"]),
                int(c["volume"] or 0), 0,
            ])
        return out


# ═══════════════════════════════════════════════════════════════════════════
#  REPLAY CLIENT  (stands in for UpstoxClient)
# ═══════════════════════════════════════════════════════════════════════════

class ReplayClient:
    """
    Serves the recorded snapshot at the simulated clock time, in exactly the
    response shapes the live parsers expect. Anything that would place an order
    raises loudly: a backtest must never reach the broker.
    """

    def __init__(self, clock: SimClock):
        self.clock = clock
        self.day: Optional[DaySlice] = None
        self.cycle: Optional[str] = None

    def point(self, day: DaySlice, capture_time: str) -> None:
        self.day, self.cycle = day, capture_time

    # -- market data ------------------------------------------------------
    def get_ltp(self, instrument_keys) -> dict:
        spot, vix = (self.day.spot_vix(self.cycle) if self.day else (None, None))
        out = {}
        if spot:
            out["NSE_INDEX:Nifty 50"] = {"last_price": spot}
        if vix:
            out["NSE_INDEX:India VIX"] = {"last_price": vix}
        return out

    def get_full_quote(self, instrument_keys: list) -> dict:
        return {}

    def get_intraday_candles(self, instrument_key: str, interval: str) -> list:
        if not self.day:
            return []
        return self.day.bars_until(self.clock.now())

    def get_historical_candles(self, instrument_key, interval, from_date, to_date) -> list:
        return []

    def get_option_contracts(self, instrument_key: str, expiry_date=None) -> list:
        exp = self.day.expiry(self.cycle) if self.day else None
        return [{"expiry": exp}] if exp else []

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list:
        if not self.day:
            return []
        by_strike: Dict[float, dict] = {}
        for r in self.day.snapshot(self.cycle):
            k = float(r["strike"])
            side = "call_options" if str(r["option_type"]).lower().startswith("c") else "put_options"
            entry = by_strike.setdefault(k, {"strike_price": k})
            entry[side] = {
                "instrument_key": f"SIM|{k:.0f}{side[0].upper()}E",
                "market_data": {
                    "bid_price": float(r["bid"] or 0),
                    "ask_price": float(r["ask"] or 0),
                    "ltp": float(r["ltp"] or 0),
                    "oi": int(r["oi"] or 0),
                    "volume": int(r["volume"] or 0),
                },
                "option_greeks": {
                    "iv": float(r["iv"] or 0),
                    "delta": float(r["delta"] or 0),
                    "gamma": float(r["gamma"] or 0),
                    "theta": float(r["theta"] or 0),
                    "vega": float(r["vega"] or 0),
                },
            }
        return list(by_strike.values())

    # -- anything that trades ---------------------------------------------
    def _blocked(self, *a, **k):
        raise RuntimeError(
            "backtest_engine attempted a broker call — this is a bug, not a fill"
        )

    place_order = cancel_order = get_order_details = _blocked
    get_positions = get_funds_and_margin = _blocked

    def validate_token(self) -> bool:
        return True


# ═══════════════════════════════════════════════════════════════════════════
#  FILL MODEL
# ═══════════════════════════════════════════════════════════════════════════

class FillModel:
    """
    Turns an intention into a price, from the recorded two-sided quote.

    edge = 0.0  -> you pay the full spread (sell at bid, buy at ask)
    edge = 0.5  -> you get mid, i.e. the spread costs you nothing
    Default 0.25 assumes a resting limit that usually gets a quarter of the
    spread back. Raise it to flatter yourself; lower it to be honest about
    size.
    """

    def __init__(self, edge: float = 0.25, stress_mult: float = 0.5):
        self.edge = min(max(edge, 0.0), 0.5)
        self.stress_mult = min(max(stress_mult, 0.0), 1.0)

    def price(self, quote: dict, action: str, urgent: bool = False) -> Optional[float]:
        bid = float(quote.get("bid") or 0)
        ask = float(quote.get("ask") or 0)
        ltp = float(quote.get("ltp") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return ltp if ltp > 0 else None
        mid = (bid + ask) / 2.0
        edge = self.edge * (self.stress_mult if urgent else 1.0)
        if action == "SELL":
            return round(bid + (mid - bid) * (edge / 0.5), 2)
        return round(ask - (ask - mid) * (edge / 0.5), 2)


# ═══════════════════════════════════════════════════════════════════════════
#  RESULTS
# ═══════════════════════════════════════════════════════════════════════════

class Trade:
    __slots__ = (
        "trading_date", "strategy", "regime", "confidence", "dte",
        "entry_time", "exit_time", "lots", "entry_credit", "exit_debit",
        "gross_pts", "costs_rs", "pnl_rs", "exit_reason", "exit_priority",
        "entry_spot", "exit_spot", "strikes", "wing", "held_min",
    )

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def as_row(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}


class Results:
    def __init__(self, starting_capital: float):
        self.starting_capital = starting_capital
        self.trades: List[Trade] = []
        self.halted_days: List[str] = []
        self.rejections: Counter = Counter()
        self.rejection_detail: Dict[str, List[str]] = defaultdict(list)
        self.cycles = 0
        self.days: List[str] = []
        self.daily_pnl: Dict[str, float] = defaultdict(float)
        self.entries_considered = 0
        self.reason_log: List[Tuple[str, str, str]] = []   # (date, bucket, raw)
        self._cur_day = "?"

    # -- accumulation -----------------------------------------------------
    def add_trade(self, t: Trade) -> None:
        self.trades.append(t)
        self.daily_pnl[t.trading_date] += t.pnl_rs

    def add_rejection(self, reason: str) -> None:
        self.entries_considered += 1
        bucket = self._bucket(reason)
        self.reason_log.append((self._cur_day, bucket, str(reason)))
        self.rejections[bucket] += 1
        if len(self.rejection_detail[bucket]) < 5:
            self.rejection_detail[bucket].append(reason)

    @staticmethod
    def _bucket(reason: str) -> str:
        """
        Collapse a parameterised reason string into a stable category.

        The engine prefixes almost every refusal with NO_TRADE: or ABORT:,
        which carries no information - it is the *suffix* that names the gate
        that fired. Those prefixes are stripped first, otherwise the entire
        census collapses into a single useless NO_TRADE row.
        """
        r = str(reason or "unknown").strip()
        for _ in range(4):
            for pfx in ("NO_TRADE:", "ABORT:", "REJECT:", "SKIP:"):
                if r.upper().startswith(pfx):
                    r = r[len(pfx):].strip()
                    break
            else:
                break
        low = r.lower()
        for key in (
            "ev_gate", "credit_risk_ratio", "net_credit", "credit_ratio",
            "brokerage", "friction",
            "target_", "risk_budget", "wing_cost", "condor_weak_side",
            "margin", "daily", "confidence", "expanding", "cooldown",
            "consecutive", "entry_window", "day_move", "chain_stale",
            "no_strategy", "strike", "hard_exit", "lots", "or_not_established",
            "vix", "dte", "spread", "liquidity", "regime",
        ):
            if key in low:
                return key.rstrip("_")
        # Drop any trailing free text / numbers so variants of one gate group.
        head = re.split(r"[ \u2014\-(]", r, maxsplit=1)[0]
        return (head or r)[:36].upper()

    # -- statistics -------------------------------------------------------
    def summary(self) -> dict:
        n = len(self.trades)
        if n == 0:
            return {"trades": 0}
        pnls = [t.pnl_rs for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total = sum(pnls)
        gross_win = sum(wins)
        gross_loss = -sum(losses)

        equity, peak, max_dd = self.starting_capital, self.starting_capital, 0.0
        for d in sorted(self.daily_pnl):
            equity += self.daily_pnl[d]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        day_returns = [
            self.daily_pnl[d] / self.starting_capital for d in sorted(self.daily_pnl)
        ]
        mean = sum(day_returns) / len(day_returns) if day_returns else 0.0
        var = (
            sum((r - mean) ** 2 for r in day_returns) / (len(day_returns) - 1)
            if len(day_returns) > 1 else 0.0
        )
        sd = math.sqrt(var)
        sharpe = (mean / sd * math.sqrt(252)) if sd > 0 else 0.0

        return {
            "trades": n,
            "days": len(self.days),
            "trading_days_with_a_trade": len(self.daily_pnl),
            "win_rate": len(wins) / n,
            "total_pnl": total,
            "return_on_capital": total / self.starting_capital,
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "expectancy": total / n,
            "max_drawdown": max_dd,
            "sharpe_daily_ann": sharpe,
            "total_costs": sum(t.costs_rs for t in self.trades),
        }


# ═══════════════════════════════════════════════════════════════════════════
#  THE SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════

class BacktestRunner:

    def __init__(
        self,
        store: HistoricalStore,
        config: Config,
        fills: FillModel,
        verbose: bool = False,
    ):
        self.store = store
        self.config = config
        self.fills = fills
        self.verbose = verbose
        self.clock = SimClock()
        self.results = Results(float(config.starting_capital))
        self._scratch: Optional[str] = None
        self._sink = io.StringIO()

    @contextlib.contextmanager
    def _quiet(self):
        """
        The engine narrates every cycle to stdout - a 40-line data dashboard
        and a decision block. Over a 20-session replay that buries the one
        table that matters under tens of thousands of lines.
        """
        if self.verbose:
            yield
            return
        self._sink.seek(0)
        self._sink.truncate(0)
        with contextlib.redirect_stdout(self._sink):
            yield

    # -- engine wiring ----------------------------------------------------
    def _build(self):
        import data_engine, regime_engine, strategy_engine, execution_engine
        import calibration_engine

        self.clock.install()

        fd, self._scratch = tempfile.mkstemp(prefix="bt_", suffix=".db")
        os.close(fd)
        db = Database(Path(self._scratch))

        logger = logging.getLogger("backtest_engine")
        logger.handlers = [logging.NullHandler()]
        logger.setLevel(logging.ERROR if not self.verbose else logging.INFO)
        logger.propagate = False

        client = ReplayClient(self.clock)
        rl = RateLimiter(self.config.rate_limits)

        me = data_engine.MarketDataEngine(self.config, db, client, rl, logger)
        ce = calibration_engine.CalibrationEngine(db, self.config, logger)
        se = strategy_engine.StrategyEngine(self.config, db, me, ce, logger)
        xe = execution_engine.ExecutionEngine(
            self.config, db, me, ce, client, logger
        )
        rg = regime_engine.RegimeEngine(self.config, db, me, logger)

        # The engine narrates every cycle to stdout: a 40-line dashboard from
        # the data engine and a decision block from the strategy engine. Over a
        # 20-session replay that is tens of thousands of lines of noise around
        # the one table that matters, so both are muted unless --verbose.
        if not self.verbose:
            me._print_cycle_dashboard = lambda *_a, **_k: None

        self.db, self.client, self.me, self.se, self.xe = db, client, me, se, xe
        self.regime = rg
        self.merge_regime = getattr(regime_engine, "merge_regime_into_signals", None)

    def _teardown(self):
        try:
            self.db.close()
        except Exception:
            pass
        self.clock.uninstall()
        if self._scratch and Path(self._scratch).exists():
            try:
                os.unlink(self._scratch)
            except OSError:
                pass

    # -- regime step ------------------------------------------------------
    def _classify(self, signals: dict) -> dict:
        """
        Run the real regime layer exactly as main.run_one_cycle does:
        process_signals() then merge_regime_into_signals(). Without this the
        strategy engine sees no final_regime and refuses every entry, which is
        an artefact of the harness rather than a decision by the engine.
        """
        try:
            with self._quiet():
                snapshot = self.regime.process_signals(signals)
            if self.merge_regime is not None:
                return self.merge_regime(signals, snapshot)
        except Exception as exc:
            if self.verbose:
                print(f"  regime layer failed: {exc}")
        return signals

    # -- entry ------------------------------------------------------------
    def _open(self, params: dict, signals: dict, day: DaySlice) -> Optional[dict]:
        chain = self.me.last_chain or {}
        legs_spec = params.get("legs") or []
        lots = int(params.get("final_lots") or 1)
        filled = []
        for leg in legs_spec:
            k = float(leg["strike"])
            q = (chain.get(k) or {}).get(leg["option_type"]) or {}
            px = self.fills.price(q, leg["action"])
            if px is None or px <= 0:
                return None
            filled.append({
                "strike": k,
                "option_type": leg["option_type"],
                "action": leg["action"],
                "exec_price": px,
                "bid": float(q.get("bid") or 0),
                "ask": float(q.get("ask") or 0),
                "delta": float(q.get("delta") or 0),
                "gamma": float(q.get("gamma") or 0),
                "vega": float(q.get("vega") or 0),
                "theta": float(q.get("theta") or 0),
                "iv": float(q.get("iv") or 0),
                "oi": int(q.get("oi") or 0),
            })

        credit = sum(
            f["exec_price"] if f["action"] == "SELL" else -f["exec_price"]
            for f in filled
        )
        entry_costs = self.se._compute_costs(filled, lots, "ENTRY")["total_rupees"]

        pid = f"BT_{uuid.uuid4().hex[:12]}"
        now = self.clock.now()
        self.db.insert("positions", {
            "position_id": pid,
            "trading_date": day.trading_date,
            "strategy_name": params.get("strategy_name") or "UNKNOWN",
            "strategy_type": params.get("strategy_type"),
            "selection_reason": params.get("selection_reason"),
            "target_expiry": params.get("target_expiry"),
            "actual_dte": params.get("actual_dte"),
            "entry_time": now.isoformat(),
            "entry_spot": params.get("entry_spot"),
            "entry_vix": params.get("entry_vix"),
            "entry_credit": credit,
            "gross_credit": params.get("gross_credit"),
            "opening_straddle_at_entry": params.get("opening_straddle_at_entry"),
            "entry_costs_rupees": entry_costs,
            "stop_premium": params.get("stop_premium"),
            "target_premium": params.get("target_premium"),
            "price_stop_pts": params.get("price_stop_pts"),
            "price_stop_level_call": params.get("price_stop_level_call"),
            "price_stop_level_put": params.get("price_stop_level_put"),
            "hard_exit_time": params.get("hard_exit_time"),
            "final_lots": lots,
            "max_loss_per_lot": params.get("max_loss_per_lot"),
            "total_max_risk": params.get("total_max_risk"),
            "status": "OPEN",
            "last_known_premium": credit,
            "profit_lock_activated": 0,
            "paper_trade": 1,
            "raw_params_json": json.dumps(params, default=str),
            "final_regime_at_entry": params.get("final_regime_at_entry"),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        })
        for f in filled:
            self.db.insert("position_legs", {
                "position_id": pid,
                "strike": f["strike"],
                "option_type": f["option_type"],
                "action": f["action"],
                "qty": lots * self.config.lot_size,
                "entry_price": f["exec_price"],
                "entry_bid": f["bid"],
                "entry_ask": f["ask"],
                "entry_delta": f["delta"],
                "entry_gamma": f["gamma"],
                "entry_vega": f["vega"],
                "entry_theta": f["theta"],
                "entry_iv": f["iv"],
                "entry_oi": f["oi"],
                "quoted_mid_at_entry": (f["bid"] + f["ask"]) / 2.0,
                "leg_status": "OPEN",
            })

        return {
            "position_id": pid,
            "lots": lots,
            "entry_credit": credit,
            "entry_costs": entry_costs,
            "entry_time": now,
            "entry_spot": float(signals.get("spot") or 0),
            "filled": filled,
            "params": params,
            "signals_at_entry": {
                "final_regime": signals.get("final_regime"),
                "confidence_level": signals.get("confidence_level"),
                "actual_dte": signals.get("actual_dte"),
            },
        }

    # -- exit -------------------------------------------------------------
    def _close(self, live: dict, signals: dict, reason: str, priority: int,
               day: DaySlice) -> Trade:
        chain = self.me.last_chain or {}
        urgent = priority in (1, 2, 3, 7)
        exit_legs = []
        debit = 0.0
        for f in live["filled"]:
            q = (chain.get(f["strike"]) or {}).get(f["option_type"]) or {}
            close_action = "BUY" if f["action"] == "SELL" else "SELL"
            px = self.fills.price(q, close_action, urgent=urgent)
            if px is None or px <= 0:
                px = f["exec_price"]
            exit_legs.append({
                "action": close_action,
                "option_type": f["option_type"],
                "exec_price": px,
            })
            debit += px if close_action == "BUY" else -px

        lots = live["lots"]
        exit_costs = self.se._compute_costs(exit_legs, lots, "EXIT")["total_rupees"]
        gross_pts = live["entry_credit"] - debit
        costs = live["entry_costs"] + exit_costs
        pnl = gross_pts * self.config.lot_size * lots - costs

        now = self.clock.now()
        self.db.update("positions", {"status": "CLOSED",
                                     "updated_at": now.isoformat()},
                       {"position_id": live["position_id"]})
        self.db.execute(
            "UPDATE position_legs SET leg_status='CLOSED' WHERE position_id=?",
            (live["position_id"],),
        )

        strikes = "/".join(
            f"{f['action'][0]}{f['option_type'][0].upper()}{f['strike']:.0f}"
            for f in live["filled"]
        )
        return Trade(
            trading_date=day.trading_date,
            strategy=live["params"].get("strategy_name"),
            regime=live["signals_at_entry"].get("final_regime"),
            confidence=live["signals_at_entry"].get("confidence_level"),
            dte=live["signals_at_entry"].get("actual_dte"),
            entry_time=live["entry_time"].strftime("%H:%M:%S"),
            exit_time=now.strftime("%H:%M:%S"),
            lots=lots,
            entry_credit=round(live["entry_credit"], 2),
            exit_debit=round(debit, 2),
            gross_pts=round(gross_pts, 2),
            costs_rs=round(costs, 2),
            pnl_rs=round(pnl, 2),
            exit_reason=reason,
            exit_priority=priority,
            entry_spot=round(live["entry_spot"], 1),
            exit_spot=round(float(signals.get("spot") or 0), 1),
            strikes=strikes,
            wing=live["params"].get("wing_width"),
            held_min=int((now - live["entry_time"]).total_seconds() / 60),
        )

    # -- one session ------------------------------------------------------
    def run_day(self, trading_date: str) -> None:
        day = self.store.load_day(trading_date)
        if len(day.cycles) < 5:
            if self.verbose:
                print(f"  {trading_date}: only {len(day.cycles)} snapshots — skipped")
            return

        self.results.days.append(trading_date)
        self.results._cur_day = trading_date
        state = self.me.state
        state["daily_halted"] = False
        live: Optional[dict] = None
        day_pnl = 0.0

        for capture_time in day.cycles:
            dt = day.cycle_dt(capture_time)
            self.clock.set(dt)
            self.client.point(day, capture_time)

            try:
                with self._quiet():
                    signals = self.me.run_cycle()
            except Exception as exc:
                if self.verbose:
                    print(f"  {trading_date} {dt:%H:%M}: run_cycle failed: {exc}")
                continue
            self.results.cycles += 1

            signals = self._classify(signals)
            state["current_capital"] = self.results.starting_capital + \
                sum(self.results.daily_pnl.values())
            state["daily_pnl"] = day_pnl

            # ── manage an open position first ─────────────────────────────
            if live is not None:
                row = self.db.query_one(
                    "SELECT * FROM positions WHERE position_id=?",
                    (live["position_id"],),
                )
                try:
                    with self._quiet():
                        action, priority, ctx = self.xe.monitor_position(row, signals)
                except Exception as exc:
                    if self.verbose:
                        print(f"  monitor_position failed: {exc}")
                    action, priority, ctx = "HOLD", 0, {}

                if action != "HOLD" and not action.startswith("TIGHTEN"):
                    reason = ctx.get("reason_detail") or action
                    t = self._close(live, signals, reason, priority, day)
                    self.results.add_trade(t)
                    day_pnl += t.pnl_rs
                    live = None
                    state["daily_pnl"] = day_pnl
                    state["consecutive_stops"] = (
                        int(state.get("consecutive_stops", 0)) + 1
                        if t.pnl_rs < 0 else 0
                    )
                    state["last_stop_time"] = (
                        self.clock.now().isoformat() if t.pnl_rs < 0 else
                        state.get("last_stop_time")
                    )
                    if self.verbose:
                        print(f"  {trading_date} {dt:%H:%M} EXIT  "
                              f"{t.exit_reason[:34]:34s} pnl={t.pnl_rs:>10,.0f}")
                    continue

            # ── daily loss halt ──────────────────────────────────────────
            # main.check_daily_loss_halt stops new entries once the session is
            # down max_daily_loss_pct of day-start capital. Without mirroring
            # it here the simulation keeps trading through days the live
            # engine would have shut off, which flatters bad days.
            if not state.get("daily_halted"):
                day_start = state["current_capital"] - day_pnl
                if day_start > 0 and (max(0.0, -day_pnl) / day_start) >= \
                        self.config.max_daily_loss_pct:
                    state["daily_halted"] = True
                    self.results.halted_days.append(trading_date)
                    if self.verbose:
                        print(f"  {trading_date}: daily loss limit hit "
                              f"({day_pnl:,.0f}) — no further entries")
            if state.get("daily_halted") and live is None:
                self.results.add_rejection("daily_loss_halt")
                continue

            # ── otherwise consider a new entry ────────────────────────────
            if live is None:
                try:
                    with self._quiet():
                        decision = self.se.decide(signals)
                except Exception as exc:
                    if self.verbose:
                        print(f"  decide() failed: {exc}")
                    continue

                if decision.get("action") == "ENTER":
                    params = decision.get("params") or {}
                    live = self._open(params, signals, day)
                    if live is not None:
                        state["last_entry_time"] = self.clock.now().isoformat()
                        state["entry_count"] = int(state.get("entry_count", 0)) + 1
                        if self.verbose:
                            print(f"  {trading_date} {dt:%H:%M} ENTER "
                                  f"{params.get('strategy_name')} "
                                  f"credit={live['entry_credit']:.2f} "
                                  f"lots={live['lots']}")
                    else:
                        self.results.add_rejection("fill_unavailable")
                else:
                    self.results.add_rejection(decision.get("reason", "unknown"))

        # ── forced flat at the last snapshot of the session ──────────────
        if live is not None:
            self.clock.set(day.cycle_dt(day.cycles[-1]))
            self.client.point(day, day.cycles[-1])
            try:
                with self._quiet():
                    signals = self.me.run_cycle()
            except Exception:
                signals = {"spot": live["entry_spot"]}
            t = self._close(live, signals, "END_OF_DATA_FORCED_FLAT", 7, day)
            self.results.add_trade(t)

    # -- driver -----------------------------------------------------------
    def run(self, dates: List[str]) -> Results:
        self._build()
        try:
            for d in dates:
                self.run_day(d)
        finally:
            self._teardown()
        return self.results


# ═══════════════════════════════════════════════════════════════════════════
#  REPORTING
# ═══════════════════════════════════════════════════════════════════════════

def hr(char: str = "─", n: int = 78) -> str:
    return char * n


def print_audit(store: HistoricalStore) -> int:
    a = store.audit()
    days = a["days"]
    print()
    print(hr("═"))
    print("RECORDED DATA AUDIT")
    print(hr("═"))
    if not days:
        print("""
  No option_chain_snapshot rows found.

  This is expected on a fresh install: the table is written by
  MarketDataEngine.run_cycle(), so it only fills up on sessions the engine has
  actually run. Until then there is nothing to replay and no way to measure
  whether any threshold in this engine is right.

  To start collecting, run the engine in paper mode through live sessions:

      PAPER_TRADE_MODE=true python main.py

  Each session writes ~450 chain snapshots and ~375 one-minute bars. Come back
  when you have 20+ sessions; fewer than that cannot separate a real edge from
  noise at this trade frequency.
""")
        return 1

    print(f"  {'date':<12} {'cycles':>7} {'strikes':>8} {'1m bars':>8}  quality")
    print(f"  {hr('-', 62)}")
    usable = 0
    for d in days:
        ok = d["cycles"] >= 30 and d["bars"] >= 200 and d["strikes"] >= 15
        usable += 1 if ok else 0
        print(f"  {d['trading_date']:<12} {d['cycles']:>7} {d['strikes']:>8} "
              f"{d['bars']:>8}  {'usable' if ok else 'THIN'}")
    print(f"  {hr('-', 62)}")
    print(f"  {len(days)} session(s) recorded, {usable} usable.")
    print()
    if usable < 20:
        print(f"  NOT ENOUGH DATA. {usable} usable session(s) is far below the ~20")
        print("  minimum, and 20 is itself only enough to reject something obviously")
        print("  broken - not to confirm an edge. At roughly one trade per session,")
        print("  20 sessions is a 20-trade sample: a 60% win rate and a 40% win rate")
        print("  are statistically indistinguishable at that size.")
    else:
        print("  Enough to run. Hold out at least the last third of the dates and do")
        print("  not look at them until the parameters are fixed.")
    print()
    return 0 if usable else 1


# Gate order as actually implemented in StrategyEngine.decide(). Verified by
# reading the function, not assumed: the regime verdict is consumed first,
# then safety interlocks, then the entry window, then position limits, then
# structure validation (credit_risk_ratio at strategy_engine.py:1244), and
# the EV gate last (strategy_engine.py:1304).
#
# This ordering is what makes the raw census misleading. A gate late in the
# chain is only ever offered the candidates every earlier gate approved, so
# its share of TOTAL cycles understates it badly. The number that matters is
# the conditional one: of the candidates that reached this gate, how many did
# it kill.
STAGE_ORDER: List[Tuple[str, Tuple[str, ...]]] = [
    ("regime verdict", (
        "VOL_NEUTRAL", "VOL_BUY_OPTIONS", "CHOPPY_MARKET", "regime",
        "RANGE_UNCLEAR", "STRADDLE_EXPLOSION", "or_not_established",
        "NO_CLEAR", "UNCLEAR", "TRENDING", "EXPANSION")),
    ("safety interlocks", (
        "vix", "circuit_breaker", "expanding", "spiking", "daily_loss_halt",
        "daily", "abort")),
    ("entry window", ("entry_window",)),
    ("position limits", (
        "max_concurrent", "max_entries", "position_already_open",
        "cooldown", "consecutive")),
    ("structure build", (
        "no_strategy", "strike", "lots", "net_credit", "credit_ratio",
        "credit_risk", "wing_cost", "condor_weak_side", "friction",
        "brokerage", "risk_budget", "target", "margin", "chain_stale",
        "fill_unavailable")),
    ("EV gate", ("ev_gate",)),
]


def _stage_of(bucket: str) -> int:
    b = bucket.lower()
    for i, (_, keys) in enumerate(STAGE_ORDER):
        for k in keys:
            if k.lower() in b:
                return i
    return len(STAGE_ORDER) - 1


_NUM = re.compile(r"(-?\d+\.?\d*)")


def _margin_of(raw: str) -> Optional[float]:
    """
    Pull a (value, threshold) pair out of a reason string such as
    'ev_-6.60pts_below_min_1.47pts' or 'credit_risk_ratio_0.41_below_min_0.50'
    and return how far short it fell. Returns None when the reason carries no
    numbers, which is itself worth knowing.
    """
    low = raw.lower()
    if not any(w in low for w in ("below", "above", "min_", "max_", "_vs_")):
        return None
    nums = [float(x) for x in _NUM.findall(raw)]
    if len(nums) < 2:
        return None
    return abs(nums[-1] - nums[0])


def print_funnel(res: "Results") -> None:
    """
    Where candidates die, in the order the engine actually kills them.
    """
    if not res.reason_log:
        return
    total = len(res.reason_log) + len(res.trades)

    blocked_by_stage: Dict[int, Counter] = defaultdict(Counter)
    for _d, bucket, _raw in res.reason_log:
        blocked_by_stage[_stage_of(bucket)][bucket] += 1

    print()
    print(hr())
    print("ENTRY FUNNEL  (gates in the order decide() applies them)")
    print(hr())
    print("  A late gate only ever sees what the earlier gates let through, so")
    print("  its share of all cycles understates it. Read the pass rate.")
    print()
    print(f"  {'stage':<20} {'blocked':>8} {'reached':>8} {'passed':>8} "
          f"{'pass rate':>10}   top gate")
    print(f"  {hr('-', 88)}")

    reached = total
    terminal = None
    for i, (name, _keys) in enumerate(STAGE_ORDER):
        blocked = sum(blocked_by_stage[i].values())
        if blocked == 0 and reached == total and i == 0:
            pass
        passed = reached - blocked
        rate = (100.0 * passed / reached) if reached else 0.0
        top = blocked_by_stage[i].most_common(1)
        topname = f"{top[0][0]} ({top[0][1]})" if top else "-"
        if blocked or reached != total:
            print(f"  {name:<20} {blocked:>8} {reached:>8} {passed:>8} "
                  f"{rate:>9.0f}%   {topname}")
        if blocked and passed == 0 and terminal is None:
            terminal = (name, blocked)
        reached = passed
        if reached <= 0:
            break

    print(f"  {hr('-', 88)}")
    print(f"  {'ENTERED':<20} {'':>8} {'':>8} {len(res.trades):>8}")

    if terminal:
        name, blocked = terminal
        print()
        print(f"  >> TERMINAL GATE: '{name}' rejected {blocked} of {blocked} "
              f"candidates (0% pass).")
        print(f"     Nothing downstream of it was ever evaluated. If you change")
        print(f"     one thing, change this. Every other gate is speculation")
        print(f"     until this one lets something through.")

    # -- near-miss margins ------------------------------------------------
    margins: Dict[str, List[float]] = defaultdict(list)
    nonum: Counter = Counter()
    for _d, bucket, raw in res.reason_log:
        m = _margin_of(raw)
        if m is None:
            nonum[bucket] += 1
        else:
            margins[bucket].append(m)
    if margins:
        print()
        print("  NEAR-MISS MARGINS  (how far short, when the reason says)")
        print(f"  {hr('-', 74)}")
        print(f"  {'gate':<34} {'n':>5} {'median':>10} {'best':>10}")
        for k in sorted(margins, key=lambda x: -len(margins[x]))[:8]:
            v = sorted(margins[k])
            med = v[len(v) // 2]
            print(f"  {k:<34} {len(v):>5} {med:>10.2f} {min(v):>10.2f}")
        print()
        print("  A gate missing by a hair on most cycles is mis-calibrated.")
        print("  A gate missing by a mile is telling you the trade was not there.")

    # -- episodes ---------------------------------------------------------
    # 787 consecutive VOL_NEUTRAL cycles is ONE market condition observed 787
    # times, not 787 independent observations. Counting contiguous runs is a
    # far better guide to how much evidence you actually have.
    episodes: Counter = Counter()
    last = None
    for d, bucket, _raw in res.reason_log:
        keyed = (d, bucket)
        if keyed != last:
            episodes[bucket] += 1
        last = keyed
    print()
    print("  EPISODES  (contiguous runs - the honest sample size)")
    print(f"  {hr('-', 74)}")
    print(f"  {'gate':<34} {'cycles':>8} {'episodes':>10} {'cycles/ep':>11}")
    for k, c in res.rejections.most_common(8):
        e = episodes.get(k, 0)
        print(f"  {k:<34} {c:>8} {e:>10} {(c / e if e else 0):>11.0f}")
    print()
    print("  Treat 'episodes', not 'cycles', as n when judging significance.")


_EVF = re.compile(
    r"ev_(?P<ev>-?[\d.]+)pts_below_min_(?P<min>[\d.]+)pts"
    r".*?p_win=(?P<p_win>[\d.]+)"
    r".*?p_tail=(?P<p_tail>[\d.]+)"
    r".*?rew=(?P<rew>-?[\d.]+)"
    r".*?stop=(?P<stop>-?[\d.]+)"
    r".*?tail=(?P<tail>-?[\d.]+)"
    r".*?fric=(?P<fric>-?[\d.]+)"
)


def print_ev_decomposition(res: "Results") -> None:
    """
    Break the EV gate's verdict into its four terms.

    'EV was -6.6' is not actionable. 'The tail term cost 4.4 points and
    friction cost 4.2, against a reward of 3.3' tells you exactly which
    assumption to argue with. The engine already prints every input in the
    rejection string; this just stops them being thrown away.
    """
    rows = []
    for _d, bucket, raw in res.reason_log:
        if "ev_gate" not in bucket:
            continue
        m = _EVF.search(raw)
        if m:
            rows.append({k: float(v) for k, v in m.groupdict().items()})
    if not rows:
        return

    def med(key: str) -> float:
        v = sorted(r[key] for r in rows)
        return v[len(v) // 2]

    n = len(rows)
    p_win, p_tail = med("p_win"), med("p_tail")
    rew, stop, tail, fric = med("rew"), med("stop"), med("tail"), med("fric")
    pwe = p_win * (1.0 - p_tail)
    pst = max(1.0 - pwe - p_tail, 0.0)

    c_rew, c_stop = pwe * rew, -pst * stop
    c_tail, c_fric = -p_tail * tail, -fric
    ev_med = med("ev")

    print()
    print(hr())
    print(f"EV DECOMPOSITION  (median of {n} rejected candidates)")
    print(hr())
    print("  Which term is actually killing expected value.")
    print()
    print(f"  {'term':<26} {'probability':>12} {'points':>10} {'contribution':>14}")
    print(f"  {hr('-', 66)}")
    print(f"  {'reward (target hit)':<26} {pwe:>12.3f} {rew:>10.2f} {c_rew:>+14.2f}")
    print(f"  {'stop loss':<26} {pst:>12.3f} {stop:>10.2f} {c_stop:>+14.2f}")
    print(f"  {'tail (stop jumped)':<26} {p_tail:>12.3f} {tail:>10.2f} {c_tail:>+14.2f}")
    print(f"  {'round-trip friction':<26} {'':>12} {fric:>10.2f} {c_fric:>+14.2f}")
    print(f"  {hr('-', 66)}")
    print(f"  {'EXPECTED VALUE':<26} {'':>12} {'':>10} "
          f"{c_rew + c_stop + c_tail + c_fric:>+14.2f}"
          f"   (engine: {ev_med:+.2f})")
    print(f"  {'required minimum':<26} {'':>12} {'':>10} {med('min'):>+14.2f}")

    worst = min(
        [("tail", c_tail), ("friction", c_fric), ("stop", c_stop)],
        key=lambda x: x[1],
    )
    print()
    print(f"  Largest drag: {worst[0].upper()} at {worst[1]:+.2f} pts.")
    if c_fric < -2.0:
        print(f"  Friction of {fric:.2f} pts is heavy - that is roughly a 1-lot")
        print(f"  cost base. Friction per point falls fast with size, so check")
        print(f"  whether sizing collapsed to 1 lot before blaming the gate.")
    if -c_tail > c_rew:
        print(f"  The tail term alone ({c_tail:+.2f}) exceeds the entire reward")
        print(f"  ({c_rew:+.2f}). With tail_loss = 0.80 x (wing - credit), a wide")
        print(f"  wing on a thin credit makes this gate close to unsatisfiable.")
    print()
    print("  This is the engine's OPINION of these trades, not their outcome.")
    print("  Only replaying the rejected structures can say if it was right.")


def print_report(res: Results, config: Config, args) -> None:
    s = res.summary()
    print()
    print(hr("═"))
    print("BACKTEST RESULT")
    print(hr("═"))
    print(f"  sessions replayed : {len(res.days)}")
    print(f"  engine cycles     : {res.cycles:,}")
    print(f"  entry decisions   : {res.entries_considered + s.get('trades', 0):,}")
    print(f"  fill model        : edge={args.fill_edge:.2f} "
          f"stress={args.stress_exit:.2f}")
    print(f"  capital           : Rs {config.starting_capital:,.0f}")

    if not s.get("trades"):
        print()
        print("  NO TRADES TAKEN.")
        print()
        print("  That is a result, not a failure of the harness. The rejection")
        print("  census below says which gate was responsible. If one gate accounts")
        print("  for most of it, that is the number to examine first - and it is")
        print("  now a measurement rather than an opinion.")
    else:
        print()
        print(f"  trades            : {s['trades']}")
        print(f"  win rate          : {s['win_rate']*100:.1f}%")
        print(f"  total P&L         : Rs {s['total_pnl']:>12,.0f}")
        print(f"  return on capital : {s['return_on_capital']*100:>12.2f}%")
        print(f"  expectancy/trade  : Rs {s['expectancy']:>12,.0f}")
        print(f"  avg win / avg loss: Rs {s['avg_win']:,.0f} / Rs {s['avg_loss']:,.0f}")
        print(f"  profit factor     : {s['profit_factor']:.2f}")
        print(f"  max drawdown      : Rs {s['max_drawdown']:,.0f}")
        print(f"  Sharpe (daily ann): {s['sharpe_daily_ann']:.2f}")
        print(f"  total costs paid  : Rs {s['total_costs']:,.0f}"
              f"   ({s['total_costs']/max(abs(s['total_pnl']),1)*100:.0f}% of |P&L|)")

        n = s["trades"]
        if n < 30:
            print()
            print(f"  ⚠  {n} trades is not a sample. The standard error on a win rate")
            print(f"     at n={n} is about {50/math.sqrt(n):.0f} percentage points. Any")
            print("     conclusion drawn from these numbers is noise with a decimal")
            print("     point attached.")

    # -- exits ------------------------------------------------------------
    if res.trades:
        print()
        print(hr())
        print("EXIT ATTRIBUTION  (where the P&L actually comes from)")
        print(hr())
        by_reason: Dict[str, List[float]] = defaultdict(list)
        for t in res.trades:
            key = f"P{t.exit_priority} {str(t.exit_reason).split('_')[0][:20]}"
            by_reason[key].append(t.pnl_rs)
        print(f"  {'exit':<28} {'n':>4} {'total':>12} {'avg':>10}")
        print(f"  {hr('-', 58)}")
        for k in sorted(by_reason, key=lambda x: -abs(sum(by_reason[x]))):
            v = by_reason[k]
            print(f"  {k:<28} {len(v):>4} {sum(v):>12,.0f} {sum(v)/len(v):>10,.0f}")

    if res.halted_days:
        print()
        print(f"  daily loss limit hit on {len(res.halted_days)} session(s): "
              f"{', '.join(res.halted_days[:6])}"
              f"{' ...' if len(res.halted_days) > 6 else ''}")

    # -- funnel -----------------------------------------------------------
    print_funnel(res)
    print_ev_decomposition(res)

    # -- rejections -------------------------------------------------------
    print()
    print(hr())
    print("REJECTION CENSUS  (why entries did not happen)")
    print(hr())
    if not res.rejections:
        print("  none recorded")
    else:
        total = sum(res.rejections.values())
        print(f"  {'gate':<38} {'count':>7} {'share':>7}   example")
        print(f"  {hr('-', 74)}")
        for gate, cnt in res.rejections.most_common(14):
            ex = (res.rejection_detail[gate][0] or "")[:70]
            print(f"  {gate:<38} {cnt:>7} {cnt/total*100:>6.1f}%   {ex}")
    print()
    print(hr("═"))
    print("""
  HOW TO READ THIS
  ----------------
  * Fills are modelled, not observed. Nothing here includes partial fills,
    rejections or latency, and the simulated monitoring loop is only as fast
    as the recorded snapshot cadence - a stop hit and recovered between two
    snapshots is invisible. Both of those bias the result OPTIMISTIC.
  * If you tuned any threshold while looking at these dates, this is an
    in-sample number and it is not evidence. Re-run on dates you have not
    looked at.
  * The rejection census is the actionable half of this report.
""")


def write_csv(res: Results, path: str) -> None:
    import csv
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(Trade.__slots__))
        w.writeheader()
        for t in res.trades:
            w.writerow(t.as_row())
    print(f"  trade blotter -> {p}")


# ═══════════════════════════════════════════════════════════════════════════
#  SYNTHETIC HARNESS SELF-TEST
# ═══════════════════════════════════════════════════════════════════════════

def _bs(S: float, K: float, T: float, sig: float, typ: str) -> Tuple[float, float]:
    if T <= 0 or sig <= 0:
        intrinsic = max(0.0, (S - K) if typ == "call" else (K - S))
        return intrinsic, (1.0 if (S > K and typ == "call") else 0.0)

    def N(x: float) -> float:
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if typ == "call":
        return S * N(d1) - K * N(d2), N(d1)
    return K * N(-d2) - S * N(-d1), -N(-d1)


def build_synthetic_db(path: str, n_days: int = 3, seed: int = 11) -> List[str]:
    """
    Fabricate a database in the live schema so the harness can be tested end
    to end without any recorded sessions.

    THIS IS A TEST OF THE PLUMBING, NOT OF THE STRATEGY. A geometric Brownian
    motion has no trends, no pinning, no gaps, no liquidity structure and no
    volatility risk premium. Any P&L it produces is an artefact of the
    generator and means nothing whatsoever about NIFTY.
    """
    rng = random.Random(seed)
    db = Database(Path(path))
    dates: List[str] = []

    d = date(2026, 3, 3)  # a Tuesday
    for _ in range(n_days):
        while d.weekday() > 4:
            d += timedelta(days=1)
        ds = d.isoformat()
        dates.append(ds)

        # Implied and realised vol are deliberately DIFFERENT. A single sigma
        # would give a variance premium of zero, the vol regime would come out
        # NEUTRAL and the engine would never enter, so the harness would only
        # ever exercise the rejection path. A rich-but-plausible premium
        # (IV ~12.2, RV ~7.6) makes entries reachable so that _open, the exit
        # ladder, the fill model and the cost accounting all get covered.
        # It is a fixture, not a forecast.
        iv_base = 0.122 * rng.uniform(0.97, 1.03)
        sigma   = iv_base * 0.62
        spot0 = 24000.0 + rng.gauss(0, 120)
        spot = spot0
        bars = []
        # Per-minute vol. The wicks are drawn from the SAME sigma as the
        # close-to-close path, so that the Parkinson high-low estimator the
        # engine actually uses recovers roughly this sigma. Arbitrary wick
        # noise would inflate measured RV until the variance premium vanished
        # and the vol regime came out NEUTRAL on every cycle.
        sig_m = sigma / math.sqrt(252 * 375)
        for m in range(375):
            o = spot
            spot = o * math.exp(rng.gauss(0, sig_m))
            c = spot
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.5 * sig_m)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.5 * sig_m)))
            t = (datetime.combine(d, dtime(9, 15)) + timedelta(minutes=m))
            bars.append((t, c, h, lo))
            db.insert("intraday_candles", {
                "trading_date": ds,
                "candle_time": t.strftime("%H:%M:%S"),
                "interval_min": 1,
                "open": round(o, 2), "high": round(h, 2),
                "low": round(lo, 2), "close": round(c, 2),
                "volume": 100000, "source": "synthetic",
            })

        vix = iv_base * 100 * rng.uniform(0.98, 1.02)
        for m in range(5, 340, 5):          # a snapshot every 5 minutes
            t, sp, _, _ = bars[m]
            T = max((375 - m) / 375.0 / 252.0, 1e-6)
            atm = round(sp / 50) * 50
            for off in range(-14, 15):
                K = float(atm + off * 50)
                for typ in ("call", "put"):
                    iv = iv_base * (1 + 0.0018 * abs(off) +
                                    (0.004 * abs(off) if (typ == "put" and off < 0) else 0))
                    px, dl = _bs(sp, K, T, iv, typ)
                    px = max(px, 0.05)
                    spread = max(0.10, min(0.60, px * 0.01))
                    db.insert("option_chain_snapshot", {
                        "capture_time": t.isoformat(),
                        "trading_date": ds,
                        "expiry": ds,
                        "strike": K,
                        "option_type": typ,
                        "bid": round(px - spread / 2, 2),
                        "ask": round(px + spread / 2, 2),
                        "ltp": round(px, 2),
                        "oi": max(1200, 90000 - abs(off) * 4000),
                        "volume": 5000,
                        "iv": iv, "delta": dl,
                        "gamma": 0.0002, "theta": -2.0, "vega": 5.0,
                        "data_timestamp": t.isoformat(),
                        "spot_at_capture": round(sp, 2),
                        "vix_at_capture": round(vix, 2),
                    })
        d += timedelta(days=1)

    db.close()
    return dates


def _forced_round_trip(store: "HistoricalStore", cfg: Config,
                       dates: List[str]) -> dict:
    """
    Drive one position through _open -> monitor -> _close by hand.

    Nothing here is a trading decision. The legs are picked geometrically
    (+-200 shorts, 100-point wings around the ATM) purely so that the entry
    persistence, the fill model, the real _compute_costs call and the P&L
    identity all get executed at least once per self-test run.
    """
    day = store.load_day(dates[0])
    runner = BacktestRunner(store, cfg, FillModel(0.25, 0.5), verbose=False)
    runner._build()
    try:
        entry_ct = day.cycles[len(day.cycles) // 4]
        exit_ct = day.cycles[-3]

        runner.clock.set(day.cycle_dt(entry_ct))
        runner.client.point(day, entry_ct)
        with runner._quiet():
            signals = runner.me.run_cycle()

        chain = runner.me.last_chain or {}
        if not chain:
            raise AssertionError("replay produced an empty option chain")
        spot = float(signals.get("spot") or 0)
        atm = round(spot / 50.0) * 50.0
        want = [(atm + 200, "call", "SELL"), (atm + 300, "call", "BUY"),
                (atm - 200, "put", "SELL"), (atm - 300, "put", "BUY")]
        legs = []
        for k, typ, act in want:
            q = (chain.get(float(k)) or {}).get(typ)
            if not q:
                raise AssertionError(f"strike {k} {typ} missing from replay chain")
            legs.append({"strike": float(k), "option_type": typ,
                         "action": act, "exec_price": q.get("ltp")})

        params = {
            "strategy_name": "HARNESS_FORCED_CONDOR",
            "strategy_type": "IRON_CONDOR",
            "selection_reason": "self_test_only",
            "legs": legs,
            "final_lots": 1,
            "num_legs": 4,
            "wing_width": 100,
            "actual_dte": 0,
            "target_expiry": day.trading_date,
            "entry_spot": spot,
            "entry_vix": signals.get("vix"),
            "hard_exit_time": "15:00:00",
            "final_regime_at_entry": "HARNESS",
        }

        live = runner._open(params, signals, day)
        if live is None:
            raise AssertionError("_open returned None - fill model rejected the chain")

        row = runner.db.query_one(
            "SELECT * FROM positions WHERE position_id=?", (live["position_id"],))
        assert row and row["status"] == "OPEN", "position row not written as OPEN"
        leg_rows = runner.db.query(
            "SELECT * FROM position_legs WHERE position_id=?",
            (live["position_id"],))
        assert len(leg_rows) == 4, f"expected 4 leg rows, got {len(leg_rows)}"

        # the real monitor must at least run against a live position
        runner.clock.set(day.cycle_dt(exit_ct))
        runner.client.point(day, exit_ct)
        with runner._quiet():
            signals = runner.me.run_cycle()
            runner.xe.monitor_position(dict(row), signals)

        trade = runner._close(live, signals, "HARNESS_FORCED_EXIT", 7, day)

        row = runner.db.query_one(
            "SELECT status FROM positions WHERE position_id=?",
            (live["position_id"],))
        assert row["status"] == "CLOSED", "position not marked CLOSED"
        open_legs = runner.db.query_one(
            "SELECT COUNT(*) AS n FROM position_legs "
            "WHERE position_id=? AND leg_status!='CLOSED'",
            (live["position_id"],))
        assert open_legs["n"] == 0, "leg rows left open after close"

        expect = round(
            trade.gross_pts * cfg.lot_size * trade.lots - trade.costs_rs, 2)
        assert abs(trade.pnl_rs - expect) < 0.01, (
            f"P&L identity broken: {trade.pnl_rs} != {expect}")
        assert trade.costs_rs > 0, "round trip cost zero - _compute_costs not wired"

        exit_costs = trade.costs_rs - live["entry_costs"]
        return {
            "legs": len(leg_rows),
            "lots": trade.lots,
            "entry_credit": trade.entry_credit,
            "entry_costs": live["entry_costs"],
            "exit_costs": exit_costs,
            "gross_pts": trade.gross_pts,
            "pnl_rs": trade.pnl_rs,
            "held_min": trade.held_min,
        }
    finally:
        runner._teardown()


def self_test() -> int:
    print()
    print(hr("═"))
    print("BACKTEST HARNESS SELF-TEST")
    print(hr("═"))
    print("""
  Building a synthetic database in the live schema and replaying it through
  the real engine. This checks the PLUMBING - clock injection, the replay
  client, signal construction, the decision path, the exit ladder, fills,
  cost accounting and reporting.

  It says nothing at all about whether the strategy makes money. The price
  process is a driftless random walk.
""")
    tmp = tempfile.mkdtemp(prefix="bt_selftest_")
    src = str(Path(tmp) / "synthetic.db")
    print("  generating 3 synthetic sessions ...")
    dates = build_synthetic_db(src, n_days=3)

    store = HistoricalStore(src)
    audit = store.audit()
    assert len(audit["days"]) == 3, f"expected 3 days, got {len(audit['days'])}"
    print(f"  {len(audit['days'])} sessions, "
          f"{sum(d['cycles'] for d in audit['days'])} snapshots  [OK]")

    cfg = load_config()
    runner = BacktestRunner(store, cfg, FillModel(0.25, 0.5), verbose=False)
    res = runner.run(dates)

    print(f"  replayed {res.cycles} cycles across {len(res.days)} sessions  [OK]")
    assert res.cycles > 100, f"too few cycles replayed: {res.cycles}"
    assert len(res.days) == 3, f"expected 3 sessions, got {len(res.days)}"

    decisions = res.entries_considered + len(res.trades)
    assert decisions > 0, "the strategy engine was never asked for a decision"
    print(f"  {decisions} entry decisions, {len(res.trades)} trades, "
          f"{sum(res.rejections.values())} rejections  [OK]")

    for t in res.trades:
        expect = round(
            t.gross_pts * cfg.lot_size * t.lots - t.costs_rs, 2
        )
        assert abs(t.pnl_rs - expect) < 0.05, (
            f"P&L accounting mismatch: {t.pnl_rs} vs {expect}"
        )
    if res.trades:
        print(f"  P&L identity verified on {len(res.trades)} trades  [OK]")

    s = res.summary()
    assert isinstance(s, dict)
    print("  summary statistics computed  [OK]")

    # ── forced round trip ────────────────────────────────────────────────
    # A driftless random walk mostly classifies as CHOPPY, so the replay
    # above may legitimately reject every cycle and never touch _open,
    # the fill model, the exit fill or the cost accounting. Those are the
    # parts of the harness most likely to be silently wrong, so they are
    # exercised directly with a hand-built iron condor.
    #
    # This trade is FORCED. The strategy engine did not choose it and its
    # P&L is meaningless. What is being checked is that a position round
    # trips through the real schema and that the money adds up.
    print()
    print("  forced round trip (harness coverage, not a signal):")
    fr = _forced_round_trip(store, cfg, dates)
    print(f"    opened {fr['legs']} legs @ credit {fr['entry_credit']:.2f} pts, "
          f"{fr['lots']} lot(s)  [OK]")
    print(f"    entry costs Rs {fr['entry_costs']:,.0f}, "
          f"exit costs Rs {fr['exit_costs']:,.0f}  [OK]")
    print(f"    closed after {fr['held_min']} min, "
          f"gross {fr['gross_pts']:+.2f} pts, net Rs {fr['pnl_rs']:+,.0f}  [OK]")
    print(f"    positions/position_legs rows written and closed  [OK]")
    print(f"    P&L identity reconciles to the paisa  [OK]")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("  HARNESS SELF-TEST PASSED")
    print("  (again: this validates the simulator, not the strategy)")
    print()
    return 0


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def install_warning_dedupe() -> None:
    """
    Print each distinct warning once instead of once per cycle.

    pandas calls warnings.catch_warnings() internally, which bumps the filter
    version and invalidates every module's __warningregistry__. Since the
    replay loop goes through pandas on every single cycle, Python's built-in
    "show this location only once" behaviour is destroyed and a single
    deprecated call site emits one line per cycle - thousands of lines that
    bury the report and look like a hang.

    This dedupes on (category, filename, lineno) in our own dict, which no
    amount of filter mutation can clear. Nothing is suppressed: every distinct
    warning is still shown, and a count of what was collapsed is available.
    """
    seen: Dict[tuple, int] = {}
    original = warnings.showwarning

    def showwarning(message, category, filename, lineno, file=None, line=None):
        key = (category, filename, lineno)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 1:
            original(message, category, filename, lineno, file, line)
        elif seen[key] == 2:
            sys.stderr.write(
                f"  ... further identical warnings from {Path(filename).name}:"
                f"{lineno} suppressed\n"
            )

    warnings.showwarning = showwarning


def main() -> int:
    install_warning_dedupe()
    ap = argparse.ArgumentParser(
        description="Event-driven replay backtester for the NIFTY intraday "
                    "options engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", default=None, help="database (default: config.db_path)")
    ap.add_argument("--from", dest="d_from", default=None, help="first date YYYY-MM-DD")
    ap.add_argument("--to", dest="d_to", default=None, help="last date YYYY-MM-DD")
    ap.add_argument("--capital", type=float, default=None, help="override capital")
    ap.add_argument("--fill-edge", type=float, default=0.25,
                    help="0=pay the full spread, 0.5=get mid (default 0.25)")
    ap.add_argument("--stress-exit", type=float, default=0.5,
                    help="fraction of the edge retained on urgent exits "
                         "(default 0.5)")
    ap.add_argument("--csv", default=None, help="write the trade blotter here")
    ap.add_argument("--audit", action="store_true", help="report data coverage only")
    ap.add_argument("--test", action="store_true", help="run the harness self-test")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.test:
        return self_test()

    config = load_config()
    if args.capital:
        import dataclasses
        config = dataclasses.replace(config, starting_capital=args.capital)

    db_path = args.db or str(config.db_path)
    try:
        store = HistoricalStore(db_path)
    except FileNotFoundError:
        print(f"\n  No database at {db_path}")
        print("  Run the engine in paper mode first, or pass --db.\n")
        return 1

    if args.audit:
        return print_audit(store)

    dates = store.tradable_dates(args.d_from, args.d_to)
    if not dates:
        print_audit(store)
        return 1

    print()
    print(hr("═"))
    print(f"REPLAYING {len(dates)} SESSION(S): {dates[0]} .. {dates[-1]}")
    print(hr("═"))

    runner = BacktestRunner(
        store, config, FillModel(args.fill_edge, args.stress_exit), args.verbose
    )
    res = runner.run(dates)
    print_report(res, config, args)
    if args.csv:
        write_csv(res, args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())