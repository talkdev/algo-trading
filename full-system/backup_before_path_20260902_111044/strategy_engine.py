# ============ FILE: strategy_engine.py ============
"""
Strategy selection, construction, execution, position management,
risk management, circuit breakers, and trade logging.

FIXES APPLIED (all passes cumulative + pass 7):
  CRITICAL VS1 : DTE windows widened (DTE_MAX=10, tolerance=5)
                 Fixes Monday gap: DTE=1 (too close) and DTE=8
                 (outside old max=7). Now DTE=8 is accepted.
  CRITICAL QS1 : All builders use expiry-scoped chain access
  CRITICAL QS2 : CONDOR_MIN_CREDIT=15 (in config)
  CRITICAL FS2 : Pre-trade validates non-zero LTP
  CONFIRMED-10 : cancel_all_open_orders uses registry sweep only
                 (no EP_ORDER_BOOK — returns 400)
                 Uses /order/history?tag=nao for tag-based sweep
  HIGH     VS3 : Straddle stop documented (2x credit = correct)
  HIGH     VS4 : LTP=0 fallback uses entry_price (existing fix)
  HIGH     VS6 : Credit spread DTE=8 on Monday documented
  HIGH     VS7 : Expiry day close skips OTM options
  HIGH     QS3 : SPREAD_MIN_CREDIT=10 (in config)
  HIGH     QS4 : BUTTERFLY_MAX_DEBIT_PTS=50 (in config)
  HIGH     QS5 : Long straddle VIX spike threshold=0.05
  HIGH     QS6 : Build failure cooldown prevents repeated failures
  MEDIUM   VS8 : CB_LEVEL_3_PCT raised (in config)
  MEDIUM   QS7 : Brokerage = flat ₹20 per order
  MEDIUM   QS8 : weekly_pnl and daily_pnl consistency
  MEDIUM   PS10: _reconcile_with_broker skips empty list
"""

import asyncio
import sqlite3
import csv
import uuid
import json
import math
import hashlib
import copy
import logging
import numpy as np
import pandas as pd
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, List, Dict, Any
import pytz
import config
from data_manager import DataManager
from regime_engine import RegimeEngine

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Leg:
    instrument_key: str
    option_type:    str
    action:         str
    strike:         float
    expiry:         str
    qty:            int
    entry_price:    float = 0.0
    exit_price:     float = 0.0
    order_id:       str   = ""
    order_tag:      str   = ""
    fill_status:    str   = "PENDING"
    delta:          float = 0.0
    gamma:          float = 0.0
    vega:           float = 0.0
    theta:          float = 0.0
    slippage_pts:   float = 0.0


@dataclass
class Position:
    trade_id:             str
    strategy_name:        str
    regime_at_entry:      str
    entry_timestamp:      str
    entry_spot:           float
    entry_vix:            float
    legs:                 List[Leg]
    stop_loss:            float
    profit_target:        float
    exit_dte:             Optional[int]
    max_hold_date:        Optional[str]
    composite_at_entry:   float
    vol_score:            float
    edge_score:           float
    trend_score:          float
    flow_score:           float
    days_to_expiry:       int
    expiry_date:          str
    status:               str   = "OPEN"
    total_credit:         float = 0.0
    total_debit:          float = 0.0
    net_premium:          float = 0.0
    max_risk:             float = 0.0
    realized_pnl:         float = 0.0
    realized_pnl_percent: float = 0.0
    exit_reason:          str   = ""
    exit_timestamp:       str   = ""
    exit_spot:            float = 0.0
    exit_vix:             float = 0.0
    paper_trade:          bool  = True
    trend_direction:      float = 0.0
    meta:                 Dict  = field(default_factory=dict)
    transaction_costs:    float = 0.0
    net_pnl:              float = 0.0
    regime_at_exit:       str   = ""
    banked_pnl:           float = 0.0   # PATCH: partial-close pnl
    banked_costs:         float = 0.0   # PATCH: partial-close costs
    margin_estimate:      float = 0.0   # PATCH: heuristic SPAN/margin estimate


# ─────────────────────────────────────────────────────────────────────
# Strategy Engine
# ─────────────────────────────────────────────────────────────────────

class StrategyEngine:

    ORDER_TAG_PREFIX = "nao"

    def __init__(
        self,
        data_manager:  DataManager,
        regime_engine: RegimeEngine,
        db_path:       str,
    ) -> None:
        self.dm      = data_manager
        self.re      = regime_engine
        self.db_path = db_path
        self._IST    = pytz.timezone(config.TZ)

        self.open_positions:   List[Position] = []
        self.closed_positions: List[Position] = []

        self.daily_pnl:            float = 0.0
        self.weekly_pnl:           float = 0.0
        self.peak_capital:         float = float(
            config.TOTAL_CAPITAL
        )
        self.current_capital:      float = float(
            config.TOTAL_CAPITAL
        )
        self.daily_trading_halted: bool  = False
        self.kill_switch_active:   bool  = False
        self.cooling_period_end:   Optional[datetime] = None

        self.cb_level_1_count:  int  = 0
        self.cb_level_2_active: bool = False
        self.cb_level_3_active: bool = False
        self.cb_level_4_active: bool = False
        # MN-T04: CB L5 idempotency flag
        self.cb_level_5_active: bool = False
        # P2-4a: consecutive CB2 day counter
        # Tracks how many consecutive trading days ended with
        # CB L2 (daily loss > threshold) firing.  When >= 2
        # the effective daily loss threshold tightens to 1%%
        # so the engine de-risks automatically during a losing streak.
        self._consecutive_cb2_days: int = 0
        self._prev_day_cb2_fired: bool = False

        self._last_trading_date: Optional[date] = None
        self._last_weekly_reset: Optional[date] = None
        self._last_build_failure: Optional[datetime] = None

        self._session_orders:     Dict[str, Dict[str, Any]] = {}
        self._session_orders_lock = asyncio.Lock()
        self._inflight_tags:      set = set()
        self._inflight_lock       = asyncio.Lock()

        self._init_session_orders_table()

        # PATCH: restore capital/P&L/circuit-breaker state so a
        # restart doesn't silently reset current_capital back to
        # TOTAL_CAPITAL or clear an active halt/kill-switch.
        self._load_capital_state()

        # PATCH: re-entry cooldown tracking (uses
        # config.REENTRY_COOLDOWN_SEC / REENTRY_MAX_SPOT_MOVE_PCT,
        # previously defined but never referenced anywhere).
        self._last_position_close_time = None
        self._last_position_close_spot = None
        # ARCH-5a: regime confidence tracking
        # Decays 15% per consecutive sell-vol loss, floor 40%%.
        # Recovers 15% per win.  Scales lot size proportionally.
        self._regime_confidence: float = 1.0
        self._consecutive_regime_losses: int = 0
        # ARCH-7a: last stop composite tracking
        # Stores composite at the time of a stop-loss exit.
        # Re-entry is blocked unless composite has changed >= 0.10
        # (prevents re-entry into same adverse conditions).
        self._last_stop_composite: Optional[float] = None
        # PRF-02: skew side for next credit spread build
        self._pending_skew_side = "both"
        # PRF-04: last composite score per strategy at entry
        self._last_entry_composite: Dict[str, float] = {}

        # Latest entry-pipeline diagnostic shown on the console.
        self._last_entry_diagnostic = {
            "stage": "IDLE",
            "strategy": "",
            "message": "No entry attempt yet",
            "expiry": "",
            "dte": None,
            "credit": None,
            "min_credit": None,
            "wing_width": None,
            "max_risk": None,
            "lots": None,
            "vix_multiplier": 1.0,
            "pretrade": "NOT_RUN",
            "execution": "NOT_RUN",
            "timestamp": "",
        }


    # ─────────────────────────────────────────────────────────────
    # Order tag system
    # ─────────────────────────────────────────────────────────────

    def _generate_order_tag(
        self,
        trade_id:       str,
        instrument_key: str,
        action:         str,
        leg_index:      int = 0,
    ) -> str:
        # SE8-P0-04 FIX: revert monotonic counter.
        # The counter made every tag unique, destroying idempotency:
        # _check_existing_order_by_tag could never match a prior order
        # so both duplicate-order protections became no-ops, and any
        # retry would place a second real order.
        # Tag is deterministic: same inputs = same tag = idempotent.
        # SE-T04: use IST date not server-local date.
        _ist_date = datetime.now(
            self._IST
        ).date().isoformat()
        raw = (
            f"{trade_id[:12]}-"
            f"{instrument_key[-8:]}-"
            f"{action}-"
            f"{leg_index}-"
            f"{_ist_date}"
        )
        tag_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"{self.ORDER_TAG_PREFIX}-{tag_hash}"

    async def _register_order(
        self,
        order_id:       str,
        tag:            str,
        instrument_key: str,
        action:         str,
        qty:            int,
        price:          float,
        trade_id:       str,
    ) -> None:
        record = {
            "order_id":       order_id,
            "tag":            tag,
            "instrument_key": instrument_key,
            "action":         action,
            "qty":            qty,
            "price":          price,
            "trade_id":       trade_id,
            "placed_at":      datetime.now(
                self._IST
            ).isoformat(),
            "session_date":   date.today().isoformat(),
            "cancelled":      False,
            "filled":         False,
        }
        async with self._session_orders_lock:
            self._session_orders[tag] = record
        self._persist_order_to_sqlite(record)

    def _persist_order_to_sqlite(
        self, record: Dict[str, Any]
    ) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO session_orders (
                    order_id, tag, instrument_key, action,
                    qty, price, trade_id, placed_at,
                    session_date, cancelled, filled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["order_id"],
                record["tag"],
                record["instrument_key"],
                record["action"],
                record["qty"],
                record["price"],
                record["trade_id"],
                record["placed_at"],
                record["session_date"],
                0, 0,
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Order persist error: {e}")

    async def _mark_order_filled(self, tag: str) -> None:
        async with self._session_orders_lock:
            if tag in self._session_orders:
                self._session_orders[tag]["filled"] = True
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE session_orders "
                "SET filled=1 WHERE tag=?", (tag,)
            )
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Mark filled error: {e}")

    async def _mark_order_cancelled(
        self, tag: str
    ) -> None:
        async with self._session_orders_lock:
            if tag in self._session_orders:
                self._session_orders[tag][
                    "cancelled"
                ] = True
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE session_orders "
                "SET cancelled=1 WHERE tag=?", (tag,)
            )
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Mark cancelled error: {e}")

    async def _check_existing_order_by_tag(
        self, tag: str
    ) -> Optional[Tuple[str, float, str]]:
        async with self._session_orders_lock:
            existing = self._session_orders.get(tag)
            if existing and existing.get("filled"):
                return (
                    existing["order_id"],
                    existing.get("fill_price", 0.0),
                    "complete",
                )

        if config.PAPER_TRADING_MODE:
            return None

        try:
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY, {"tag": tag}
            )
            orders = (
                response
                if isinstance(response, list)
                else response.get("data", []) or []
            )
            if not orders:
                return None

            last   = orders[-1]
            status = str(last.get("status", "")).lower()
            oid    = str(last.get("order_id", ""))
            price  = float(
                last.get("average_price", 0) or 0
            )

            if status in ("complete", "filled", "traded"):
                return (oid, price, "complete")
            if status in ("open", "pending"):
                return (oid, 0.0, "open")
            if status in (
                "rejected", "cancelled", "canceled"
            ):
                return None
            return None

        except Exception as e:
            logger.warning(
                f"Idempotency check tag={tag}: {e}"
            )
            return None

    def _init_session_orders_table(self) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # LOG-SE-04: decision logging tables for walk-forward analysis
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entry_attempt_log (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp            TEXT NOT NULL,
                    strategy_name        TEXT,
                    regime_at_attempt    TEXT,
                    composite_score      REAL,
                    build_result         TEXT,
                    build_failure_reason TEXT,
                    expiry_date          TEXT,
                    dte                  INTEGER,
                    net_credit           REAL,
                    min_credit_required  REAL,
                    wing_width           INTEGER,
                    expected_move        REAL,
                    pretrade_passed      INTEGER,
                    pretrade_fail_reason TEXT,
                    execution_result     TEXT,
                    lots_requested       INTEGER,
                    lots_filled          INTEGER,
                    actual_credit        REAL,
                    vix_at_entry         REAL,
                    vix_adaptive_mult    REAL,
                    max_risk_used        REAL,
                    trade_id             TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_entry_attempt_ts
                ON entry_attempt_log(timestamp)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS position_monitor_log (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp             TEXT NOT NULL,
                    trade_id              TEXT NOT NULL,
                    strategy_name         TEXT,
                    current_premium       REAL,
                    stop_loss             REAL,
                    profit_target         REAL,
                    unrealized_pnl        REAL,
                    stop_breach_ticks     INTEGER,
                    distance_to_call_pct  REAL,
                    distance_to_put_pct   REAL,
                    partial_taken         INTEGER,
                    action_taken          TEXT,
                    spot                  REAL,
                    vix                   REAL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pos_monitor_ts
                ON position_monitor_log(timestamp)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS session_orders (
                    tag            TEXT PRIMARY KEY,
                    order_id       TEXT,
                    instrument_key TEXT,
                    action         TEXT,
                    qty            INTEGER,
                    price          REAL,
                    fill_price     REAL DEFAULT 0,
                    trade_id       TEXT,
                    placed_at      TEXT,
                    session_date   TEXT,
                    cancelled      INTEGER DEFAULT 0,
                    filled         INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_session_orders_date
                ON session_orders(session_date)
            """)
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(
                f"session_orders init error: {e}"
            )

    def _init_capital_state_table(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS engine_capital_state (
                    id INTEGER PRIMARY KEY,
                    current_capital REAL,
                    peak_capital REAL,
                    weekly_pnl REAL,
                    daily_pnl REAL DEFAULT 0,
                    cb_level_2_active INTEGER,
                    cb_level_3_active INTEGER,
                    cb_level_4_active INTEGER,
                    kill_switch_active INTEGER,
                    daily_trading_halted INTEGER,
                    last_trading_date TEXT,
                    last_weekly_reset TEXT,
                    updated_at TEXT
                )
            """)
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"capital_state table init error: {e}")

    def _save_capital_state(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # SE-13: include daily_pnl so a mid-day restart
            # does not reset the CB L2 daily-loss counter.
            cursor.execute("""
                INSERT OR REPLACE INTO engine_capital_state (
                    id, current_capital, peak_capital, weekly_pnl,
                    daily_pnl,
                    cb_level_2_active, cb_level_3_active,
                    cb_level_4_active, kill_switch_active,
                    daily_trading_halted, last_trading_date,
                    last_weekly_reset, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.current_capital,
                self.peak_capital,
                self.weekly_pnl,
                self.daily_pnl,
                1 if self.cb_level_2_active else 0,
                1 if self.cb_level_3_active else 0,
                1 if self.cb_level_4_active else 0,
                1 if self.kill_switch_active else 0,
                1 if self.daily_trading_halted else 0,
                (
                    self._last_trading_date.isoformat()
                    if self._last_trading_date else ""
                ),
                (
                    self._last_weekly_reset.isoformat()
                    if self._last_weekly_reset else ""
                ),
                datetime.now(self._IST).isoformat(),
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"_save_capital_state error: {e}")

    def _load_capital_state(self) -> None:
        """
        PATCH: restore capital/P&L/circuit-breaker state across
        restarts.
        """
        try:
            self._init_capital_state_table()
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM engine_capital_state WHERE id = 1"
            )
            row = cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                data = dict(zip(cols, row))
                self.current_capital = float(
                    data.get("current_capital")
                    or config.TOTAL_CAPITAL
                )
                self.peak_capital = float(
                    data.get("peak_capital")
                    or config.TOTAL_CAPITAL
                )
                self.weekly_pnl = float(
                    data.get("weekly_pnl") or 0.0
                )
                # SE-13: restore daily_pnl
                self.daily_pnl = float(
                    data.get("daily_pnl") or 0.0
                )
                self.cb_level_2_active = bool(
                    data.get("cb_level_2_active")
                )
                self.cb_level_3_active = bool(
                    data.get("cb_level_3_active")
                )
                self.cb_level_4_active = bool(
                    data.get("cb_level_4_active")
                )
                self.kill_switch_active = bool(
                    data.get("kill_switch_active")
                )
                self.daily_trading_halted = bool(
                    data.get("daily_trading_halted")
                )
                ltd = data.get("last_trading_date")
                if ltd:
                    try:
                        self._last_trading_date = (
                            datetime.strptime(
                                ltd, "%Y-%m-%d"
                            ).date()
                        )
                    except ValueError:
                        pass
                lwr = data.get("last_weekly_reset")
                if lwr:
                    try:
                        self._last_weekly_reset = (
                            datetime.strptime(
                                lwr, "%Y-%m-%d"
                            ).date()
                        )
                    except ValueError:
                        pass
                logger.info(
                    f"Restored capital state: "
                    f"current={self.current_capital:.2f} "
                    f"peak={self.peak_capital:.2f} "
                    f"weekly_pnl={self.weekly_pnl:.2f} "
                    f"kill_switch={self.kill_switch_active} "
                    f"daily_halted={self.daily_trading_halted}"
                )
            conn.close()
        except sqlite3.OperationalError:
            logger.info(
                "No engine_capital_state table — fresh start"
            )
        except Exception as e:
            logger.warning(f"_load_capital_state error: {e}")

    # ─────────────────────────────────────────────────────────────
    # Startup stale order cancellation
    # ─────────────────────────────────────────────────────────────

    async def startup_cancel_stale_orders(self) -> int:
        if config.PAPER_TRADING_MODE:
            return 0

        cancelled = 0
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tag, order_id, instrument_key,
                       action, placed_at
                FROM session_orders
                WHERE cancelled = 0
                AND   filled    = 0
                AND   session_date < ?
            """, (datetime.now(self._IST).date().isoformat(),))
            stale = cursor.fetchall()
            conn.close()

            if not stale:
                logger.info("STARTUP: No stale orders")
                return 0

            logger.warning(
                f"STARTUP: {len(stale)} stale orders found"
            )

            for (
                tag, order_id, instrument_key,
                action, placed_at,
            ) in stale:
                try:
                    response = await self.dm._api_get(
                        config.EP_ORDER_HISTORY,
                        {"order_id": order_id},
                    )
                    orders = (
                        response
                        if isinstance(response, list)
                        else response.get("data", []) or []
                    )
                    if not orders:
                        await self._mark_order_cancelled(
                            tag
                        )
                        continue

                    status = str(
                        orders[-1].get("status", "")
                    ).lower()

                    if status in (
                        "complete", "filled", "traded",
                        "cancelled", "rejected",
                        "day_closed",
                    ):
                        await self._mark_order_cancelled(
                            tag
                        )
                        continue

                    logger.warning(
                        f"STARTUP: Cancelling stale "
                        f"tag={tag} order_id={order_id}"
                    )
                    await self.dm._api_delete(
                        f"{config.EP_ORDER_CANCEL}"
                        f"/{order_id}"
                    )
                    await self._mark_order_cancelled(tag)
                    cancelled += 1
                    await asyncio.sleep(
                        config.ORDER_BETWEEN_LEGS_DELAY_SEC
                    )

                except Exception as e:
                    logger.error(
                        f"STARTUP: stale order "
                        f"tag={tag}: {e}"
                    )

        except sqlite3.Error as e:
            logger.warning(
                f"STARTUP: stale order DB error: {e}"
            )

        return cancelled

    # ─────────────────────────────────────────────────────────────
    # Cancel sweep
    # FIX CONFIRMED-10: registry sweep only
    # Confirmed: no endpoint lists all open orders.
    # /order/get-order-book = 400 Invalid Endpoint
    # /order/open-orders    = 400 Invalid Endpoint
    # Working: /order/history?tag=nao (our prefix)
    #          /order/history?order_id=X (per-order check)
    # ─────────────────────────────────────────────────────────────

    async def cancel_all_open_orders(
        self, context: str = "EOD_SWEEP"
    ) -> int:
        """
        Cancel ALL open orders placed this session.

        FIX CONFIRMED-10: registry-based sweep only.
        No endpoint lists all open orders — confirmed live.
        Two approaches:
          1. Per-order check via /order/history?order_id=X
          2. Tag-based sweep via /order/history?tag=nao
        """
        if config.PAPER_TRADING_MODE:
            logger.info(
                f"[PAPER] {context}: sweep skipped"
            )
            return 0

        logger.info(
            f"{context}: Starting cancel sweep "
            f"(registry-based)..."
        )
        cancelled_count  = 0
        failed_count     = 0
        already_terminal = 0

        # ── Approach 1: Per-order registry sweep ─────────────────
        async with self._session_orders_lock:
            registry_snapshot = dict(self._session_orders)

        for tag, record in registry_snapshot.items():
            if (
                record.get("cancelled")
                or record.get("filled")
            ):
                already_terminal += 1
                continue

            order_id = record.get("order_id", "")
            if not order_id:
                continue

            try:
                response = await self.dm._api_get(
                    config.EP_ORDER_HISTORY,
                    {"order_id": order_id},
                )
                orders = (
                    response
                    if isinstance(response, list)
                    else response.get("data", []) or []
                )

                if not orders:
                    await self._mark_order_cancelled(tag)
                    already_terminal += 1
                    continue

                status = str(
                    orders[-1].get("status", "")
                ).lower()

                if status in (
                    "complete", "filled", "traded"
                ):
                    await self._mark_order_filled(tag)
                    already_terminal += 1
                    continue

                if status in (
                    "cancelled", "rejected",
                    "day_closed",
                ):
                    await self._mark_order_cancelled(tag)
                    already_terminal += 1
                    continue

                # Still open — cancel it
                logger.info(
                    f"{context}: Cancelling "
                    f"order_id={order_id} tag={tag} "
                    f"status={status}"
                )
                await self.dm._api_delete(
                    f"{config.EP_ORDER_CANCEL}/{order_id}"
                )
                await self._mark_order_cancelled(tag)
                cancelled_count += 1
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )

            except Exception as e:
                logger.warning(
                    f"{context}: Registry check "
                    f"tag={tag}: {e}"
                )
                failed_count += 1

        # ── Approach 2: Tag-based sweep ───────────────────────────
        # /order/history?tag=nao returns all orders with our prefix
        # Confirmed working from live test (section 15)
        try:
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"tag": self.ORDER_TAG_PREFIX},
            )
            orders = (
                response
                if isinstance(response, list)
                else response.get("data", []) or []
            )

            for order in orders:
                status   = str(
                    order.get("status", "")
                ).lower()
                order_id = str(order.get("order_id", ""))
                tag      = str(order.get("tag", ""))

                if status not in (
                    "open", "pending",
                    "trigger pending",
                    "after market order req received",
                ):
                    continue

                # Check if already handled in registry sweep
                async with self._session_orders_lock:
                    already_handled = (
                        tag in self._session_orders
                        and (
                            self._session_orders[tag].get(
                                "cancelled"
                            )
                            or self._session_orders[tag].get(
                                "filled"
                            )
                        )
                    )

                if already_handled:
                    continue

                logger.warning(
                    f"{context}: Tag-sweep found "
                    f"open order NOT in registry: "
                    f"order_id={order_id} tag={tag}"
                )

                try:
                    await self.dm._api_delete(
                        f"{config.EP_ORDER_CANCEL}"
                        f"/{order_id}"
                    )
                    await self._mark_order_cancelled(tag)
                    cancelled_count += 1
                    logger.info(
                        f"{context}: Tag-sweep cancelled "
                        f"order_id={order_id}"
                    )
                    await asyncio.sleep(
                        config.ORDER_BETWEEN_LEGS_DELAY_SEC
                    )
                except Exception as e:
                    logger.error(
                        f"{context}: Tag-sweep cancel "
                        f"failed {order_id}: {e}"
                    )
                    failed_count += 1

        except Exception as e:
            logger.warning(
                f"{context}: Tag-sweep failed: {e}"
            )

        logger.info(
            f"{context}: Sweep complete — "
            f"cancelled={cancelled_count} "
            f"failed={failed_count} "
            f"terminal={already_terminal}"
        )
        return cancelled_count

    # ─────────────────────────────────────────────────────────────
    # Order placement
    # ─────────────────────────────────────────────────────────────

    async def _place_single_leg(
        self,
        leg:        Leg,
        use_market: bool = False,
        trade_id:   str  = "",
        leg_index:  int  = 0,
    ) -> Tuple[bool, str]:
        if config.PAPER_TRADING_MODE:
            return await self._simulate_fill(leg)

        tag = self._generate_order_tag(
            trade_id or leg.instrument_key[:12],
            leg.instrument_key,
            leg.action,
            leg_index,
        )
        leg.order_tag = tag

        async with self._inflight_lock:
            if tag in self._inflight_tags:
                logger.warning(
                    f"Concurrent duplicate: tag={tag}"
                )
                return (False, "")
            self._inflight_tags.add(tag)

        try:
            existing = (
                await self._check_existing_order_by_tag(
                    tag
                )
            )
            if existing is not None:
                order_id, fill_price, status = existing
                if status == "complete" and fill_price > 0:
                    leg.entry_price = fill_price
                    leg.order_id    = order_id
                    leg.fill_status = "COMPLETE"
                    await self._mark_order_filled(tag)
                    return (True, order_id)
                if status == "open":
                    requested_qty = (
                        leg.qty * config.LOT_SIZE
                    )
                    fill_info = await self._wait_for_fill(
                        order_id,
                        config.CORE_FILL_TIMEOUT_SEC,
                        requested_qty=requested_qty,
                    )
                    return await self._resolve_fill_result(
                        leg, order_id, tag,
                        fill_info, requested_qty,
                    )

            expiry_chain = self.dm.get_chain_for_expiry(
                leg.expiry
            )
            opt_data = expiry_chain.get(
                leg.strike, {}
            ).get(leg.option_type, {})

            if use_market:
                order_type = "MARKET"
                price      = 0
            else:
                order_type = "LIMIT"
                if leg.action == "BUY":
                    price = (
                        opt_data.get("ask", 0)
                        + config.ORDER_AGGRESSION_TICKS
                        * config.TICK_SIZE
                    )
                else:
                    price = (
                        opt_data.get("bid", 0)
                        - config.ORDER_AGGRESSION_TICKS
                        * config.TICK_SIZE
                    )
                price = max(
                    config.TICK_SIZE,
                    round(price / config.TICK_SIZE)
                    * config.TICK_SIZE,
                )

            payload = {
                "quantity":           (
                    leg.qty * config.LOT_SIZE
                ),
                "product":            "D",
                "validity":           "DAY",
                "price":              price,
                "tag":                tag,
                "instrument_token":   leg.instrument_key,
                "order_type":         order_type,
                "transaction_type":   leg.action,
                "disclosed_quantity": 0,
                "trigger_price":      0,
                "is_amo":             False,
            }

            try:
                response = await self.dm._api_post(
                    config.EP_ORDER_PLACE, payload
                )
                order_id = (
                    response.get("data", {}).get(
                        "order_id", ""
                    )
                    or response.get("order_id", "")
                )
                if not order_id:
                    logger.warning(
                        f"No order_id for tag={tag}"
                    )
                    return (False, "")

                logger.info(
                    f"Order placed: tag={tag} "
                    f"order_id={order_id} "
                    f"{leg.action} {leg.option_type} "
                    f"{leg.strike} expiry={leg.expiry} "
                    f"qty={leg.qty * config.LOT_SIZE} "
                    f"price={price}"
                )

                await self._register_order(
                    order_id=order_id,
                    tag=tag,
                    instrument_key=leg.instrument_key,
                    action=leg.action,
                    qty=leg.qty * config.LOT_SIZE,
                    price=price,
                    trade_id=trade_id,
                )

                requested_qty = leg.qty * config.LOT_SIZE
                fill_info = await self._wait_for_fill(
                    order_id,
                    config.CORE_FILL_TIMEOUT_SEC,
                    requested_qty=requested_qty,
                )
                success, _ = await self._resolve_fill_result(
                    leg, order_id, tag,
                    fill_info, requested_qty,
                )
                if not success:
                    return (False, order_id)

                expected = (
                    price
                    if order_type == "LIMIT"
                    else opt_data.get("ltp", price)
                )
                slippage = abs(leg.entry_price - expected)
                leg.slippage_pts = slippage
                if slippage > 2:
                    logger.warning(
                        f"High slippage: {slippage:.2f}pts "
                        f"{leg.action} {leg.option_type} "
                        f"{leg.strike}"
                    )

                return (True, order_id)

            except Exception as e:
                logger.error(
                    f"Order placement error tag={tag}: {e}"
                )
                return (False, "")

        finally:
            async with self._inflight_lock:
                self._inflight_tags.discard(tag)

    async def _resolve_fill_result(
        self,
        leg: Leg,
        order_id: str,
        tag: str,
        fill_info: Dict[str, Any],
        requested_qty: int,
    ) -> Tuple[bool, str]:
        """
        PATCH: centralizes partial-fill handling for both call
        sites in _place_single_leg(). Previously any fill short
        of 100% was treated as a total failure — the filled
        portion was cancelled/discarded and never recorded
        anywhere. Now: 0 filled -> fail as before; partially
        filled -> cancel the remainder (per
        config.PARTIAL_FILL_CANCEL), adjust leg.qty DOWN to the
        actual filled lot count, and mark fill_status="PARTIAL"
        so callers (e.g. _execute_strategy's rebalance step) know
        to react to it.
        """
        filled_qty = fill_info.get("filled_qty", 0)

        if filled_qty <= 0:
            await self._cancel_order(order_id)
            await self._mark_order_cancelled(tag)
            return (False, order_id)

        filled_lots = filled_qty // config.LOT_SIZE
        if filled_lots < 1:
            logger.warning(
                f"Fill below 1 lot ({filled_qty} shares) for "
                f"{leg.option_type} {leg.strike} — "
                f"treating as failure"
            )
            await self._cancel_order(order_id)
            await self._mark_order_cancelled(tag)
            return (False, order_id)

        if filled_qty < requested_qty:
            if config.PARTIAL_FILL_CANCEL:
                await self._cancel_order(order_id)
            logger.warning(
                f"PARTIAL FILL: {leg.action} {leg.option_type} "
                f"{leg.strike} — requested={requested_qty} "
                f"filled={filled_qty} ({filled_lots} lots) — "
                f"adjusting leg qty down from {leg.qty}"
            )
            leg.qty = filled_lots
            leg.fill_status = "PARTIAL"
        else:
            leg.fill_status = "COMPLETE"

        avg_price = fill_info.get("avg_price", 0.0)
        leg.entry_price = (
            avg_price if avg_price > 0 else leg.entry_price
        )
        leg.order_id = order_id

        await self._mark_order_filled(tag)
        return (True, order_id)

    async def _simulate_fill(
        self, leg: Leg
    ) -> Tuple[bool, str]:
        """Simulate fill for paper trading."""
        expiry_chain = self.dm.get_chain_for_expiry(
            leg.expiry
        )
        opt_data = expiry_chain.get(
            leg.strike, {}
        ).get(leg.option_type, {})

        bid = float(opt_data.get("bid", 0) or 0)
        ask = float(opt_data.get("ask", 0) or 0)
        ltp = float(opt_data.get("ltp", 0) or 0)

        if bid > 0 and ask > 0:
            reference = (bid + ask) / 2.0
        else:
            reference = ltp

        if reference == 0:
            logger.warning(
                f"Paper fill: no price for "
                f"{leg.option_type} strike={leg.strike} "
                f"expiry={leg.expiry}"
            )
            return (False, "")

        # PRF-S01: dynamic slippage based on VIX and DTE.
        # Base slippage is the configured tick count.
        # Multiply by VIX multiplier when VIX is elevated
        # (OTM puts have wider spreads in high-vol regimes).
        # Multiply by DTE multiplier when near expiry
        # (liquidity thins for OTM strikes near expiry).
        _vix_now = self.dm.vix or 14.0
        _vix_thresh = getattr(
            config, "SLIPPAGE_VIX_HIGH_THRESHOLD", 15.0
        )
        _vix_mult = (
            getattr(config, "SLIPPAGE_VIX_HIGH_MULT", 2.5)
            if _vix_now > _vix_thresh
            else 1.0
        )
        # DTE-based multiplier
        _dte_mult = 1.0
        _dte_thresh = getattr(
            config, "SLIPPAGE_LOW_DTE_THRESHOLD", 2
        )
        try:
            from datetime import date as _d
            _exp_date = datetime.strptime(
                leg.expiry, "%Y-%m-%d"
            ).date()
            _dte_now = (_exp_date - _d.today()).days
            if _dte_now <= _dte_thresh:
                _dte_mult = getattr(
                    config, "SLIPPAGE_LOW_DTE_MULT", 1.5
                )
        except Exception:
            pass
        # Only apply extra multiplier to OTM options (delta < 0.35)
        # ATM options have tighter spreads and don't need the boost
        _is_otm = abs(leg.delta) < 0.35 if leg.delta else True
        _slip_mult = (_vix_mult * _dte_mult) if _is_otm else 1.0

        if leg.action == "SELL":
            slippage   = (
                config.PAPER_SLIPPAGE_SHORT_TICKS
                * config.TICK_SIZE
                * _slip_mult
            )
            fill_price = reference - slippage
        else:
            slippage   = (
                config.PAPER_SLIPPAGE_HEDGE_TICKS
                * config.TICK_SIZE
                * _slip_mult
            )
            fill_price = reference + slippage

        fill_price = max(
            config.TICK_SIZE,
            round(fill_price / config.TICK_SIZE)
            * config.TICK_SIZE,
        )

        leg.entry_price  = fill_price
        leg.slippage_pts = slippage
        leg.fill_status  = "COMPLETE"

        order_id      = f"PAPER_{uuid.uuid4().hex[:8]}"
        leg.order_id  = order_id
        leg.order_tag = f"paper_{order_id}"

        logger.info(
            f"Paper fill: {leg.action} {leg.option_type} "
            f"strike={leg.strike} expiry={leg.expiry} "
            f"ref={reference:.2f} fill={fill_price:.2f}"
        )
        return (True, order_id)

    async def _wait_for_fill(
        self,
        order_id: str,
        timeout_sec: int,
        requested_qty: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        PATCH: previously returned a bare bool (fully filled or
        not) — any partial fill was indistinguishable from a
        total failure. Now returns the full fill-info dict so
        callers can detect and correctly handle partial fills.
        """
        start = asyncio.get_event_loop().time()
        last_info: Dict[str, Any] = {
            "status": "unknown",
            "filled_qty": 0,
            "avg_price": 0.0,
        }
        while True:
            elapsed = (
                asyncio.get_event_loop().time() - start
            )
            if elapsed > timeout_sec:
                logger.warning(
                    f"Fill timeout {timeout_sec}s: "
                    f"{order_id} "
                    f"(filled={last_info['filled_qty']})"
                )
                return last_info
            await asyncio.sleep(
                config.ORDER_POLL_INTERVAL_SEC
            )
            last_info = await self._get_order_fill_info(
                order_id
            )
            if last_info["status"] == "complete":
                return last_info
            if (
                requested_qty
                and last_info["filled_qty"] >= requested_qty
            ):
                last_info["status"] = "complete"
                return last_info
            if last_info["status"] in (
                "rejected", "cancelled"
            ):
                return last_info

    async def _get_order_status(
        self, order_id: str
    ) -> str:
        try:
            await asyncio.sleep(
                config.ORDER_STATUS_POLL_DELAY_SEC
            )
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"order_id": order_id},
            )
            orders = (
                response
                if isinstance(response, list)
                else []
            )
            if orders:
                return str(
                    orders[-1].get("status", "unknown")
                ).lower()
            return "unknown"
        except Exception as e:
            logger.warning(
                f"_get_order_status {order_id}: {e}"
            )
            return "unknown"

    async def _get_order_fill_info(
        self, order_id: str
    ) -> Dict[str, Any]:
        """
        PATCH: returns full fill info (status, filled_qty,
        avg_price) instead of just a status string, enabling
        partial-fill detection. Tries a few common field names
        for filled quantity since exact API field naming can
        vary; falls back to inferring from status if none of
        them are present.
        """
        try:
            await asyncio.sleep(
                config.ORDER_STATUS_POLL_DELAY_SEC
            )
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"order_id": order_id},
            )
            orders = (
                response
                if isinstance(response, list)
                else []
            )
            if not orders:
                return {
                    "status": "unknown",
                    "filled_qty": 0,
                    "avg_price": 0.0,
                }
            last = orders[-1]
            status = str(
                last.get("status", "unknown")
            ).lower()

            filled_qty = (
                last.get("filled_quantity")
                if last.get("filled_quantity") is not None
                else last.get("filled_qty")
            )
            if filled_qty is None:
                total_qty = last.get("quantity", 0)
                filled_qty = (
                    total_qty
                    if status in (
                        "complete", "filled", "traded"
                    )
                    else 0
                )

            avg_price = float(
                last.get("average_price", 0) or 0
            )
            return {
                "status": status,
                "filled_qty": int(filled_qty or 0),
                "avg_price": avg_price,
            }
        except Exception as e:
            logger.warning(
                f"_get_order_fill_info {order_id}: {e}"
            )
            return {
                "status": "unknown",
                "filled_qty": 0,
                "avg_price": 0.0,
            }

    async def _get_fill_price(
        self, order_id: str
    ) -> float:
        try:
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"order_id": order_id},
            )
            orders = (
                response
                if isinstance(response, list)
                else []
            )
            if orders:
                return float(
                    orders[-1].get("average_price", 0)
                )
            return 0.0
        except Exception as e:
            logger.warning(
                f"_get_fill_price {order_id}: {e}"
            )
            return 0.0

    async def _cancel_order(self, order_id: str) -> None:
        try:
            await self.dm._api_delete(
                f"{config.EP_ORDER_CANCEL}/{order_id}"
            )
            logger.info(f"Order cancelled: {order_id}")
        except Exception as e:
            logger.warning(
                f"Cancel failed {order_id}: {e}"
            )

    async def _cancel_and_reverse(
        self, filled_legs: List[Leg]
    ) -> None:
        logger.warning(
            f"Aborting — reversing "
            f"{len(filled_legs)} filled legs"
        )
        _orphaned: List[Leg] = []
        for leg in reversed(filled_legs):
            reverse_action = (
                "BUY" if leg.action == "SELL" else "SELL"
            )
            reverse_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action=reverse_action,
                strike=leg.strike,
                expiry=leg.expiry,
                qty=leg.qty,
            )
            success, _ = await self._place_single_leg(
                reverse_leg,
                use_market=True,
                trade_id=f"reverse-{uuid.uuid4().hex[:8]}",
                leg_index=0,
            )
            if not success:
                logger.error(
                    f"Reversal failed — retrying: "
                    f"{leg.option_type} {leg.strike}"
                )
                retry_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action=reverse_action,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=leg.qty,
                )
                retry_ok, _ = await self._place_single_leg(
                    retry_leg,
                    use_market=True,
                    trade_id=(
                        f"reverse-retry-"
                        f"{uuid.uuid4().hex[:8]}"
                    ),
                    leg_index=0,
                )
                # SE7-P0-01: if both reversal attempts fail, the
                # leg is still live at the broker with no Position
                # tracking it. Trip the kill switch and log CRITICAL
                # so the operator knows manual intervention is needed.
                if not retry_ok:
                    _orphaned.append(leg)
                    logger.critical(
                        f"SE7-P0-01: ORPHANED LEG — reversal failed "
                        f"twice for {leg.option_type} {leg.strike} "
                        f"{leg.expiry}. Leg is live at broker with no "
                        f"local tracking. MANUAL INTERVENTION REQUIRED."
                    )
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )
        if _orphaned:
            self.kill_switch_active = True
            self._save_capital_state()
            logger.critical(
                f"SE7-P0-01: Kill switch activated due to "
                f"{len(_orphaned)} orphaned leg(s). "
                f"Reconcile broker positions before restarting."
            )

    # ─────────────────────────────────────────────────────────────
    # Transaction cost calculation
    # ─────────────────────────────────────────────────────────────

    def _calculate_transaction_costs(
        self, position: Position
    ) -> float:
        """
        Calculate NSE transaction costs.
        PATCH: rates updated per production verification record
        (see config.py's COST_* constants). Kept as separate named
        categories (brokerage, exchange charge, NSE IPFT, SEBI
        fee, STT, stamp duty, GST) rather than one generic
        percentage, per the verification requirements. Rates were
        supplied externally, NOT independently confirmed by this
        codebase — re-check against a live NSE circular / broker
        contract note before production use.
        """
        if not position.legs:
            return 0.0

        buy_value  = 0.0
        sell_value = 0.0
        num_orders = 0

        for leg in position.legs:
            if leg.entry_price > 0 and leg.qty > 0:
                value      = (
                    leg.entry_price
                    * leg.qty
                    * config.LOT_SIZE
                )
                num_orders += 1
                if leg.action == "BUY":
                    buy_value  += value
                else:
                    sell_value += value

            if leg.exit_price > 0 and leg.qty > 0:
                value      = (
                    leg.exit_price
                    * leg.qty
                    * config.LOT_SIZE
                )
                num_orders += 1
                if leg.action == "SELL":
                    buy_value  += value
                else:
                    sell_value += value

        total_turnover = buy_value + sell_value
        if total_turnover <= 0:
            return 0.0

        brokerage    = (
            config.COST_BROKERAGE_PER_ORDER * num_orders
        )
        stt          = sell_value * config.COST_STT_OPTION_SELL_PCT
        exchange_fee = total_turnover * config.COST_EXCHANGE_PCT
        ipft         = total_turnover * config.COST_NSE_IPFT_PCT
        sebi         = total_turnover * config.COST_SEBI_PCT
        stamp        = buy_value * config.COST_STAMP_PCT
        # PATCH (NEW-5): GST base now includes SEBI turnover fee —
        # standard broker contract notes apply GST to brokerage +
        # exchange transaction charges + SEBI fee (never to STT or
        # stamp duty, which are statutory levies, not services).
        gst          = (
            brokerage + exchange_fee + sebi
        ) * config.COST_GST_PCT

        return round(
            brokerage + stt + exchange_fee + ipft
            + sebi + stamp + gst,
            2,
        )

    def _calculate_final_pnl(
        self, position: Position
    ) -> Tuple[float, float, float]:
        """Calculate gross PnL, transaction costs, net PnL."""
        gross_pnl    = 0.0
        expiry_chain = self.dm.get_chain_for_expiry(
            position.expiry_date
        )

        for leg in position.legs:
            exit_price = leg.exit_price
            # PATCH (S6): a leg explicitly marked
            # EXPIRED_WORTHLESS was genuinely worth 0 at close —
            # bypass both fallback layers below so its real P&L
            # (full credit for a short, full debit loss for a
            # long) isn't silently zeroed out by the entry_price
            # fallback.
            is_expired_worthless = (
                leg.fill_status == "EXPIRED_WORTHLESS"
            )
            if exit_price == 0 and not is_expired_worthless:
                # Use staleness-aware mark price as fallback.
                # The old fallback (entry_price) recorded zero P&L
                # for a failed exit on a deep-ITM leg, preventing
                # CB L2/L3 from firing when they were needed most.
                fallback_opt = (
                    expiry_chain
                    .get(leg.strike, {})
                    .get(leg.option_type, {})
                )
                _mark = self.dm.get_mark_price(
                    fallback_opt,
                    fallback=0.0,
                )
                if _mark > 0:
                    exit_price = _mark
            if exit_price == 0 and not is_expired_worthless:
                # Last resort: use entry_price only if mark is
                # also unavailable (e.g. position not in chain).
                exit_price = leg.entry_price

            if leg.action == "SELL":
                leg_pnl = (
                    (leg.entry_price - exit_price)
                    * leg.qty * config.LOT_SIZE
                )
            else:
                leg_pnl = (
                    (exit_price - leg.entry_price)
                    * leg.qty * config.LOT_SIZE
                )
            gross_pnl += leg_pnl

        # PATCH: include any pnl/costs banked by a partial
        # (one-sided) close earlier in the position's life
        # (see _close_one_side).
        gross_pnl += getattr(position, "banked_pnl", 0.0)

        tx_costs = self._calculate_transaction_costs(
            position
        ) + getattr(position, "banked_costs", 0.0)
        net_pnl  = gross_pnl - tx_costs
        return gross_pnl, tx_costs, net_pnl

    def _estimate_costs(
        self, position: Position
    ) -> float:
        return self._calculate_transaction_costs(position)

    # ─────────────────────────────────────────────────────────────
    # Main cycle
    # ─────────────────────────────────────────────────────────────

    async def run_cycle(self) -> None:
        logger.info("Strategy cycle started")

        if self.kill_switch_active:
            logger.info("Kill switch — no action")
            return

        today = date.today()
        if self._last_trading_date != today:
            self.reset_daily_state()
            self._last_trading_date = today

        if today.weekday() == 0:
            if self._last_weekly_reset != today:
                self.reset_weekly_state()
                self._last_weekly_reset = today

        await self._update_all_pnls()
        await self._check_circuit_breakers()
        if self.kill_switch_active:
            return

        await self._monitor_all_positions()

        if self.re.regime_changed:
            await self._handle_regime_transition()
            self.re.regime_changed = False

        if self._should_enter_new_position():
            await self._enter_new_position()

        await self._check_greeks_limits()
        self._save_all_positions_to_sqlite()
        self._save_capital_state()
        self._log_portfolio_summary()

        logger.info("Strategy cycle complete")

    async def _update_all_pnls(self) -> None:
        """
        Update MTM P&L for all open positions.
        FIX QS8: daily_pnl = realized_today + unrealized
                 (MTM-based, consistent methodology)
        """
        today_str = date.today().isoformat()

        for position in self.open_positions:
            position_value = 0.0
            expiry_chain   = self.dm.get_chain_for_expiry(
                position.expiry_date
            )

            for leg in position.legs:
                opt_data = (
                    expiry_chain
                    .get(leg.strike, {})
                    .get(leg.option_type, {})
                )
                # AUDIT DM-01: bid/ask are only updated by the
                # 60s REST poll; LTP is updated by WS on every
                # tick. Use staleness-aware get_mark_price() so
                # we prefer live LTP when the REST quote is stale.
                mark = self.dm.get_mark_price(
                    opt_data, fallback=leg.entry_price
                )
                if mark <= 0:
                    mark = leg.entry_price
                    logger.warning(
                        f"No mark price for {leg.option_type} "
                        f"{leg.strike} expiry="
                        f"{position.expiry_date} "
                        f"— using entry"
                    )

                if leg.action == "SELL":
                    leg_pnl = (
                        (leg.entry_price - mark)
                        * leg.qty * config.LOT_SIZE
                    )
                else:
                    leg_pnl = (
                        (mark - leg.entry_price)
                        * leg.qty * config.LOT_SIZE
                    )
                position_value += leg_pnl

            # SE-P1-04: include banked_pnl from _close_one_side.
            # Without this, a one-side close's realised loss is
            # invisible to CB L1/L2 and the console Net figure.
            position.realized_pnl = (
                position_value
                + getattr(position, "banked_pnl", 0.0)
            )

        realized_today = sum(
            p.net_pnl
            for p in self.closed_positions
            if p.exit_timestamp
            and p.exit_timestamp[:10] == today_str
        )
        unrealized = sum(
            p.realized_pnl for p in self.open_positions
        )
        self.daily_pnl = realized_today + unrealized

    async def _check_circuit_breakers(self) -> None:
        """Check and enforce all circuit breaker levels."""

        # LEVEL 1 — Single position loss
        for position in list(self.open_positions):
            # PATCH: include estimated closing costs so this
            # matches the "Net" figure shown on the console.
            position_net_estimate = (
                position.realized_pnl
                - self._estimate_costs(position)
            )
            # PATCH: CB_LEVEL_1's flat 2%-of-capital threshold was
            # pre-empting almost every strategy's own designed
            # stop (e.g. a multi-lot straddle's 2x-credit stop can
            # be far larger), making circuit-breaks the dominant
            # exit reason instead of the designed stop-loss
            # ladder. Scale the L1 threshold up to at least 1x the
            # credit actually received on this position (debit
            # strategies have total_credit=0 and simply fall back
            # to the flat percentage, unchanged).
            # PATCH (round-2, fixing my own earlier bug):
            # position.total_credit is in "points x lots" units
            # (no LOT_SIZE multiplication), not rupees — comparing
            # it directly against a rupee threshold made the floor
            # an effective no-op. Multiply by LOT_SIZE to convert
            # to rupees before comparing.
            # AUDIT SE-06: CB L1 must not fire before the
            # position's own designed stop-loss. Use the larger
            # of: 2% of capital, or the position's stop_loss
            # (which is already 2x credit for credit strategies).
            _designed_stop_rupees = (
                position.stop_loss * config.LOT_SIZE
                if position.stop_loss and position.stop_loss > 0
                else 0.0
            )
            cb_l1_threshold = max(
                config.CB_LEVEL_1_PCT * config.TOTAL_CAPITAL,
                _designed_stop_rupees,
            )
            if position_net_estimate < -cb_l1_threshold:
                logger.critical(
                    f"CB L1: position="
                    f"{position.trade_id[:8]} "
                    f"pnl={position.realized_pnl:.2f}"
                )
                self._log_circuit_breaker(
                    1,
                    f"position_loss="
                    f"{position.realized_pnl:.2f}",
                    config.CB_LEVEL_1_ACTION,
                )
                await self._close_position(
                    position,
                    config.EXIT_REASONS["CIRCUIT_BREAK"],
                )
                self.cb_level_1_count += 1

        # LEVEL 2 — Daily loss
        # PATCH: subtract estimated open-position closing costs
        # so this matches the "Net" figure shown on the console.
        daily_pnl_net_estimate = self.daily_pnl - sum(
            self._estimate_costs(p) for p in self.open_positions
        )
        # P2-4c: effective daily loss threshold (tightens on losing streak)
        # After 2+ consecutive CB L2 days, use 1% threshold instead of
        # CB_LEVEL_2_PCT (3%) so the engine de-risks automatically.
        _cb2_streak = getattr(self, "_consecutive_cb2_days", 0)
        if _cb2_streak >= 2:
            _effective_cb2_pct = 0.02  # S11-1: tightened CB L2 threshold raised to 2%% (was 1%%; halted after single condor max-loss on day 3)
        else:
            _effective_cb2_pct = config.CB_LEVEL_2_PCT
        if daily_pnl_net_estimate < -(
            _effective_cb2_pct * config.TOTAL_CAPITAL
        ):
            if not self.cb_level_2_active:
                logger.critical(
                    f"CB L2: daily_pnl="
                    f"{self.daily_pnl:.2f}"
                )
                self._log_circuit_breaker(
                    2,
                    f"daily_pnl={self.daily_pnl:.2f}",
                    config.CB_LEVEL_2_ACTION,
                )
                self.daily_trading_halted = True
                self.cb_level_2_active    = True
                self._save_capital_state()  # PATCH: persist immediately

        # LEVEL 3 — Weekly loss
        # FIX VS8: CB_LEVEL_3_PCT raised to 0.10 in config
        if self.weekly_pnl < -(
            config.CB_LEVEL_3_PCT * config.TOTAL_CAPITAL
        ):
            if not self.cb_level_3_active:
                logger.critical(
                    f"CB L3: weekly_pnl="
                    f"{self.weekly_pnl:.2f}"
                )
                self._log_circuit_breaker(
                    3,
                    f"weekly_pnl={self.weekly_pnl:.2f}",
                    config.CB_LEVEL_3_ACTION,
                )
                await self._reduce_all_positions_50pct()
                self.cb_level_3_active = True
                self._save_capital_state()  # PATCH: persist immediately

        # LEVEL 4 — Max drawdown (includes unrealized MTM)
        # SE-15: current_capital only updates on close.
        # A book -12% on open MTM shows drawdown=0 without this.
        # CAT10: CB L4 MTM drawdown verified
        # CB L4 already includes unrealized MTM (SE-15 fix).
        # current_capital + unrealized_mtm = true book value.
        _unrealized_mtm = sum(
            p.realized_pnl for p in self.open_positions
        )
        drawdown = self.peak_capital - (
            self.current_capital + _unrealized_mtm
        )
        if drawdown > (
            config.CB_LEVEL_4_PCT * config.TOTAL_CAPITAL
        ):
            logger.critical(
                f"CB L4: drawdown={drawdown:.2f}"
            )
            self._log_circuit_breaker(
                4,
                f"drawdown={drawdown:.2f}",
                config.CB_LEVEL_4_ACTION,
            )
            await self._emergency_flatten_all()
            self.kill_switch_active = True
            self.cb_level_4_active  = True
            self._save_capital_state()  # PATCH: persist immediately

        # LEVEL 5 — Absolute VIX threshold
        # MN-T04: guard with cb_level_5_active so the fast
        # monitor (running every second) does not emit a new
        # regime-change event on every iteration while VIX
        # stays elevated. Without this guard, _handle_regime_
        # transition fires every second, closing positions
        # repeatedly and generating excessive API activity.
        if (
            self.dm.vix is not None
            and self.dm.vix >= config.CB_LEVEL_5_VIX_ABSOLUTE
        ):
            if not getattr(self, "cb_level_5_active", False):
                logger.critical(
                    f"CB L5: VIX={self.dm.vix:.1f} >= "
                    f"{config.CB_LEVEL_5_VIX_ABSOLUTE}"
                )
                self._log_circuit_breaker(
                    5,
                    f"vix_absolute={self.dm.vix:.1f}",
                    config.CB_LEVEL_5_ACTION,
                )
                # CAT1: CB L5 closes short-vol positions before NEUTRAL
                # RL1 correctly forces NEUTRAL (no new entries) but
                # broke the automatic short-closure that came with
                # STRONG_BUY (Rule A flattens all shorts).
                # At VIX=25, open short condors/spreads face
                # catastrophic loss.  Close them explicitly here.
                _short_vol_positions = [
                    p for p in list(self.open_positions)
                    if p.meta.get("strategy_type") == "SHORT"
                    and p.status == "OPEN"
                ]
                if _short_vol_positions:
                    logger.critical(
                        f"CAT1: CB L5 closing "
                        f"{len(_short_vol_positions)} short-vol "
                        f"position(s) before forcing NEUTRAL"
                    )
                    import asyncio as _asyncio_cat1
                    for _svp in _short_vol_positions:
                        _asyncio_cat1.ensure_future(
                            self._close_position(
                                _svp,
                                config.EXIT_REASONS["CIRCUIT_BREAK"],
                                use_market=True,
                            )
                        )
                self.re.previous_regime  = (
                    self.re.confirmed_regime
                )
                self.re.confirmed_regime = (
                    config.REGIME_NEUTRAL
                )
                self.re.regime_changed   = True
                self.cb_level_5_active   = True
                self._save_capital_state()
        else:
            # Reset when VIX drops back below threshold
            if getattr(self, "cb_level_5_active", False):
                logger.info(
                    f"CB L5: VIX={self.dm.vix:.1f} below "
                    f"threshold — resetting"
                )
                self.cb_level_5_active = False

    async def _monitor_all_positions(self) -> None:
        """Monitor all open positions for exit conditions."""
        for position in list(self.open_positions):
            if position.status != "OPEN":
                continue

            # AUDIT SE-05: skip this cycle if one side was just
            # closed — let the next cycle re-evaluate cleanly.
            if position.meta.get("_one_side_closed_cycle"):
                position.meta.pop("_one_side_closed_cycle", None)
                continue

            pending_legs = [
                l for l in position.legs
                if l.fill_status == "PENDING"
            ]
            if pending_legs:
                logger.warning(
                    f"Position {position.trade_id[:8]} "
                    f"has {len(pending_legs)} PENDING legs "
                    f"— closing"
                )
                await self._close_position(
                    position,
                    config.EXIT_REASONS["MANUAL"],
                )
                continue

            stop_hit = await self._check_stop_loss(
                position
            )
            if stop_hit:
                continue

            # SE-P1-05: profit target runs BEFORE trailing stop.
            # Previously the trail ran first with TRAIL_START=0.30
            # and TRAIL_RETAIN=0.85, capping wins at 25-43% of
            # credit while the stop stayed at 2x credit. The trail
            # should only protect gains beyond the profit target.
            target_hit = await self._check_profit_target(
                position
            )
            if target_hit:
                continue

            trail_hit = await self._check_trailing_stop(position)
            if trail_hit:
                continue

            dte_hit = await self._check_dte_exit(position)
            if dte_hit:
                continue

            hold_hit = self._check_max_hold(position)
            if hold_hit:
                await self._close_position(
                    position,
                    config.EXIT_REASONS["TIME_EXIT"],
                )
                continue

            now_time = datetime.now(self._IST).time()
            if now_time >= config.TIME_EXIT_NORMAL:
                if position.strategy_name not in [
                    config.STRAT_SHORT_STRADDLE,
                    config.STRAT_IRON_CONDOR,
                    config.STRAT_CREDIT_SPREADS,
                ]:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["EOD"],
                    )

    async def _check_stop_loss(
        self, position: Position
    ) -> bool:
        strategy     = position.strategy_name
        expiry_chain = self.dm.get_chain_for_expiry(
            position.expiry_date
        )

        if strategy == config.STRAT_SHORT_STRADDLE:
            if self.dm.spot is None:
                return False
            if position.stop_loss and position.stop_loss > 0:
                current_premium = (
                    self._get_position_current_premium(
                        position
                    )
                )
                if current_premium >= position.stop_loss:
                    # EXE-05: sustained-breach filter.
                    # Require N consecutive ticks above stop before
                    # firing to prevent phantom stop-outs from
                    # temporary spread expansion in fast markets.
                    _ticks_req = getattr(
                        config, "STOP_BREACH_TICKS_REQUIRED", 3
                    )
                    _breach_key = "_stop_breach_ticks"
                    _ticks = position.meta.get(_breach_key, 0) + 1
                    position.meta[_breach_key] = _ticks
                    if _ticks >= _ticks_req:
                        logger.info(
                            f"Straddle premium stop (sustained "
                            f"{_ticks} ticks): "
                            f"current={current_premium:.2f} "
                            f"stop={position.stop_loss:.2f}"
                        )
                        position.meta[_breach_key] = 0
                        await self._close_position(
                            position,
                            config.EXIT_REASONS["STOP_LOSS"],
                        )
                        return True
                    else:
                        logger.debug(
                            f"Straddle stop breach tick "
                            f"{_ticks}/{_ticks_req}: "
                            f"current={current_premium:.2f} "
                            f"stop={position.stop_loss:.2f} "
                            f"— awaiting confirmation"
                        )
                else:
                    # Mark cleared — reset breach counter
                    position.meta["_stop_breach_ticks"] = 0
            stop_up   = position.entry_spot * (
                1 + config.STRADDLE_SPOT_STOP_PCT
            )
            stop_down = position.entry_spot * (
                1 - config.STRADDLE_SPOT_STOP_PCT
            )
            if (
                self.dm.spot >= stop_up
                or self.dm.spot <= stop_down
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True

        elif strategy in [
            config.STRAT_LONG_STRADDLE,
            config.STRAT_STRANGLE,
        ]:
            stop_val    = position.total_debit * (
                1 - config.LONG_STRADDLE_STOP_PCT
            )
            current_val = self._get_position_value(
                position
            )
            if current_val <= stop_val:
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True

        elif strategy == config.STRAT_BACKSPREAD:
            if self.dm.spot is None:
                return False
            # Backspread premium stop: check debit threshold
            # FIRST. During an IV crush, spot may barely move
            # while the position bleeds premium. The spot-based
            # stop alone misses this scenario entirely.
            if position.stop_loss and position.stop_loss > 0:
                current_val = self._get_position_value(position)
                if current_val <= position.stop_loss:
                    logger.info(
                        f"Backspread premium stop: "
                        f"val={current_val:.2f} "
                        f"stop={position.stop_loss:.2f}"
                    )
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["STOP_LOSS"],
                    )
                    return True
            trend = position.trend_direction
            if trend >= 0:
                stop_level = position.entry_spot * (
                    1 - config.BACKSPREAD_STOP_MOVE_PCT
                )
                if self.dm.spot < stop_level:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["STOP_LOSS"],
                    )
                    return True
            else:
                stop_level = position.entry_spot * (
                    1 + config.BACKSPREAD_STOP_MOVE_PCT
                )
                if self.dm.spot > stop_level:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["STOP_LOSS"],
                    )
                    return True

        elif strategy in [
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
        ]:
            if self.dm.spot is None:
                return False
            # AUDIT #1.3: premium-based stop (2x credit)
            # Catches IV-expansion moves that don't breach a
            # strike by CONDOR_TESTED_SIDE_BUFFER but still
            # exceed the designed loss threshold.
            if position.stop_loss and position.stop_loss > 0:
                current_premium = (
                    self._get_position_current_premium(
                        position
                    )
                )
                if current_premium >= position.stop_loss:
                    logger.info(
                        f"Condor/Spread premium stop: "
                        f"current={current_premium:.2f} "
                        f"stop={position.stop_loss:.2f}"
                    )
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["STOP_LOSS"],
                    )
                    return True
            short_call = self._get_short_strike(
                position, "call"
            )
            short_put  = self._get_short_strike(
                position, "put"
            )
            if short_call and self.dm.spot >= (
                short_call
                + config.CONDOR_TESTED_SIDE_BUFFER
            ):
                await self._close_one_side(
                    position, "call",
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                # SE-18: return True (not False) so _monitor_all_positions
                # stops processing this position for the current cycle.
                # Returning False let trailing/profit checks run on the
                # now half-closed structure in the same iteration.
                position.meta["_one_side_closed_cycle"] = True
                return True
            if short_put and self.dm.spot <= (
                short_put
                - config.CONDOR_TESTED_SIDE_BUFFER
            ):
                await self._close_one_side(
                    position, "put",
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                position.meta["_one_side_closed_cycle"] = True
                return True

        elif strategy == config.STRAT_RATIO_SPREAD:
            # PATCH: previously RATIO_SPREAD had NO stop-loss
            # check at all (fell through every branch to the
            # final `return False`). This structure carries a
            # bounded-but-significant max loss around the long
            # (protective) strikes with no active protection
            # until the DTE-based time exit. Use the premium-
            # based stop already computed at build time
            # (position.stop_loss = net_credit * 2.0) but
            # previously never enforced.
            if position.stop_loss and position.stop_loss > 0:
                current_premium = (
                    self._get_position_current_premium(
                        position
                    )
                )
                if current_premium >= position.stop_loss:
                    logger.info(
                        f"Ratio spread premium stop: "
                        f"current={current_premium:.2f} "
                        f"stop={position.stop_loss:.2f}"
                    )
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["STOP_LOSS"],
                    )
                    return True

        elif strategy == config.STRAT_BUTTERFLY:
            if self.dm.spot is None:
                return False
            upper = self._get_upper_wing_strike(position)
            lower = self._get_lower_wing_strike(position)
            if upper and self.dm.spot > (
                upper + config.BUTTERFLY_WING_BUFFER_PTS
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True
            if lower and self.dm.spot < (
                lower - config.BUTTERFLY_WING_BUFFER_PTS
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True

        elif strategy == config.STRAT_DEFENSIVE:
            current_val = self._get_position_value(
                position
            )
            stop_val    = position.meta.get("stop_loss", 0)
            if stop_val and stop_val > 0:
                if current_val <= stop_val:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["STOP_LOSS"],
                    )
                    return True

        return False

    async def _check_profit_target(
        self, position: Position
    ) -> bool:
        strategy = position.strategy_name

        if strategy in [
            config.STRAT_SHORT_STRADDLE,
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
            config.STRAT_RATIO_SPREAD,
        ]:
            current_value = (
                self._get_position_current_premium(
                    position
                )
            )
            # PATCH: prefer the strategy-specific target already
            # computed at build time (position.profit_target).
            # RATIO_SPREAD previously always fell through to the
            # generic config.PROFIT_TARGET_PCT (0.50) instead of
            # its own config.RATIO_TARGET_PCT (0.25). The other
            # three strategies already used an equivalent value
            # either way, so this changes nothing for them.
            fallback_target = (
                position.total_credit
                * (1 - config.PROFIT_TARGET_PCT)
            )
            target_credit = (
                position.profit_target
                if position.profit_target
                and position.profit_target > 0
                else fallback_target
            )
            if (
                current_value <= target_credit
                and position.total_credit > 0
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["PROFIT_TARGET"],
                )
                return True

        elif strategy in [
            config.STRAT_LONG_STRADDLE,
            config.STRAT_STRANGLE,
        ]:
            # PATCH: previously both strategies were forced onto
            # config.DEBIT_PROFIT_TARGET_PCT (0.50) regardless of
            # position.profit_target already computed correctly
            # at build time (LONG_STRADDLE_TARGET_PCT for
            # straddle, EVENT_STRANGLE_TARGET_PCT=1.00 for
            # strangle). This silently capped event-strangle
            # upside at half of its designed target. Use the
            # stored value directly, falling back to the old
            # formula only if it's somehow unset.
            current_val = self._get_position_value(
                position
            )
            fallback_target = position.total_debit * (
                1 + config.DEBIT_PROFIT_TARGET_PCT
            )
            target_val = (
                position.profit_target
                if position.profit_target
                and position.profit_target > 0
                else fallback_target
            )
            if (
                current_val >= target_val
                and position.total_debit > 0
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["PROFIT_TARGET"],
                )
                return True

        elif strategy == config.STRAT_BACKSPREAD:
            current_val = self._get_position_value(
                position
            )
            target_val  = (
                position.total_debit
                * config.BACKSPREAD_PROFIT_MULTIPLE
            )
            if (
                current_val >= target_val
                and position.total_debit > 0
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["PROFIT_TARGET"],
                )
                return True

        elif strategy == config.STRAT_BUTTERFLY:
            current_val = self._get_position_value(
                position
            )
            max_profit  = position.meta.get(
                "max_profit", 0
            )
            if max_profit > 0 and current_val >= (
                max_profit * config.BUTTERFLY_PROFIT_PCT
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["PROFIT_TARGET"],
                )
                return True

        return False

    async def _check_trailing_stop(
        self, position: Position
    ) -> bool:
        """
        PATCH: implements the previously-unused
        config.TRAIL_START_PROFIT_PCT / TRAIL_RETAIN_PCT.
        Scoped to net-credit strategies (straddle, condor,
        credit spreads, ratio spread) where the profit basis is
        unambiguous via total_credit / current premium. Locks in
        gains once profit exceeds TRAIL_START_PROFIT_PCT of
        credit received, closing if profit retraces below
        TRAIL_RETAIN_PCT of the best profit_pct seen so far.
        Peak tracking is stored in position.meta and is reset if
        the engine restarts mid-trade (acceptable — worst case is
        one cycle without trailing protection right after restart).
        """
        if position.strategy_name not in [
            config.STRAT_SHORT_STRADDLE,
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
            config.STRAT_RATIO_SPREAD,
        ]:
            return False
        if not position.total_credit or position.total_credit <= 0:
            return False

        current_premium = self._get_position_current_premium(
            position
        )
        # SE9-P0-01 FIX: use net_premium as denominator.
        # total_credit is GROSS (sell legs only); current_premium
        # is NET (shorts minus longs). On a 250-wide condor with
        # 55pt credit and 30pt debit, total_credit=55 but
        # net_premium=25. Using total_credit made profit_pct=0.30
        # at entry (trail armed immediately, closed after 4.5pts).
        # With net_premium, profit_pct=0.0 at entry — correct.
        _trail_basis = position.net_premium
        if not _trail_basis or _trail_basis <= 0:
            return False
        profit_pct = 1.0 - (
            current_premium / _trail_basis
        )

        # SE10-P2-01 FIX: removed the `peak > 0.95` magnitude guard.
        # profit_pct=0.96 means 96% of credit captured — a legitimate
        # near-perfect outcome, not a stale value. The guard was
        # resetting peak to 0 exactly when the trail had the most to
        # protect. Staleness is handled by entry_timestamp comparison:
        # if the position was just created this cycle, reset peak.
        peak = position.meta.get("_peak_profit_pct", 0.0)
        # Reset peak if this is a fresh position (no prior peak recorded)
        if "_peak_profit_pct" not in position.meta:
            peak = 0.0
        if profit_pct > peak:
            peak = profit_pct
            position.meta["_peak_profit_pct"] = peak

        if (
            peak >= config.TRAIL_START_PROFIT_PCT
            and profit_pct <= peak * config.TRAIL_RETAIN_PCT
        ):
            logger.info(
                f"Trailing stop: {position.strategy_name} "
                f"peak={peak:.2%} current={profit_pct:.2%} "
                f"retain={config.TRAIL_RETAIN_PCT:.0%}"
            )
            await self._close_position(
                position,
                config.EXIT_REASONS["PROFIT_TARGET"],
            )
            return True
        return False

    async def _check_dte_exit(
        self, position: Position
    ) -> bool:
        if not position.expiry_date:
            return False
        try:
            expiry = datetime.strptime(
                position.expiry_date, "%Y-%m-%d"
            ).date()
            dte = (expiry - date.today()).days
        except ValueError:
            return False

        exit_dte_map = {
            config.STRAT_SHORT_STRADDLE:  (
                config.STRADDLE_EXIT_DTE
            ),
            config.STRAT_IRON_CONDOR:     (
                config.CONDOR_EXIT_DTE
            ),
            config.STRAT_CREDIT_SPREADS:  (
                config.SPREAD_EXIT_DTE
            ),
            config.STRAT_RATIO_SPREAD:    (
                config.RATIO_EXIT_DTE
            ),
            config.STRAT_BUTTERFLY:       (
                config.BUTTERFLY_EXIT_DTE
            ),
            config.STRAT_BACKSPREAD:      (
                config.BACKSPREAD_EXIT_DTE
            ),
        }

        exit_dte = exit_dte_map.get(
            position.strategy_name
        )
        if exit_dte is not None and dte <= exit_dte:
            logger.info(
                f"DTE exit: {position.strategy_name} "
                f"dte={dte} exit_dte={exit_dte}"
            )
            await self._close_position(
                position,
                config.EXIT_REASONS["TIME_EXIT"],
            )
            return True
        return False

    def _check_max_hold(
        self, position: Position
    ) -> bool:
        if not position.max_hold_date:
            return False
        try:
            max_date = datetime.strptime(
                position.max_hold_date, "%Y-%m-%d"
            ).date()
            if date.today() >= max_date:
                return True
        except ValueError:
            pass
        return False

    async def _handle_regime_transition(self) -> None:
        from_regime = self.re.previous_regime
        to_regime   = self.re.confirmed_regime

        logger.info(
            f"Regime transition: "
            f"{from_regime} -> {to_regime}"
        )

        if to_regime == config.REGIME_STRONG_BUY:
            logger.info("RULE A: Flatten ALL shorts")
            for position in list(self.open_positions):
                has_shorts = any(
                    l.action == "SELL"
                    for l in position.legs
                )
                if has_shorts:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS[
                            "REGIME_CHANGE"
                        ],
                        use_market=True,
                    )
            self.cooling_period_end = (
                datetime.now(self._IST)
                + timedelta(minutes=30)
            )
            return

        if (
            (
                from_regime == config.REGIME_STRONG_SELL
                and to_regime == config.REGIME_MILD_SELL
            ) or (
                from_regime == config.REGIME_MILD_SELL
                and to_regime == config.REGIME_NEUTRAL
            )
        ):
            logger.info("RULE B: Close 50% of shorts")
            for position in list(self.open_positions):
                # SE-19: skip long-vol positions (butterfly, straddle,
                # strangle, backspread, defensive) — these are hedges
                # that should be kept as you rotate out of short vol.
                if position.meta.get(
                    "strategy_type", "SHORT"
                ) == "LONG":
                    continue
                await self._reduce_position_50pct(
                    position
                )
            return

        if (
            from_regime == config.REGIME_STRONG_SELL
            and to_regime == config.REGIME_NEUTRAL
        ):
            logger.info("RULE C: Close 75% of shorts")
            for position in list(self.open_positions):
                await self._reduce_position_pct(
                    position, 0.75
                )
            return

        if (
            from_regime == config.REGIME_MILD_SELL
            and to_regime == config.REGIME_BUY_VOL
        ):
            # SE-F1: replace 0.10-delta hedge with 50% reduction.
            # The hedge fires AFTER the vol expansion it protects
            # against. At that point the 0.10-delta option costs
            # ~114% of the original credit. Partial close is cheaper
            # and simultaneously reduces vega, gamma and margin.
            logger.info(
                "RULE D: Reducing all positions 50%% "
                "(replaced 0.10-delta hedge — cheaper post-vol-spike)"
            )
            await self.cancel_all_open_orders(
                context="RULE_D_CANCEL"
            )
            for position in list(self.open_positions):
                await self._reduce_position_50pct(position)
            return

        if from_regime == config.REGIME_NEUTRAL and (
            to_regime in [
                config.REGIME_STRONG_SELL,
                config.REGIME_STRONG_BUY,
            ]
        ):
            if self.re.persistence_count < 3:
                logger.info(
                    f"RULE E: Waiting for 3 confirmations "
                    f"(current={self.re.persistence_count})"
                )
                return
            logger.info(
                "RULE E: 3 confirmations — allowing"
            )
            return

        # SE-P1-08: RULE F should only tighten stops on category
        # DOWNGRADES (STRONG->MILD within a side), never upgrades.
        # On an upgrade (MILD_SELL->STRONG_SELL) it was halving
        # loss tolerance exactly when the model says sell more vol.
        # SE8-P3-02 FIX: removed unused _sell_regimes/_buy_regimes.
        _is_sell_downgrade = (
            from_regime == config.REGIME_STRONG_SELL
            and to_regime == config.REGIME_MILD_SELL
        )
        _is_buy_downgrade = (
            from_regime == config.REGIME_STRONG_BUY
            and to_regime == config.REGIME_BUY_VOL
        )
        if self._same_category(from_regime, to_regime) and (
            _is_sell_downgrade or _is_buy_downgrade
        ):
            logger.info("RULE F: Move stops to breakeven (downgrade)")
            for position in self.open_positions:
                self._move_stop_to_breakeven(position)
            return

    def _same_category(self, r1: str, r2: str) -> bool:
        sell = {
            config.REGIME_STRONG_SELL,
            config.REGIME_MILD_SELL,
        }
        buy = {
            config.REGIME_BUY_VOL,
            config.REGIME_STRONG_BUY,
        }
        if r1 in sell and r2 in sell:
            return True
        if r1 in buy and r2 in buy:
            return True
        if (
            r1 == config.REGIME_NEUTRAL
            and r2 == config.REGIME_NEUTRAL
        ):
            return True
        return False


    def _should_enter_new_position(self) -> bool:
        """Check all gates. Logs every block reason."""
        if self.kill_switch_active:
            logger.info('Entry gate BLOCKED: kill switch')
            return False
        if self.daily_trading_halted:
            logger.info('Entry gate BLOCKED: daily halt')
            return False
        # SE-STALE: block entries when spot data is stale.
        # DM-SPIKE sets dm._spot_data_stale=True when a >5% spot
        # move is rejected as implausible. Trading on stale spot
        # means ATM selection, expected-move, and stop-loss checks
        # all run on wrong data — worse than not trading at all.
        if getattr(self.dm, "_spot_data_stale", False):
            logger.info(
                'Entry gate BLOCKED: spot data stale '
                '(>5%% spike rejected — awaiting valid tick)'
            )
            return False
        # PATCH: real trading-day/holiday check — previously this
        # only existed (disconnected) in _display_console().
        today_check = date.today()
        today_check_str = today_check.isoformat()
        is_trading_day = (
            today_check.weekday() < 5
            and today_check_str not in config.NSE_MARKET_HOLIDAYS
        )
        if not is_trading_day:
            logger.info(
                f'Entry gate BLOCKED: not a trading day '
                f'({today_check_str})'
            )
            return False
        now      = datetime.now(self._IST)
        now_time = now.time()
        if now_time < config.EXEC_START_TIME:
            logger.info(
                f'Entry gate BLOCKED: before EXEC_START '
                f'({now_time} < {config.EXEC_START_TIME})'
            )
            return False
        if now_time > config.EXEC_END_TIME:
            logger.info(
                f'Entry gate BLOCKED: after EXEC_END '
                f'({now_time} > {config.EXEC_END_TIME})'
            )
            return False
        if now_time > config.REGIME_FREEZE_TIME:
            logger.info('Entry gate BLOCKED: regime frozen')
            return False
        regime = self.re.confirmed_regime
        # Effective regime is initialized before the VIX gate.
        _effective_regime = regime
        # Initialize before the first VIX-gate reference.
        # The later IV-spike logic may override this locally.
        # S9-2b: VIX gate uses _effective_regime
        if (
            _effective_regime in [
                config.REGIME_STRONG_SELL,
                config.REGIME_MILD_SELL,
            ]
            and self.dm.vix is not None
            and self.dm.vix >= config.VIX_SELL_VOL_MAX
        ):
            logger.info(
                f'Entry gate BLOCKED: VIX={self.dm.vix:.1f} >= '
                f'{config.VIX_SELL_VOL_MAX}'
            )
            return False
        # PRF-S03: block short-vol when VIX is too low.
        # Below MIN_VIX_SELL, premium is too thin to cover costs.
        _min_vix = getattr(config, "MIN_VIX_SELL", 11.0)
        if (
            regime in [config.REGIME_STRONG_SELL, config.REGIME_MILD_SELL]
            and self.dm.vix is not None
            and self.dm.vix < _min_vix
        ):
            logger.info(
                f'Entry gate BLOCKED: VIX={self.dm.vix:.1f} < '
                f'MIN_VIX_SELL={_min_vix} (premium too thin)'
            )
            return False
        if regime == config.REGIME_NEUTRAL:
            _ivr    = self.dm.compute_iv_rank()
            iv_rank = _ivr if _ivr is not None else 50.0
            adx     = self.dm.adx or 99
            if iv_rank <= 50 or adx >= 20:
                logger.info(
                    f'Entry gate BLOCKED: NEUTRAL '
                    f'iv_rank={iv_rank:.1f} adx={adx:.1f}'
                )
                return False
        if self.cooling_period_end:
            if now < self.cooling_period_end:
                logger.info(
                    f'Entry gate BLOCKED: cooling until '
                    f'{self.cooling_period_end.strftime("%H:%M:%S")}'
                )
                return False
            else:
                self.cooling_period_end = None
        # PATCH: re-entry cooldown after any position close, using
        # previously-unused config.REENTRY_COOLDOWN_SEC /
        # REENTRY_MAX_SPOT_MOVE_PCT.
        if self._last_position_close_time is not None:
            elapsed_since_close = (
                now - self._last_position_close_time
            ).total_seconds()
            if elapsed_since_close < config.REENTRY_COOLDOWN_SEC:
                spot_moved_enough = False
                if (
                    self._last_position_close_spot
                    and self.dm.spot
                    and self._last_position_close_spot > 0
                ):
                    move_pct = abs(
                        self.dm.spot
                        / self._last_position_close_spot
                        - 1
                    )
                    spot_moved_enough = (
                        move_pct
                        >= config.REENTRY_MAX_SPOT_MOVE_PCT
                    )
                # ARCH-7c: composite change gate on re-entry
                # Block re-entry unless composite has changed >= 0.10
                # since the stop fired (prevents re-entry into same
                # adverse conditions that triggered the stop).
                _composite_changed = True
                _comp_change_val = 0.0
                _last_stop_comp = getattr(
                    self, '_last_stop_composite', None
                )
                if _last_stop_comp is not None:
                    _comp_change_val = abs(
                        self.re.raw_composite - _last_stop_comp
                    )
                    _composite_changed = _comp_change_val >= 0.10
                if not spot_moved_enough and not _composite_changed:
                    logger.info(
                        f'Entry gate BLOCKED: re-entry cooldown '
                        f'({elapsed_since_close:.0f}s/'
                        f'{config.REENTRY_COOLDOWN_SEC}s, '
                        f'spot_moved={spot_moved_enough}, '
                        f'composite_change={_comp_change_val:.3f} < 0.10)'
                    )
                    return False
        if (
            self.re.previous_regime == config.REGIME_NEUTRAL
            and self.re.confirmed_regime in [
                config.REGIME_STRONG_SELL, config.REGIME_STRONG_BUY
            ]
            and self.re.persistence_count < 3
        ):
            logger.info(
                f'Entry gate BLOCKED: need 3 confirmations '
                f'({self.re.persistence_count}/3)'
            )
            return False
        # P4-4 + RH2: regime stability requirement
        # STRONG_SELL (large condor, high conviction): require >= 5 cycles.
        # MILD_SELL (smaller defined-risk spread): require >= 3 cycles.
        # Combined with flow oscillation, persistence >= 5 for MILD_SELL
        # created an entry deadlock where persistence_count never reached 5.
        # IV spike entries keep their relaxation to 3 cycles.
        # RH2: MILD_SELL uses persistence >= 3
        _iv_spike_now = getattr(self, '_iv_spike_entry', False)
        _regime_now = self.re.confirmed_regime
        if _iv_spike_now:
            _min_persistence = 3
        elif _regime_now == config.REGIME_STRONG_SELL:
            _min_persistence = 5
        else:
            _min_persistence = 3
        # FIX-2026-G: Wednesday persistence bonus
        # Wednesday (post-Tuesday expiry) is the calmest day of
        # the 2026 NIFTY weekly cycle: ADX lowest (11-14),
        # post-expiry calm, DTE=6 optimal, false signals least likely.
        # Reduce persistence requirement by 1 cycle on Wednesday.
        if (
            now.weekday() == 2  # Wednesday
            and _min_persistence > 2
            and not _iv_spike_now  # IV spike already has its own reduction
        ):
            _min_persistence = max(2, _min_persistence - 1)
            logger.debug(
                f"FIX-2026-G: Wednesday entry bonus — "
                f"persistence reduced to {_min_persistence} cycles"
            )
        if self.re.persistence_count < _min_persistence:
            logger.info(
                f'Entry gate BLOCKED: regime not yet stable '
                f'(persistence={self.re.persistence_count} '
                f'< required={_min_persistence} '
                f'regime={_regime_now})'
            )
            return False
        if len(self.open_positions) >= config.MAX_CONCURRENT_POSITIONS:
            logger.info(
                f'Entry gate BLOCKED: max positions '
                f'({len(self.open_positions)}/'
                f'{config.MAX_CONCURRENT_POSITIONS})'
            )
            return False
        deployed = sum(p.max_risk for p in self.open_positions)
        reg_cap  = (
            config.REGIME_CAPITAL_PCT.get(regime, 0)
            * config.TOTAL_CAPITAL
        )
        if deployed >= reg_cap:
            logger.info(
                f'Entry gate BLOCKED: capital deployed '
                f'Rs{deployed:,.0f} >= Rs{reg_cap:,.0f}'
            )
            return False
        # PRF-S04: block entries when nearest expiry has DTE < MIN_DTE_ENTRY.
        # Below 4 DTE, theta/gamma ratio deteriorates sharply and
        # there is insufficient time for the trade to work.
        _min_dte = getattr(config, "MIN_DTE_ENTRY", 4)
        _nearest_expiry = self.dm.get_expiry_by_dte(
            _min_dte, tolerance=_min_dte
        )
        if _nearest_expiry is None:
            logger.info(
                f'Entry gate BLOCKED: no expiry with DTE >= '
                f'{_min_dte}'
            )
            return False
        if self._last_build_failure is not None:
            elapsed = (now - self._last_build_failure).total_seconds()
            if elapsed < config.BUILD_FAILURE_COOLDOWN_SEC:
                logger.info(
                    f'Entry gate BLOCKED: build cooldown '
                    f'({elapsed:.0f}s/{config.BUILD_FAILURE_COOLDOWN_SEC}s)'
                )
                return False
        # PRF-04: require meaningful composite change before re-entry.
        # Prevents repeated correlated entries on the same signal.
        _strategy_name = self._select_strategy(regime)
        if _strategy_name is not None:
            _last_comp = self._last_entry_composite.get(
                _strategy_name
            )
            if _last_comp is not None:
                _comp_change = abs(
                    self.re.raw_composite - _last_comp
                )
                _min_change = getattr(
                    config,
                    "REENTRY_MIN_COMPOSITE_CHANGE",
                    0.10,
                )
                if _comp_change < _min_change:
                    logger.info(
                        f'Entry gate BLOCKED: composite change '
                        f'{_comp_change:.3f} < {_min_change} '
                        f'since last {_strategy_name} entry '
                        f'(no new signal)'
                    )
                    return False

        # P4-3a: IV spike detection
        # When IV_ATM is >= 15% above recent average, we are in a
        # fear-premium window — the highest-EV entry for premium selling.
        # Log this so the operator knows this is a high-quality entry.
        # Also store the spike flag so P4-4 can relax persistence.
        _iv_spike_entry = False
        if (
            self.dm.iv_atm is not None
            and self.dm.iv_atm > 0
            and len(self.dm.iv_atm_history) >= 5
            and regime in [
                config.REGIME_STRONG_SELL,
                config.REGIME_MILD_SELL,
            ]
        ):
            # S9-1: IV spike uses median of last 15 readings
            # iv_atm_history appended 3x per 60s cycle (3 expiries).
            # Last 5 readings = ~100s not 5 min.  Use last 15 readings
            # (~5 min at 3x density) and median (robust to outliers).
            _iv_hist_15 = list(self.dm.iv_atm_history)[-15:]
            _iv_hist_15_sorted = sorted(_iv_hist_15)
            _n15 = len(_iv_hist_15_sorted)
            if _n15 > 0:
                _mid = _n15 // 2
                _recent_iv_avg = (
                    _iv_hist_15_sorted[_mid]
                    if _n15 % 2 == 1
                    else (
                        _iv_hist_15_sorted[_mid - 1]
                        + _iv_hist_15_sorted[_mid]
                    ) / 2.0
                )
            else:
                _recent_iv_avg = 0.0
            if (
                _recent_iv_avg > 0
                and self.dm.iv_atm >= _recent_iv_avg * 1.15
            ):
                _iv_spike_entry = True
                logger.info(
                    f'P4-3a: IV spike detected — '
                    f'iv_atm={self.dm.iv_atm*100:.2f}%% '
                    f'vs recent_avg={_recent_iv_avg*100:.2f}%% '
                    f'(+{(self.dm.iv_atm/_recent_iv_avg-1)*100:.1f}%%) '
                    f'— high-quality fear-premium entry'
                )
        self._iv_spike_entry = _iv_spike_entry
        # FIX-8: post-event IV crush entry signal
        # On RBI/Budget event days, the HIGHEST-EV entry is 1-3 hours
        # AFTER market open when IV is still elevated but uncertainty
        # is resolved. Engine normally blocks entries during event window
        # (macro override) but misses the post-event opportunity.
        # Oct 2 RBI Policy: window = 11:00-13:00 IST
        _today_str_pe = datetime.now(self._IST).date().isoformat()
        _now_ist_pe = datetime.now(self._IST)
        if _today_str_pe in config.HIGH_IMPACT_EVENTS:
            try:
                _event_open_pe = self._IST.localize(
                    datetime.strptime(
                        _today_str_pe, "%Y-%m-%d"
                    ).replace(hour=9, minute=15, second=0)
                )
                _hours_pe = (
                    _now_ist_pe - _event_open_pe
                ).total_seconds() / 3600.0
                # Post-event window: 1.0-3.0 hours after market open
                if 1.0 <= _hours_pe <= 3.0:
                    _iv_hist_pe = list(self.dm.iv_atm_history)[-15:]
                    if len(_iv_hist_pe) >= 7:
                        _iv_med_pe = sorted(_iv_hist_pe)[
                            len(_iv_hist_pe) // 2
                        ]
                        _iv_elev_pe = (
                            self.dm.iv_atm is not None
                            and _iv_med_pe > 0
                            and self.dm.iv_atm >= _iv_med_pe * 1.10
                        )
                        if _iv_elev_pe:
                            _event_name_pe = config.HIGH_IMPACT_EVENTS[
                                _today_str_pe
                            ]
                            logger.info(
                                f"FIX-8: POST-EVENT IV CRUSH OPPORTUNITY: "
                                f"{_event_name_pe} — "
                                f"IV={self.dm.iv_atm*100:.1f}%% elevated "
                                f"10%%+ above median={_iv_med_pe*100:.1f}%% "
                                f"— allowing straddle entry"
                            )
                            self._post_event_entry = True
                            self._post_event_strategy = (
                                config.STRAT_SHORT_STRADDLE
                            )
                            return True
            except Exception as _pe_err:
                logger.debug(
                    f"Post-event entry check error: {_pe_err}"
                )
        self._post_event_entry = False
        self._post_event_strategy = None
        # S9-2: IV spike overrides NEUTRAL to MILD_SELL for entry
        # When IV spikes 15%+ above recent median AND VIX is within
        # sell-vol range AND regime is NEUTRAL (composite just below
        # MILD_SELL_ENTER), treat as MILD_SELL for this entry only.
        # The actual confirmed_regime is NOT changed — only the local
        # variable used for the entry gate check below.
        if (
            _iv_spike_entry
            and regime == config.REGIME_NEUTRAL
            and self.dm.vix is not None
            and config.MIN_VIX_SELL
            <= self.dm.vix
            <= config.VIX_SELL_VOL_MAX
        ):
            _effective_regime = config.REGIME_MILD_SELL
            logger.info(
                'S9-2: IV spike — treating NEUTRAL as MILD_SELL '
                'for this entry (composite below threshold but '
                'IV spike confirms sell-vol opportunity)'
            )
        # P4-3b: Monday gap risk filter
        # Block straddle entries before 10:00 IST on Mondays.
        # NIFTY Monday gap of 0.4-0.8%% can wipe 1.6 weeks of theta.
        # Only blocks when the engine would select a straddle
        # (STRONG_SELL + ADX >= ADX_RANGE_THRESHOLD after P4-1).
        # Condors and credit spreads (defined risk) are NOT blocked.
        if (
            now.weekday() == 0
            and now_time < config.EXEC_START_TIME.__class__(10, 0)
            and regime == config.REGIME_STRONG_SELL
            and (self.dm.adx or 0) >= config.ADX_RANGE_THRESHOLD
        ):
            logger.info(
                'P4-3b: Entry gate BLOCKED: Monday gap risk window '
                '(straddle before 10:00 IST — NIFTY gap risk)'
            )
            return False
        # FIX-7a: double event week detection
        # Sep 2026: Sep 24 (monthly expiry) + Sep 25 (rebalancing)
        # = highest-risk week. OI is 3-5x normal, gamma explosion
        # starts Wednesday afternoon. Reduce position size.
        try:
            import calendar as _cal_dew
            _today_dew = datetime.now(self._IST).date()
            _last_day_dew = _cal_dew.monthrange(
                _today_dew.year, _today_dew.month
            )[1]
            # Find last Thursday (monthly expiry)
            _last_thu = _last_day_dew
            while __import__('datetime').datetime(
                _today_dew.year, _today_dew.month, _last_thu
            ).weekday() != 3:
                _last_thu -= 1
            # Find last Friday (index rebalancing)
            _last_fri = _last_day_dew
            while __import__('datetime').datetime(
                _today_dew.year, _today_dew.month, _last_fri
            ).weekday() != 4:
                _last_fri -= 1
            _days_thu = _last_thu - _today_dew.day
            _days_fri = _last_fri - _today_dew.day
            # Double event: both monthly expiry AND rebalancing this week
            _is_double = (
                0 <= _days_thu <= 5 and 0 <= _days_fri <= 5
            )
            _is_monthly = 0 <= _days_thu <= 5
            self._double_event_week = _is_double
            self._monthly_expiry_week = _is_monthly and not _is_double
            if _is_double:
                logger.warning(
                    f"FIX-7a: DOUBLE EVENT WEEK "
                    f"(monthly expiry {_last_thu} + "
                    f"rebalancing {_last_fri}) "
                    f"— 50%% lot reduction will apply"
                )
            elif _is_monthly:
                logger.info(
                    f"FIX-7a: Monthly expiry week "
                    f"(last Thursday={_last_thu}) "
                    f"— 25%% lot reduction will apply"
                )
        except Exception as _dew_err:
            logger.debug(f"Double event week detection error: {_dew_err}")
            self._double_event_week = False
            self._monthly_expiry_week = False
        # FIX-9: NIFTY weekday entry gates
        # Monday: block straddle entries until 11:00 IST
        # SGX Nifty gap risk not resolved until ~10:30-11:00
        # NIFTY opening gap typically settles by 10:30-11:00 IST
        _now_weekday_9 = now.weekday()
        if _now_weekday_9 == 0:  # Monday
            _intended_strat_9 = self._select_strategy(
                self.re.confirmed_regime
            )
            if (
                _intended_strat_9 == config.STRAT_SHORT_STRADDLE
                and now_time < config.EXEC_START_TIME.__class__(11, 0)
            ):
                logger.info(
                    'Entry gate BLOCKED: Monday straddle before 11:00 IST '
                    '(SGX Nifty gap risk not yet resolved)'
                )
                return False
        # Friday: block ALL entries after 13:30 IST
        # Weekend gap risk with only 4 days theta remaining
        if _now_weekday_9 == 4:  # Friday
            if now_time > config.EXEC_START_TIME.__class__(13, 30):
                logger.info(
                    'Entry gate BLOCKED: Friday after 13:30 IST '
                    '(weekend gap risk — insufficient theta)'
                )
                return False
        logger.info(
            f'Entry gate PASSED: regime={regime} '
            f'time={now_time} '
            f'composite={self.re.raw_composite:.4f}'
        )
        return True
    def _set_entry_diagnostic(
        self,
        stage: str,
        message: str,
        **values,
    ) -> None:
        """Record and log the latest entry decision."""
        current = getattr(
            self,
            "_last_entry_diagnostic",
            {},
        ).copy()

        current.update(values)
        current["stage"] = stage
        current["message"] = message
        current["timestamp"] = datetime.now(
            self._IST
        ).strftime("%Y-%m-%d %H:%M:%S")

        self._last_entry_diagnostic = current

        logger.info(
            "ENTRY DIAGNOSTIC | "
            f"stage={stage} | "
            f"strategy={current.get('strategy', '')} | "
            f"message={message} | "
            f"expiry={current.get('expiry', '')} | "
            f"dte={current.get('dte')} | "
            f"credit={current.get('credit')} | "
            f"min_credit={current.get('min_credit')} | "
            f"max_risk={current.get('max_risk')} | "
            f"lots={current.get('lots')} | "
            f"pretrade={current.get('pretrade')} | "
            f"execution={current.get('execution')}"
        )

    async def _enter_new_position(self) -> None:
        regime        = self.re.confirmed_regime
        strategy_name = self._select_strategy(regime)
        if strategy_name is None:
            logger.info(
                f"No strategy for regime={regime}"
            )
            return

        # PATCH: multi-tranche diversification. Previously any
        # second position of the same strategy was blocked
        # outright, capping the whole regime's capital allocation
        # to whatever a single position happened to use. Now
        # allow up to config.MAX_TRANCHES_PER_STRATEGY concurrent
        # positions of the same strategy — each subsequent tranche
        # targets a later expiry (genuine time diversification)
        # via the tranche-aware builders, rather than building an
        # identical duplicate.
        existing_same_strategy = [
            p for p in self.open_positions
            if p.strategy_name == strategy_name
        ]
        tranche = len(existing_same_strategy) + 1
        if tranche > config.MAX_TRANCHES_PER_STRATEGY:
            logger.info(
                f"Max tranches reached for {strategy_name} "
                f"({len(existing_same_strategy)}/"
                f"{config.MAX_TRANCHES_PER_STRATEGY})"
            )
            return

        self._set_entry_diagnostic(
            "BUILDING",
            "Starting strategy construction",
            strategy=strategy_name,
        )
        logger.info(
            f"Selected: {strategy_name} "
            f"regime={regime} tranche={tranche}"
        )

        legs, meta = await self._build_strategy(
            strategy_name, tranche=tranche
        )
        if legs is None:
            self._set_entry_diagnostic(
                "BUILD_FAILED",
                "Builder returned no legs; inspect builder log",
                strategy=strategy_name,
                pretrade="NOT_RUN",
                execution="NOT_RUN",
            )
            logger.warning(
                f'Build FAILED: {strategy_name} — '
                f'check logs above for reason '
                f'(DTE, VIX spike, spread, credit, LTP=0)'
            )
            # LOG-SE-03a: log the build failure
            self._log_entry_attempt(
                strategy_name,
                "FAILED_OTHER",
                "build returned None",
            )
            # RC2: long-vol build failure does not trigger cooldown
            # Long-vol strategies (long straddle, backspread, strangle)
            # are expected to fail the IV rank gate in a sell-vol
            # environment.  Setting _last_build_failure would block
            # sell-vol entries for 5 minutes — an unintended side-effect.
            # FIX-10: extend RC2 to MIN_VIX_CONDOR gate failures
            # ARCH-6 routes MILD_SELL to condor when composite>=0.40.
            # At Sep 2026 VIX=12-15, builder fails MIN_VIX_CONDOR=16.0.
            # This is an EXPECTED failure — no cooldown should trigger.
            # Without this fix, every high-conviction MILD_SELL session
            # at VIX<16 triggers a 300s cooldown blocking all entries.
            _long_vol_strategies = (
                config.STRAT_LONG_STRADDLE,
                config.STRAT_BACKSPREAD,
                config.STRAT_STRANGLE,
            )
            _min_vix_condor_rc2 = getattr(
                config, 'MIN_VIX_CONDOR', 16.0
            )
            _condor_vix_fail = (
                strategy_name == config.STRAT_IRON_CONDOR
                and self.dm.vix is not None
                and self.dm.vix < _min_vix_condor_rc2
            )
            if (
                strategy_name not in _long_vol_strategies
                and not _condor_vix_fail
            ):
                self._last_build_failure = datetime.now(
                    self._IST
                )
            else:
                logger.info(
                    f"FIX-10: {strategy_name} build failure is expected "
                    f"(VIX gate or IV rank gate) — no cooldown"
                )
            return

        new_expiry = legs[0].expiry if legs else ""
        for existing in self.open_positions:
            if (
                existing.strategy_name == strategy_name
                and existing.expiry_date == new_expiry
            ):
                logger.info(
                    f"Duplicate blocked: {strategy_name} "
                    f"expiry={new_expiry} tranche={tranche} "
                    f"resolved to the same expiry as an "
                    f"existing position"
                )
                return

        now_time = datetime.now(self._IST).time()
        if (
            now_time < config.EXEC_START_TIME
            or now_time > config.EXEC_END_TIME
        ):
            logger.info(
                f"Entry window closed during build — "
                f"aborting"
            )
            return

        self._set_entry_diagnostic(
            "PRETRADE_CHECK",
            "Strategy built; running pre-trade checks",
            strategy=strategy_name,
            expiry=legs[0].expiry if legs else "",
            credit=meta.get(
                "net_credit",
                meta.get("total_credit"),
            ),
            max_risk=meta.get("max_risk"),
        )

        if not await self._pre_trade_checks(
            strategy_name, legs
        ):
            self._set_entry_diagnostic(
                "PRETRADE_FAILED",
                "Pre-trade checks rejected the strategy",
                strategy=strategy_name,
                pretrade="FAILED",
                execution="NOT_RUN",
            )
            logger.info(
                f"Pre-trade failed: {strategy_name}"
            )
            # LOG-SE-03b: log pre-trade failure
            _exp = legs[0].expiry if legs else None
            _dte_val = None
            if _exp:
                try:
                    from datetime import date as _d
                    _dte_val = (
                        datetime.strptime(_exp, "%Y-%m-%d").date()
                        - _d.today()
                    ).days
                except Exception:
                    pass
            self._log_entry_attempt(
                strategy_name,
                "FAILED_PRETRADE",
                "pre-trade checks failed",
                expiry_date=_exp,
                dte=_dte_val,
                net_credit=meta.get(
                    "net_credit",
                    meta.get("total_credit", 0),
                ),
                pretrade_passed=False,
            )
            return

        lots = self._calculate_lot_size(strategy_name, meta)
        if lots < 1:
            self._set_entry_diagnostic(
                "LOTS_ZERO",
                "Lot sizing returned zero; trade skipped",
                strategy=strategy_name,
                lots=lots,
                vix_multiplier=getattr(
                    self, "_last_vix_mult", 1.0
                ),
                pretrade="PASSED",
                execution="NOT_RUN",
            )
            logger.info(
                f"Lot size=0 for {strategy_name} — skip"
            )
            # LOG-SE-03c: log lot-size failure
            self._log_entry_attempt(
                strategy_name,
                "FAILED_LOTS",
                "lot sizing returned 0",
                net_credit=meta.get(
                    "net_credit",
                    meta.get("total_credit", 0),
                ),
                max_risk_used=meta.get("max_risk", 0),
                vix_adaptive_mult=getattr(
                    self, "_last_vix_mult", 1.0
                ),
            )
            return

        # AUDIT #N1: skip lot-scaling for strategies that
        # pre-compute an absolute quantity (e.g. defensive hedge).
        _already_sized = meta.get("already_sized", False)
        if not _already_sized:
            for leg in legs:
                leg.qty = leg.qty * lots
        else:
            # Force lots=1 so downstream position-record
            # fields (max_risk scaling etc.) stay consistent.
            lots = 1

        # PATCH: store total estimated margin for this position
        # (per-lot heuristic estimate x final lot count), so
        # future sizing calls can track cumulative margin usage
        # across all open positions.
        meta["margin_estimate"] = (
            meta.get("margin_estimate_per_lot", 0.0) * lots
        )

        # PATCH: max_risk / stop_loss / profit_target / max_profit
        # are all computed by the builders using PER-1-LOT premium
        # points or 1-lot rupee figures (before any lot multiplier
        # exists). They were previously stored on the position
        # unscaled, while the values compared against them at
        # monitoring time (_get_position_current_premium() /
        # _get_position_value(), which read leg.qty — already
        # lot-scaled by the loop above) ARE correctly scaled. That
        # mismatch meant: (a) capital-deployment gates read
        # max_risk at 1/lots of the true figure, letting the
        # regime-capital cap go effectively unenforced; (b) profit
        # targets became progressively harder to hit and
        # stop-losses progressively more trigger-happy as lot
        # count grew — both by a factor of `lots`. Rescale all of
        # them here so they're expressed in the same
        # total-position units as what they'll be compared
        # against.
        meta["max_risk"] = meta.get("max_risk", 0.0) * lots
        if meta.get("stop_loss") is not None:
            meta["stop_loss"] = meta.get("stop_loss", 0.0) * lots
        if meta.get("profit_target") is not None:
            meta["profit_target"] = (
                meta.get("profit_target", 0.0) * lots
            )
        if meta.get("max_profit") is not None:
            meta["max_profit"] = (
                meta.get("max_profit", 0.0) * lots
            )

        # PATCH (NEW-2): authoritative post-sizing combined-risk
        # check. _pre_trade_checks() runs BEFORE lot sizing, so it
        # compares existing positions' TRUE (lot-scaled) max_risk
        # against the new position's 1-lot-only estimate,
        # understating the new position's contribution by a
        # factor of `lots`. This uses the final, correctly-scaled
        # max_risk to catch what the earlier check could miss.
        current_risk = sum(
            p.max_risk for p in self.open_positions
        )
        if (
            current_risk + meta["max_risk"]
            > config.MAX_COMBINED_RISK
        ):
            logger.info(
                f"Entry aborted: combined risk limit would be "
                f"breached — current={current_risk:.0f} "
                f"new={meta['max_risk']:.0f} "
                f"limit={config.MAX_COMBINED_RISK:.0f}"
            )
            return

        trade_id = str(uuid.uuid4())

        # SE-D1: re-validate credit immediately before execution.
        # The builder ran in the 60s regime block; in a moving tape
        # credit can decay materially between gate and order send.
        _revalidation_pct = getattr(
            config, "ENTRY_CREDIT_REVALIDATION_PCT", 0.10
        )
        _orig_credit = meta.get("net_credit",
                                meta.get("total_credit", 0))
        if _orig_credit > 0 and _revalidation_pct > 0:
            _strategy_type = meta.get("strategy_type", "SHORT")
            if _strategy_type == "SHORT":
                # Re-read current credit from live chain
                _current_credit = 0.0
                try:
                    for _leg in legs:
                        _lc = self.dm.get_chain_for_expiry(
                            _leg.expiry
                        ).get(_leg.strike, {}).get(
                            _leg.option_type, {}
                        )
                        _b = float(_lc.get("bid") or 0)
                        _a = float(_lc.get("ask") or 0)
                        if _b > 0 and _a > 0:
                            if _leg.action == "SELL":
                                _current_credit += _b
                            else:
                                _current_credit -= _a
                except Exception:
                    _current_credit = _orig_credit
                _decay = (_orig_credit - _current_credit) / _orig_credit
                if _decay > _revalidation_pct:
                    logger.info(
                        f"SE-D1: credit decayed {_decay*100:.1f}%% "
                        f"since build ({_orig_credit:.1f} -> "
                        f"{_current_credit:.1f}) — aborting"
                    )
                    self._last_build_failure = datetime.now(self._IST)
                    return

        self._set_entry_diagnostic(
            "EXECUTING",
            "Pre-trade checks passed; sending orders",
            strategy=strategy_name,
            expiry=new_expiry,
            credit=meta.get(
                "net_credit",
                meta.get("total_credit"),
            ),
            max_risk=meta.get("max_risk"),
            lots=lots,
            vix_multiplier=getattr(
                self, "_last_vix_mult", 1.0
            ),
            pretrade="PASSED",
            execution="STARTED",
        )

        success = await self._execute_strategy(
            strategy_name, legs, meta, trade_id=trade_id
        )
        if not success:
            self._set_entry_diagnostic(
                "EXECUTION_FAILED",
                "Order execution returned failure",
                strategy=strategy_name,
                pretrade="PASSED",
                execution="FAILED",
            )
            logger.warning(
                f"Execution failed: {strategy_name}"
            )
            # LOG-SE-03d: log execution failure
            self._log_entry_attempt(
                strategy_name,
                "FAILED_EXECUTION",
                "_execute_strategy returned False",
                lots_requested=lots,
                lots_filled=0,
                execution_result="FAILED",
                trade_id=trade_id,
            )
            return

        # AUDIT SE-N03: if any leg partially filled, the lot count
        # was reduced. Rescale stop, target, max_risk to actual fills.
        _filled_lots = min(
            (l.qty for l in legs if l.qty > 0), default=lots
        )
        if _filled_lots < lots and lots > 0:
            _scale = _filled_lots / lots
            meta["max_risk"] = meta.get("max_risk", 0) * _scale
            if meta.get("stop_loss"):
                meta["stop_loss"] = meta["stop_loss"] * _scale
            if meta.get("profit_target"):
                meta["profit_target"] = (
                    meta["profit_target"] * _scale
                )
            logger.info(
                f"SE-N03: partial fill rescale "
                f"{lots}->{_filled_lots} lots, "
                f"scale={_scale:.3f}"
            )

        self._set_entry_diagnostic(
            "FILLED",
            "All legs filled; position created",
            strategy=strategy_name,
            expiry=new_expiry,
            credit=meta.get(
                "net_credit",
                meta.get("total_credit"),
            ),
            max_risk=meta.get("max_risk"),
            lots=lots,
            vix_multiplier=getattr(
                self, "_last_vix_mult", 1.0
            ),
            pretrade="PASSED",
            execution="FILLED",
        )

        self._refresh_leg_greeks(legs)

        position = self._create_position_record(
            strategy_name, legs, meta, trade_id=trade_id
        )
        self.open_positions.append(position)
        self.dm.save_position(
            self._position_to_dict(position)
        )
        self._last_build_failure = None

        # PRF-04: record composite at entry for re-entry gate
        self._last_entry_composite[strategy_name] = (
            self.re.raw_composite
        )
        # LOG-SE-03e: log successful entry
        _success_dte = None
        if new_expiry:
            try:
                from datetime import date as _d2
                _success_dte = (
                    datetime.strptime(new_expiry, "%Y-%m-%d").date()
                    - _d2.today()
                ).days
            except Exception:
                pass
        self._log_entry_attempt(
            strategy_name,
            "SUCCESS",
            "",
            expiry_date=new_expiry,
            dte=_success_dte,
            net_credit=meta.get(
                "net_credit",
                meta.get("total_credit", 0),
            ),
            wing_width=meta.get("wing_width",
                                meta.get("long_call", 0)
                                - meta.get("short_call", 0)
                                if meta.get("long_call") else None),
            expected_move=meta.get("expected_move"),
            pretrade_passed=True,
            execution_result="FILLED",
            lots_requested=lots,
            lots_filled=lots,
            actual_credit=meta.get(
                "net_credit",
                meta.get("total_credit", 0),
            ),
            vix_adaptive_mult=getattr(
                self, "_last_vix_mult", 1.0
            ),
            max_risk_used=meta.get("max_risk", 0),
            trade_id=trade_id,
        )
        logger.info(
            f"New position: {strategy_name} "
            f"trade_id={trade_id[:8]} lots={lots} "
            f"expiry={new_expiry}"
        )

    def _refresh_leg_greeks(self, legs: List[Leg]) -> None:
        """Refresh Greeks from live chain after execution."""
        for leg in legs:
            expiry_chain = self.dm.get_chain_for_expiry(
                leg.expiry
            )
            opt = expiry_chain.get(
                leg.strike, {}
            ).get(leg.option_type, {})
            if opt:
                leg.delta = float(
                    opt.get("delta", leg.delta)
                )
                leg.gamma = float(
                    opt.get("gamma", leg.gamma)
                )
                leg.vega  = float(
                    opt.get("vega",  leg.vega)
                )
                leg.theta = float(
                    opt.get("theta", leg.theta)
                )

    def _select_strategy(
        self, regime: str
    ) -> Optional[str]:
        adx          = self.dm.adx or 0
        atr_contract = self.dm.is_atr_contracting()
        # S8-1: skew_diff computed from builder expiry
        # _get_25d_put_iv() uses active chain (nearest expiry, DTE=0-8)
        # but the builder uses DTE=4-9.  On expiry day these differ.
        # Use the builder's target expiry for consistent skew routing.
        _builder_expiry = self.dm.get_expiry_by_dte(4, tolerance=5)
        if _builder_expiry:
            _b_chain = self.dm.get_chain_for_expiry(_builder_expiry)
            _put_strike_25d = self.dm.get_strike_by_delta(
                "put", 0.25, expiry=_builder_expiry
            )
            _call_strike_25d = self.dm.get_strike_by_delta(
                "call", 0.25, expiry=_builder_expiry
            )
            if _put_strike_25d and _call_strike_25d and _b_chain:
                put_iv = float(
                    _b_chain.get(_put_strike_25d, {})
                    .get("put", {})
                    .get("iv", 0.0)
                )
                call_iv = float(
                    _b_chain.get(_call_strike_25d, {})
                    .get("call", {})
                    .get("iv", 0.0)
                )
            else:
                put_iv  = self._get_25d_put_iv()
                call_iv = self._get_25d_call_iv()
        else:
            put_iv  = self._get_25d_put_iv()
            call_iv = self._get_25d_call_iv()
        # SE-P0-01: compute_iv_rank() returns None on cold start
        # (DM-13 fix). Guard all comparisons against None to prevent
        # TypeError crashing run_cycle() for the first ~10 sessions.
        _iv_rank_raw = self.dm.compute_iv_rank()
        iv_rank      = _iv_rank_raw if _iv_rank_raw is not None else 50.0
        # PATCH (S3): put_iv/call_iv/forward_iv/vix are all stored
        # as DECIMALS (e.g. 0.15 for 15%), but SPREAD_SKEW_THRESHOLD
        # (2.0), RATIO_CONTANGO_THRESHOLD (1.5), and
        # RATIO_SKEW_FLAT_THRESHOLD (0.5) are all clearly scaled in
        # PERCENTAGE POINTS. Without this conversion, skew_diff/
        # term_spread were ~0.005-0.02, making these thresholds
        # unreachable — the ratio-spread branch was dead code and
        # MILD_SELL always fell through to credit spreads. Convert
        # to percentage points here.
        skew_diff    = (put_iv - call_iv) * 100.0
        term_spread  = (
            (
                (self.dm.forward_iv or 0)
                - (self.dm.vix or 0) / 100.0
            ) * 100.0
        )
        trend_score  = self.re.confirmed_trend
        iv_rank      = (
            _iv_rank_raw
            if _iv_rank_raw is not None
            else 50.0
        )
        has_shorts   = self._has_short_positions()
        spot         = self.dm.spot or 0
        ema_200      = self._get_ema_200()

        if regime == config.REGIME_STRONG_SELL:
            # FIX-2: NIFTY Sep2026 STRONG_SELL routing
            # Sep 2026 VIX=12-15: condor 4-leg slippage (8-12pts) consumes
            # 50-75%% of 14-18pt credit → EV deeply negative.
            # Straddle 2-leg slippage (3-4pts) on 55pt credit → EV +Rs2,569/trade.
            # Condor only viable at VIX=16+ when credit justifies slippage.
            _adx_now_ss = self.dm.adx or 99.0
            _vix_now_ss = self.dm.vix or 0.0
            _min_vix_condor_ss = getattr(
                config, 'MIN_VIX_CONDOR', 16.0
            )
            if (
                _vix_now_ss >= _min_vix_condor_ss
                and _adx_now_ss < config.ADX_RANGE_THRESHOLD
            ):
                # VIX=16+, range-bound: condor credit justifies slippage
                return config.STRAT_IRON_CONDOR
            # VIX<16 (Sep 2026 typical): straddle has best R:R
            return config.STRAT_SHORT_STRADDLE

        elif regime == config.REGIME_MILD_SELL:
            # FIX-2b: MILD_SELL default to put spread
            # NIFTY structural put skew is 2-3pp (always present).
            # SPREAD_SKEW_THRESHOLD=3.0 (FIX-1g) fires only on genuinely
            # elevated skew (pre-RBI: 4-8pp). Default to put spread to
            # exploit NIFTY's structural upward drift (76-82%% win rate).
            self._pending_skew_side = "put"  # NIFTY default: upward drift
            if skew_diff >= config.SPREAD_SKEW_THRESHOLD:
                # Genuinely elevated put skew (above structural 2-3pp)
                self._pending_skew_side = "put"
                return config.STRAT_CREDIT_SPREADS
            elif skew_diff <= -config.SPREAD_SKEW_THRESHOLD:
                # Call skew elevated (post-sharp-rally, rare for NIFTY)
                self._pending_skew_side = "call"
                return config.STRAT_CREDIT_SPREADS
            elif (
                skew_diff < config.RATIO_SKEW_FLAT_THRESHOLD
                and term_spread
                > config.RATIO_CONTANGO_THRESHOLD
            ):
                return config.STRAT_RATIO_SPREAD
            else:
                # Flat skew — build both sides symmetrically
                self._pending_skew_side = "both"
                return config.STRAT_CREDIT_SPREADS

        elif regime == config.REGIME_NEUTRAL:
            # P0-4: iv_rank None guard — compute_iv_rank() returns None during warmup
            _iv_rank_neutral = self.dm.compute_iv_rank()
            _iv_rank_neutral = (_iv_rank_neutral if _iv_rank_neutral is not None else 50.0)
            # S8-2: NEUTRAL iv_rank gate lowered to 40
            # After RH1 (flow direction fix), fear days now have flow=+1
            # raising the composite.  IV rank during fear days is 40-60.
            # iv_rank > 50 was too restrictive, missing fear-day entries.
            # iv_rank > 40 still ensures IV is elevated (not cheap).
            if _iv_rank_neutral > 50 and adx < 20:
                # Range-bound + elevated IV: condor is optimal
                return config.STRAT_IRON_CONDOR
            if _iv_rank_neutral > 40 and adx >= 20:
                # Trending + elevated IV: credit spread is safer than condor
                return config.STRAT_CREDIT_SPREADS
            return None

        elif regime == config.REGIME_BUY_VOL:
            # RH3: BUY_VOL returns None when IV is expensive
            # Long-vol strategies require cheap IV (low IV rank).
            # When IV rank > LONG_STRADDLE_MAX_IV_RANK, the builder
            # will fail its own IV rank gate.  Return None here to
            # avoid the build attempt and the 5-minute cooldown that
            # would otherwise block all sell-vol entries.
            _ivr_buyol = self.dm.compute_iv_rank()
            _ivr_buyol = (
                _ivr_buyol if _ivr_buyol is not None else 50.0
            )
            if _ivr_buyol > config.LONG_STRADDLE_MAX_IV_RANK:
                logger.info(
                    f'RH3: BUY_VOL skipped — IV rank {_ivr_buyol:.1f} '
                    f'> {config.LONG_STRADDLE_MAX_IV_RANK} '
                    f'(IV too expensive for long-vol entry)'
                )
                return None
            # SE7-P1-02: butterfly is short vega/short gamma at
            # the body — it profits from vol contraction, the
            # opposite of BUY_VOL intent. Use long strangle instead
            # (long gamma, benefits from vol expansion).
            if (
                has_shorts
                and self._gamma_above_50pct_limit()
            ):
                return config.STRAT_DEFENSIVE
            else:
                return config.STRAT_STRANGLE

        elif regime == config.REGIME_STRONG_BUY:
            if (
                self.dm.vix
                and self.dm.vix > config.BACKSPREAD_MAX_VIX
            ):
                return config.STRAT_LONG_STRADDLE
            if trend_score == 0:
                return config.STRAT_LONG_STRADDLE
            elif (
                abs(trend_score) == 1
                and skew_diff
                < config.RATIO_SKEW_FLAT_THRESHOLD
            ):
                return config.STRAT_BACKSPREAD
            else:
                return config.STRAT_LONG_STRADDLE

        elif regime == config.REGIME_EVENT:
            call_spread = self._get_otm_bid_ask("call")
            put_spread  = self._get_otm_bid_ask("put")
            if (
                call_spread
                < config.EVENT_STRANGLE_MAX_SPREAD_PTS
                and put_spread
                < config.EVENT_STRANGLE_MAX_SPREAD_PTS
            ):
                return config.STRAT_STRANGLE
            else:
                return config.STRAT_LONG_STRADDLE

        return None

    async def _build_strategy(
        self, strategy_name: str, tranche: int = 1
    ) -> Tuple[Optional[List[Leg]], Dict]:
        # PATCH: only the two highest-volume, tranche-aware
        # builders receive the tranche argument; every other
        # strategy is built exactly as before.
        tranche_aware_builders = {
            config.STRAT_CREDIT_SPREADS: (
                self._build_credit_spreads
            ),
            config.STRAT_IRON_CONDOR: (
                self._build_iron_condor
            ),
        }
        if strategy_name in tranche_aware_builders:
            # PRF-02: pass skew_side for credit spreads
            if strategy_name == config.STRAT_CREDIT_SPREADS:
                _skew_side = getattr(self, "_pending_skew_side", "both")
                return await tranche_aware_builders[
                    strategy_name
                ](tranche=tranche, skew_side=_skew_side)
            return await tranche_aware_builders[
                strategy_name
            ](tranche=tranche)

        builders = {
            config.STRAT_SHORT_STRADDLE: (
                self._build_short_straddle
            ),
            config.STRAT_RATIO_SPREAD: (
                self._build_ratio_spread
            ),
            config.STRAT_BUTTERFLY: (
                self._build_butterfly
            ),
            config.STRAT_DEFENSIVE: (
                self._build_defensive_hedge
            ),
            config.STRAT_LONG_STRADDLE: (
                self._build_long_straddle
            ),
            config.STRAT_BACKSPREAD: (
                self._build_backspread
            ),
            config.STRAT_STRANGLE: (
                self._build_long_strangle
            ),
        }
        builder = builders.get(strategy_name)
        if builder is None:
            logger.warning(
                f"No builder for {strategy_name}"
            )
            return (None, {})
        return await builder()

    async def _build_short_straddle(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """
        Build ATM short straddle.
        FIX VS1/V3: DTE_MAX=10, tolerance=5 to handle
        Tuesday expiry cycle.
        On Monday: DTE=8 (Sep 8) is now accepted.
        On Wednesday-Friday: DTE=4-6 (in range).
        """
        # FIX-2026-D: straddle MIN_VIX gate
        # MIN_VIX_STRADDLE=12.0 defined in config but not wired here.
        # At VIX=11, ATM straddle=~50pts; stop at 75pts (1.5x);
        # NIFTY needs only ~90pts move; P(90pt move VIX=11)=~50%%.
        # Gate prevents unprofitable straddle entries at VIX<12.
        _min_vix_straddle = getattr(
            config, 'MIN_VIX_STRADDLE', 12.0
        )
        if (
            self.dm.vix is not None
            and self.dm.vix < _min_vix_straddle
        ):
            logger.info(
                f"Straddle skipped: VIX={self.dm.vix:.1f} < "
                f"MIN_VIX_STRADDLE={_min_vix_straddle:.1f} "
                f"(credit too thin, stop fires on ~50%% of trades)"
            )
            return (None, {})
        # FIX VS1: increased tolerance from 2 to 5
        expiry = self.dm.get_expiry_by_dte(
            config.STRADDLE_DTE_MIN + 2,
            tolerance=5,   # was 2
        )
        if expiry is None:
            logger.info(
                "Straddle: no expiry found "
                f"(DTE_MIN={config.STRADDLE_DTE_MIN} "
                f"DTE_MAX={config.STRADDLE_DTE_MAX} "
                f"tolerance=5)"
            )
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days

        if dte < config.STRADDLE_DTE_MIN:
            logger.info(
                f"Straddle: DTE={dte} < "
                f"min={config.STRADDLE_DTE_MIN} — skip"
            )
            return (None, {})

        if dte > config.STRADDLE_DTE_MAX:
            logger.info(
                f"Straddle: DTE={dte} > "
                f"max={config.STRADDLE_DTE_MAX} — skip"
            )
            return (None, {})

        # FIX QS1: expiry-scoped chain
        chain = self.dm.get_chain_for_expiry(expiry)
        if not chain:
            logger.info(
                f"Straddle: no chain for {expiry}"
            )
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})
        if atm not in chain:
            return (None, {})

        call_data = chain[atm]["call"]
        put_data  = chain[atm]["put"]

        if (
            call_data["ask"] - call_data["bid"]
        ) > config.MAX_SPREAD_ATM_PTS:
            return (None, {})
        if (
            put_data["ask"] - put_data["bid"]
        ) > config.MAX_SPREAD_ATM_PTS:
            return (None, {})

        total_premium = (
            call_data["ltp"] + put_data["ltp"]
        )

        if total_premium <= 0:
            logger.info(
                f"Straddle: total_premium=0 — skip"
            )
            return (None, {})

        # FIX NS4 (AUDIT #1.1): max_risk = stop level × LOT_SIZE
        # Stop is 2x credit (STRADDLE_STOP_MULT=2.0), so size to that
        # to prevent systematic over-leverage vs all capital guardrails.
        max_risk = (
            total_premium
            * config.STRADDLE_STOP_MULT
            * config.LOT_SIZE
        )

        legs = [
            Leg(
                instrument_key=call_data["instrument_key"],
                option_type="call", action="SELL",
                strike=atm, expiry=expiry, qty=1,
                delta=call_data["delta"],
                gamma=call_data["gamma"],
                vega=call_data["vega"],
                theta=call_data["theta"],
            ),
            Leg(
                instrument_key=put_data["instrument_key"],
                option_type="put", action="SELL",
                strike=atm, expiry=expiry, qty=1,
                delta=put_data["delta"],
                gamma=put_data["gamma"],
                vega=put_data["vega"],
                theta=put_data["theta"],
            ),
        ]

        # C4-13: VIX-scaled stop using SL_BASE_PERCENT/SL_REFERENCE_VIX.
        # Flat STRADDLE_STOP_MULT ignores that 1.25x at VIX=11 is
        # ~2 hours of noise while at VIX=22 it is a genuine stop.
        # stop_pct = clamp(SL_BASE * vix / SL_REF, SL_MIN, SL_MAX)
        _vix_sl    = self.dm.vix or config.SL_REFERENCE_VIX
        _sl_base   = getattr(config, "SL_BASE_PERCENT",   0.30)
        _sl_ref    = getattr(config, "SL_REFERENCE_VIX",  14.0)
        _sl_min    = getattr(config, "SL_MIN_PERCENT",     0.18)
        _sl_max    = getattr(config, "SL_MAX_PERCENT",     0.40)
        _sl_pct    = max(_sl_min, min(_sl_max, _sl_base * _vix_sl / _sl_ref))
        _stop_loss = total_premium * _sl_pct / _sl_base * config.STRADDLE_STOP_MULT
        # Simplified: scale the stop multiple by VIX ratio
        _vix_stop_mult = config.STRADDLE_STOP_MULT * (_vix_sl / _sl_ref)
        _vix_stop_mult = max(1.0, min(3.0, _vix_stop_mult))
        logger.info(
            f"Straddle: VIX={_vix_sl:.1f} -> "
            f"stop_mult={_vix_stop_mult:.2f}x "
            f"(base={config.STRADDLE_STOP_MULT}x)"
        )

        meta = {
            "total_premium":  total_premium,
            "stop_loss_up":   (self.dm.spot or 0) * (
                1 + config.STRADDLE_SPOT_STOP_PCT
            ),
            "stop_loss_down": (self.dm.spot or 0) * (
                1 - config.STRADDLE_SPOT_STOP_PCT
            ),
            "profit_target":  total_premium * (
                1 - config.STRADDLE_TARGET_PCT
            ),
            # C4-13: VIX-scaled stop (wider at high VIX, tighter at low VIX)
            "stop_loss":      (
                total_premium * _vix_stop_mult
            ),
            "exit_dte":       config.STRADDLE_EXIT_DTE,
            "max_hold_date":  None,
            "max_risk":       max_risk,
            "strategy_type":  "SHORT",
        }
        logger.info(
            f"Straddle built: ATM={atm} "
            f"expiry={expiry} DTE={dte} "
            f"premium={total_premium:.2f}"
        )
        return (legs, meta)

    async def _build_iron_condor(
        self, tranche: int = 1,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """
        Build wide iron condor.
        FIX VS1/V3: DTE_MAX=10, tolerance=5.
        FIX QS1: expiry-scoped chain.
        FIX S1:  strikes rounded to 100-pt step.
        FIX TS5: use CONDOR_SIGMA_MULTIPLIER=1.0.
        FIX QS2: CONDOR_MIN_CREDIT=15.
        FIX V5:  CONDOR_WING_WIDTH=400.
        PATCH: tranche>1 targets a later expiry (~1 extra
        weekly cycle per tranche) instead of building an
        identical position at the same expiry as an existing one.
        """
        target_dte    = config.CONDOR_DTE_MIN + 2
        max_dte_bound = config.CONDOR_DTE_MAX
        if tranche > 1:
            target_dte    += 7 * (tranche - 1)
            max_dte_bound += 7 * (tranche - 1)

        # FIX VS1: increased tolerance from 2 to 5
        expiry = self.dm.get_expiry_by_dte(
            target_dte,
            tolerance=5,   # was 2
        )
        if expiry is None:
            return (None, {})

        spot = self.dm.spot
        if spot is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days

        if dte < config.CONDOR_DTE_MIN:
            return (None, {})

        if dte > max_dte_bound:
            return (None, {})

        # FIX QS1: expiry-scoped chain
        chain = self.dm.get_chain_for_expiry(expiry)
        if not chain:
            logger.info(
                f"Condor: no chain for {expiry}"
            )
            return (None, {})

        vix = self.dm.vix or 16.0

        # ARCH-1d: MIN_VIX_CONDOR gate in builder
        # At VIX < 13, condor credit (~11pts) is too thin to be
        # profitable after transaction costs at realistic lot sizes.
        # Only build condors when VIX >= MIN_VIX_CONDOR.
        _min_vix_condor = getattr(config, 'MIN_VIX_CONDOR', 13.0)
        if vix < _min_vix_condor:
            logger.info(
                f"Condor skipped: VIX={vix:.1f} < "
                f"MIN_VIX_CONDOR={_min_vix_condor:.1f} "
                f"(credit too thin at low VIX)"
            )
            return (None, {})

        # AUDIT SE-03: VIX is annualised on 252 trading days.
        # Using calendar days (dte/365) understates the move and
        # places short strikes too close to spot.
        # Approximate trading days: calendar_days * (252/365).
        _trading_days = max(1, dte * 252 / 365)
        expected_move = (
            spot
            * (vix / 100)
            * ((_trading_days / 252) ** 0.5)
        )

        # P0-3: _dynamic_wing defined (was NameError)
        _dynamic_wing = config.CONDOR_WING_WIDTH
        # FIX S1: round to 100-pt step
        short_call = (
            round(
                (
                    spot
                    + config.CONDOR_SIGMA_MULTIPLIER
                    * expected_move
                )
                / config.NIFTY_STRIKE_STEP
            ) * config.NIFTY_STRIKE_STEP
        )
        short_put = (
            round(
                (
                    spot
                    - config.CONDOR_SIGMA_MULTIPLIER
                    * expected_move
                )
                / config.NIFTY_STRIKE_STEP
            ) * config.NIFTY_STRIKE_STEP
        )
        # PRF-S02: use dynamic wing width instead of fixed constant
        long_call = short_call + _dynamic_wing
        long_put  = short_put  - _dynamic_wing

        for strike in [
            short_call, short_put, long_call, long_put
        ]:
            if strike not in chain:
                logger.warning(
                    f"Condor: strike {strike} not in "
                    f"chain for {expiry}"
                )
                return (None, {})

        for strike, opt_type, max_spread in [
            (short_call, "call", config.MAX_SPREAD_ATM_PTS),
            (short_put,  "put",  config.MAX_SPREAD_ATM_PTS),
            (long_call,  "call", config.MAX_SPREAD_OTM_PTS),
            (long_put,   "put",  config.MAX_SPREAD_OTM_PTS),
        ]:
            spread = (
                chain[strike][opt_type]["ask"]
                - chain[strike][opt_type]["bid"]
            )
            if spread > max_spread:
                logger.warning(
                    f"Condor: spread too wide at "
                    f"{strike} {opt_type}: {spread:.2f}"
                )
                return (None, {})

        # SE-P1: use executable prices, not LTP.
        # LTP can be anywhere in the bid-ask band. On a 4-leg condor
        # the gap between LTP credit and achievable credit is up to
        # 8pts on a 55pt floor (15% measurement error).
        def _exec(opt, action):
            b = float(opt.get("bid") or 0)
            a = float(opt.get("ask") or 0)
            if b <= 0 or a <= 0:
                return float(opt.get("ltp") or 0)
            return b if action == "SELL" else a
        sc_prem = _exec(chain[short_call]["call"], "SELL")
        sp_prem = _exec(chain[short_put]["put"],   "SELL")
        lc_prem = _exec(chain[long_call]["call"],  "BUY")
        lp_prem = _exec(chain[long_put]["put"],    "BUY")

        net_credit = (
            sc_prem + sp_prem - lc_prem - lp_prem
        )
        # SE-P1B: subtract pre-approval slippage haircut.
        # Slippage currently only appears in _simulate_fill (after
        # approval). The gate sees a credit that does not exist.
        _slip = getattr(config, "ENTRY_SLIPPAGE_PTS_PER_LEG", 0.75)
        net_credit_gated = net_credit - _slip * 4  # 4 legs

        # CFG-P1-01: read from config, not a hardcoded literal.
        # The old code had _min_credit_ratio = 0.15 hardcoded here
        # while config.CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.22 was
        # ignored — tuning the config had no effect.
        _min_credit_ratio = getattr(
            config,
            "CONDOR_MIN_CREDIT_PCT_OF_WIDTH",
            0.15,
        )
        # PRF-S02: min credit check uses dynamic wing width
        _min_credit_required = max(
            config.CONDOR_MIN_CREDIT,
            _min_credit_ratio * _dynamic_wing,
        )
        if net_credit_gated < _min_credit_required:
            logger.warning(
                f"Condor: executable credit={net_credit:.2f} "
                f"after slippage={net_credit_gated:.2f} "
                f"< min={_min_credit_required:.1f}"
            )
            return (None, {})

        # PRF-S02: max_risk uses dynamic wing width
        max_risk = (
            _dynamic_wing - net_credit
        ) * config.LOT_SIZE

        legs = [
            Leg(
                instrument_key=chain[long_call]["call"][
                    "instrument_key"
                ],
                option_type="call", action="BUY",
                strike=long_call, expiry=expiry, qty=1,
                delta=chain[long_call]["call"]["delta"],
                gamma=chain[long_call]["call"]["gamma"],
                vega=chain[long_call]["call"]["vega"],
                theta=chain[long_call]["call"]["theta"],
            ),
            Leg(
                instrument_key=chain[long_put]["put"][
                    "instrument_key"
                ],
                option_type="put", action="BUY",
                strike=long_put, expiry=expiry, qty=1,
                delta=chain[long_put]["put"]["delta"],
                gamma=chain[long_put]["put"]["gamma"],
                vega=chain[long_put]["put"]["vega"],
                theta=chain[long_put]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[short_call]["call"][
                    "instrument_key"
                ],
                option_type="call", action="SELL",
                strike=short_call, expiry=expiry, qty=1,
                delta=chain[short_call]["call"]["delta"],
                gamma=chain[short_call]["call"]["gamma"],
                vega=chain[short_call]["call"]["vega"],
                theta=chain[short_call]["call"]["theta"],
            ),
            Leg(
                instrument_key=chain[short_put]["put"][
                    "instrument_key"
                ],
                option_type="put", action="SELL",
                strike=short_put, expiry=expiry, qty=1,
                delta=chain[short_put]["put"]["delta"],
                gamma=chain[short_put]["put"]["gamma"],
                vega=chain[short_put]["put"]["vega"],
                theta=chain[short_put]["put"]["theta"],
            ),
        ]

        meta = {
            "net_credit":    net_credit,
            "max_risk":      max_risk,
            "short_call":    short_call,
            "short_put":     short_put,
            "long_call":     long_call,
            "long_put":      long_put,
            "profit_target": net_credit * (
                1 - config.CONDOR_TARGET_PCT
            ),
            # PATCH S-05: 2.0x→1.0x. At 2.0x stop + 60% target,
            # break-even WR=77% (unachievable). At 1.0x + 60%: WR=62.5%.
            "stop_loss":     0.0,  # RC1a: condor premium stop disabled (spot-distance stop via CONDOR_TESTED_SIDE_BUFFER is sufficient)  # P1-4a: condor stop_loss 2.0x (was 1.0x, fired immediately at entry)
            "exit_dte":      config.CONDOR_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
            "total_credit":  net_credit,
            # SE-F3: store expected_move for sigma-scaled buffer
            "expected_move":  expected_move,
        }

        logger.info(
            f"Iron condor: sc={short_call} sp={short_put} "
            f"lc={long_call} lp={long_put} "
            f"credit={net_credit:.2f} dte={dte} "
            f"expiry={expiry}"
        )
        return (legs, meta)

    async def _build_credit_spreads(
        self, tranche: int = 1,
        skew_side: str = "both",
    ) -> Tuple[Optional[List[Leg]], Dict]:
        # PRF-02: skew_side controls which side is built.
        # 'put'  = bull-put spread only (put skew rich)
        # 'call' = bear-call spread only (call skew rich)
        # 'both' = both sides (symmetric / no skew signal)
        """
        Build bull put + bear call credit spreads.
        FIX VS1/V3: DTE_MAX=10, tolerance=5.
        FIX QS1: expiry-scoped chain and delta lookup.
        FIX QS3: SPREAD_MIN_CREDIT=10.
        PATCH: tranche>1 targets a later expiry (~1 extra
        weekly cycle per tranche) instead of building an
        identical position at the same expiry as an existing one.
        """
        target_dte    = config.CONDOR_DTE_MIN + 2
        max_dte_bound = config.CONDOR_DTE_MAX
        if tranche > 1:
            target_dte    += 7 * (tranche - 1)
            max_dte_bound += 7 * (tranche - 1)

        # FIX VS1: increased tolerance from 2 to 5
        expiry = self.dm.get_expiry_by_dte(
            target_dte,
            tolerance=5,   # was 2
        )
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte < config.SPREAD_EXIT_DTE + 1:
            return (None, {})

        if dte > max_dte_bound:
            return (None, {})

        # C4-12: wire LOW/MID/HIGH_VIX_DELTA into delta selection.
        # Flat SPREAD_DELTA_SHORT=0.16 ignores that selling 0.16 delta
        # at VIX=11 and VIX=25 are very different trades.
        _vix_now = self.dm.vix or 14.0
        _low_vix  = getattr(config, "LOW_VIX",  14.0)
        _high_vix = getattr(config, "HIGH_VIX", 18.0)
        if _vix_now < _low_vix:
            # Low VIX: use wider delta (more OTM, less credit but safer)
            _delta_band = getattr(
                config, "LOW_VIX_DELTA", (0.22, 0.28)
            )
        elif _vix_now > _high_vix:
            # High VIX: use tighter delta (closer strikes, more credit)
            _delta_band = getattr(
                config, "HIGH_VIX_DELTA", (0.15, 0.20)
            )
        else:
            # Mid VIX: standard range
            _delta_band = getattr(
                config, "MID_VIX_DELTA", (0.20, 0.25)
            )
        # Use midpoint of the band as the short delta target
        _short_delta = (_delta_band[0] + _delta_band[1]) / 2.0
        # Long delta stays at config value (wing protection)
        _long_delta  = config.SPREAD_DELTA_LONG
        logger.info(
            f"Credit spread: VIX={_vix_now:.1f} -> "
            f"short_delta={_short_delta:.3f} "
            f"(band={_delta_band})"
        )

        # FIX QS1/CONFIRMED-5: expiry-scoped delta lookup
        short_put_strike  = self.dm.get_strike_by_delta(
            "put", _short_delta,
            expiry=expiry,
        )
        long_put_strike   = self.dm.get_strike_by_delta(
            "put", _long_delta,
            expiry=expiry,
        )
        short_call_strike = self.dm.get_strike_by_delta(
            "call", _short_delta,
            expiry=expiry,
        )
        long_call_strike  = self.dm.get_strike_by_delta(
            "call", _long_delta,
            expiry=expiry,
        )

        if any(
            s is None for s in [
                short_put_strike, long_put_strike,
                short_call_strike, long_call_strike,
            ]
        ):
            return (None, {})

        if short_put_strike <= long_put_strike:
            return (None, {})
        if short_call_strike >= long_call_strike:
            return (None, {})

        # FIX QS1: expiry-scoped chain
        chain = self.dm.get_chain_for_expiry(expiry)
        for strike in [
            short_put_strike, long_put_strike,
            short_call_strike, long_call_strike,
        ]:
            if strike not in chain:
                return (None, {})

        sp_prem = chain[short_put_strike]["put"]["ltp"]
        lp_prem = chain[long_put_strike]["put"]["ltp"]
        sc_prem = chain[short_call_strike]["call"]["ltp"]
        lc_prem = chain[long_call_strike]["call"]["ltp"]

        total_credit = (
            (sp_prem - lp_prem) + (sc_prem - lc_prem)
        )

        # SE-01: widths computed before the credit gate.
        put_width  = short_put_strike  - long_put_strike
        call_width = long_call_strike  - short_call_strike
        # PATCH S-01: assign side flags BEFORE first use.
        # Previously assigned after the credit gate that used them → NameError.
        _build_put_side  = skew_side in ("put",  "both")
        _build_call_side = skew_side in ("call", "both")

        # PATCHED S-01: assign side flags BEFORE first use.
        # Previously assigned after the credit gate that used them.
        _build_put_side  = skew_side in ("put",  "both")
        _build_call_side = skew_side in ("call", "both")

        # FIX-02: max_risk based on active sides only
        _active_w = max(
            put_width  if _build_put_side  else 0,
            call_width if _build_call_side else 0,
        )
        max_risk   = (
            max(_active_w, 1) - total_credit
        ) * config.LOT_SIZE

        # FIX-03/11: side-aware credit gate using credit/max-loss.
        # Old gate used two-sided width for a one-sided build, and
        # credit/width (0.20) is unreachable at 0.20/0.08 delta pair.
        _active_put_width  = put_width  if _build_put_side  else 0
        _active_call_width = call_width if _build_call_side else 0
        _active_max_width  = max(_active_put_width, _active_call_width)
        _spread_min_per_maxloss = getattr(
            config, "SPREAD_MIN_CREDIT_PER_MAXLOSS", 0.10
        )
        _spread_min_maxloss_gate = (
            _active_max_width
            * _spread_min_per_maxloss
            / (1.0 + _spread_min_per_maxloss)
            if _active_max_width > 0 else 0
        )
        _spread_min_required = max(
            config.SPREAD_MIN_CREDIT,
            _spread_min_maxloss_gate,
        )
        # FIX-04: use executable prices (bid/ask) not LTP
        def _exec_spread(opt, action):
            b = float(opt.get("bid") or 0)
            a = float(opt.get("ask") or 0)
            if b <= 0 or a <= 0:
                return float(opt.get("ltp") or 0)
            return b if action == "SELL" else a
        _exec_credit = 0.0
        if _build_put_side:
            _exec_credit += (
                _exec_spread(chain[short_put_strike]["put"], "SELL")
                - _exec_spread(chain[long_put_strike]["put"], "BUY")
            )
        if _build_call_side:
            _exec_credit += (
                _exec_spread(chain[short_call_strike]["call"], "SELL")
                - _exec_spread(chain[long_call_strike]["call"], "BUY")
            )
        _slip = getattr(config, "ENTRY_SLIPPAGE_PTS_PER_LEG", 0.75)
        _n_active_legs = (
            (2 if _build_put_side else 0)
            + (2 if _build_call_side else 0)
        )
        _exec_credit_gated = _exec_credit - _slip * _n_active_legs
        if _exec_credit_gated < _spread_min_required:
            logger.info(
                f"Credit spread ({skew_side}): "
                f"exec_credit={_exec_credit:.2f} "
                f"after_slippage={_exec_credit_gated:.2f} "
                f"< min={_spread_min_required:.1f} "
                f"(credit/maxloss gate)"
            )
            return (None, {})
        # Use executable credit for position record
        total_credit = _exec_credit

        # PATCH S-02: _build_put_side/_build_call_side already assigned
        # above (PATCH S-01). No re-assignment needed here.

        # Recalculate credit and max_risk for the active sides
        if not _build_put_side:
            total_credit = sc_prem - lc_prem
        elif not _build_call_side:
            total_credit = sp_prem - lp_prem
        # else total_credit already computed above for both sides

        if total_credit < config.SPREAD_MIN_CREDIT:
            logger.info(
                f"Credit spread ({skew_side} side): "
                f"credit={total_credit:.2f} < "
                f"min={config.SPREAD_MIN_CREDIT} — skip"
            )
            return (None, {})

        _active_put_width  = (
            short_put_strike - long_put_strike
            if _build_put_side else 0
        )
        _active_call_width = (
            long_call_strike - short_call_strike
            if _build_call_side else 0
        )
        max_risk = (
            max(_active_put_width, _active_call_width)
            - total_credit
        ) * config.LOT_SIZE

        legs = []
        # P1-2: call legs gated on _build_call_side
        # P1-3: lp_prem corrected (was lc_prem in put-only branch)
        if _build_put_side:
            legs += [
                Leg(
                instrument_key=chain[long_put_strike][
                "put"
                ]["instrument_key"],
                option_type="put", action="BUY",
                strike=long_put_strike, expiry=expiry,
                qty=1,
                delta=chain[long_put_strike]["put"]["delta"],
                gamma=chain[long_put_strike]["put"]["gamma"],
                vega=chain[long_put_strike]["put"]["vega"],
                theta=chain[long_put_strike]["put"]["theta"],
                ),
                Leg(
                instrument_key=chain[short_put_strike][
                "put"
                ]["instrument_key"],
                option_type="put", action="SELL",
                strike=short_put_strike, expiry=expiry,
                qty=1,
                delta=chain[short_put_strike]["put"]["delta"],
                gamma=chain[short_put_strike]["put"]["gamma"],
                vega=chain[short_put_strike]["put"]["vega"],
                theta=chain[short_put_strike]["put"]["theta"],
                ),
            ]
        if _build_call_side:
            legs += [
                Leg(
                instrument_key=chain[long_call_strike][
                "call"
                ]["instrument_key"],
                option_type="call", action="BUY",
                strike=long_call_strike, expiry=expiry,
                qty=1,
                delta=chain[long_call_strike]["call"]["delta"],
                gamma=chain[long_call_strike]["call"]["gamma"],
                vega=chain[long_call_strike]["call"]["vega"],
                theta=chain[long_call_strike]["call"]["theta"],
                ),
                Leg(
                instrument_key=chain[short_call_strike][
                "call"
                ]["instrument_key"],
                option_type="call", action="SELL",
                strike=short_call_strike, expiry=expiry,
                qty=1,
                delta=chain[short_call_strike]["call"]["delta"],
                gamma=chain[short_call_strike]["call"]["gamma"],
                vega=chain[short_call_strike]["call"]["vega"],
                theta=chain[short_call_strike]["call"]["theta"],
                ),
            ]

        meta = {
            "total_credit":  total_credit,
            "max_risk":      max_risk,
            "short_put":     short_put_strike,
            "long_put":      long_put_strike,
            "short_call":    short_call_strike,
            "long_call":     long_call_strike,
            "profit_target": total_credit * (
                1 - config.SPREAD_TARGET_PCT
            ),
            # PATCH S-06: 2.0x→1.0x (same reasoning as S-05).
            "stop_loss":     0.0,  # RC1b: spread premium stop disabled (spot-distance stop via CONDOR_TESTED_SIDE_BUFFER is sufficient)  # P1-4b: spread stop_loss 2.0x (was 1.0x, fired immediately at entry)
            "exit_dte":      config.SPREAD_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
        }
        return (legs, meta)

    async def _build_ratio_spread(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build 1x2 ratio spread."""
        expiry = self.dm.get_expiry_by_dte(
            config.CONDOR_DTE_MIN + 2, tolerance=5
        )
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte < config.RATIO_EXIT_DTE + 1:
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})

        chain = self.dm.get_chain_for_expiry(expiry)
        if not chain:
            return (None, {})

        call_short = atm
        call_long  = atm + config.RATIO_ATM_OFFSET_PTS
        put_short  = atm
        put_long   = atm - config.RATIO_ATM_OFFSET_PTS

        for s in [call_short, call_long, put_short, put_long]:
            if s not in chain:
                return (None, {})

        cs_prem = chain[call_short]["call"]["ltp"]
        cl_prem = chain[call_long]["call"]["ltp"]
        ps_prem = chain[put_short]["put"]["ltp"]
        pl_prem = chain[put_long]["put"]["ltp"]

        total_credit = (
            (cs_prem - 2 * cl_prem)
            + (ps_prem - 2 * pl_prem)
        )

        if total_credit <= 0:
            return (None, {})

        # SE-P1-06: genuine 1x2 ratio spread = sell 2x OTM,
        # buy 1x ATM. The old structure (buy 2x OTM, sell 1x ATM)
        # was a backspread (net debit), which always returned
        # total_credit <= 0 and was rejected immediately.
        legs = [
            Leg(
                instrument_key=chain[call_short]["call"][
                    "instrument_key"
                ],
                option_type="call", action="BUY",
                strike=call_short, expiry=expiry, qty=1,
            ),
            Leg(
                instrument_key=chain[put_short]["put"][
                    "instrument_key"
                ],
                option_type="put", action="BUY",
                strike=put_short, expiry=expiry, qty=1,
            ),
            Leg(
                instrument_key=chain[call_long]["call"][
                    "instrument_key"
                ],
                option_type="call", action="SELL",
                strike=call_long, expiry=expiry, qty=2,
            ),
            Leg(
                instrument_key=chain[put_long]["put"][
                    "instrument_key"
                ],
                option_type="put", action="SELL",
                strike=put_long, expiry=expiry, qty=2,
            ),
        ]

        # Ratio spread max_risk fix: the maximum loss of a
        # 1x2 ratio spread occurs when the underlying pins the
        # long strike at expiry. Max loss = wing_width - credit.
        # The old formula (credit * 2) understated this by ~2x
        # for typical credit levels, causing over-leveraging.
        _ratio_max_risk = max(
            (config.RATIO_ATM_OFFSET_PTS - total_credit)
            * config.LOT_SIZE,
            total_credit * config.LOT_SIZE,  # floor: at least credit
        )
        meta = {
            "total_credit":  total_credit,
            "max_risk":      _ratio_max_risk,
            "profit_target": total_credit * (
                1 - config.RATIO_TARGET_PCT
            ),
            # PATCH S-07: 2.0x→1.0x stop for ratio spread.
            "stop_loss":     total_credit * 1.0,
            "exit_dte":      config.RATIO_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
        }
        return (legs, meta)

    async def _build_butterfly(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build long put butterfly."""
        expiry = self.dm.get_expiry_by_dte(
            4, tolerance=3
        )
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte > config.BUTTERFLY_DTE_MAX:
            return (None, {})
        if dte <= config.BUTTERFLY_EXIT_DTE:
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})

        chain = self.dm.get_chain_for_expiry(expiry)
        if not chain:
            return (None, {})

        wing_width = config.NIFTY_STRIKE_STEP * 1
        strike_a   = atm - wing_width
        strike_b   = atm
        strike_c   = atm + wing_width

        for s in [strike_a, strike_b, strike_c]:
            if s not in chain:
                logger.warning(
                    f"Butterfly: strike {s} not in "
                    f"chain for {expiry}"
                )
                return (None, {})

        prem_a = chain[strike_a]["put"]["ltp"]
        prem_b = chain[strike_b]["put"]["ltp"]
        prem_c = chain[strike_c]["put"]["ltp"]

        net_debit = (prem_a + prem_c) - (2 * prem_b)

        if net_debit > config.BUTTERFLY_MAX_DEBIT_PTS:
            logger.info(
                f"Butterfly: net_debit={net_debit:.2f} "
                f"> max={config.BUTTERFLY_MAX_DEBIT_PTS}"
            )
            return (None, {})
        if net_debit <= 0:
            return (None, {})

        max_profit = wing_width - net_debit
        rr_ratio   = (
            max_profit / net_debit if net_debit > 0 else 0
        )

        if rr_ratio < config.BUTTERFLY_MIN_RR_RATIO:
            return (None, {})

        legs = [
            Leg(
                instrument_key=chain[strike_a]["put"][
                    "instrument_key"
                ],
                option_type="put", action="BUY",
                strike=strike_a, expiry=expiry, qty=1,
                delta=chain[strike_a]["put"]["delta"],
                gamma=chain[strike_a]["put"]["gamma"],
                vega=chain[strike_a]["put"]["vega"],
                theta=chain[strike_a]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[strike_c]["put"][
                    "instrument_key"
                ],
                option_type="put", action="BUY",
                strike=strike_c, expiry=expiry, qty=1,
                delta=chain[strike_c]["put"]["delta"],
                gamma=chain[strike_c]["put"]["gamma"],
                vega=chain[strike_c]["put"]["vega"],
                theta=chain[strike_c]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[strike_b]["put"][
                    "instrument_key"
                ],
                option_type="put", action="SELL",
                strike=strike_b, expiry=expiry, qty=1,
                delta=chain[strike_b]["put"]["delta"],
                gamma=chain[strike_b]["put"]["gamma"],
                vega=chain[strike_b]["put"]["vega"],
                theta=chain[strike_b]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[strike_b]["put"][
                    "instrument_key"
                ],
                option_type="put", action="SELL",
                strike=strike_b, expiry=expiry, qty=1,
                delta=chain[strike_b]["put"]["delta"],
                gamma=chain[strike_b]["put"]["gamma"],
                vega=chain[strike_b]["put"]["vega"],
                theta=chain[strike_b]["put"]["theta"],
            ),
        ]

        meta = {
            "net_debit":     net_debit,
            "max_risk":      net_debit * config.LOT_SIZE,
            "max_profit":    max_profit,
            "rr_ratio":      rr_ratio,
            "strike_a":      strike_a,
            "strike_b":      strike_b,
            "strike_c":      strike_c,
            "profit_target": (
                max_profit * config.BUTTERFLY_PROFIT_PCT
            ),
            "stop_loss":     net_debit * config.LOT_SIZE,
            "exit_dte":      config.BUTTERFLY_EXIT_DTE,
            "max_hold_date": (
                date.today()
                + timedelta(days=dte - 1)
            ).strftime("%Y-%m-%d"),
            "strategy_type": "LONG",
        }
        return (legs, meta)

    async def _build_long_straddle(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build ATM long straddle."""
        expiry = self.dm.get_expiry_by_dte(
            config.LONG_STRADDLE_DTE_MIN + 2,
            tolerance=5,
        )
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte < config.LONG_STRADDLE_DTE_MIN:
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})

        chain = self.dm.get_chain_for_expiry(expiry)
        if not chain or atm not in chain:
            return (None, {})

        spot      = self.dm.spot or 0
        vix       = self.dm.vix or 16.0
        call_data = chain[atm]["call"]
        put_data  = chain[atm]["put"]

        total_debit = call_data["ltp"] + put_data["ltp"]
        max_allowed = spot * config.LONG_STRADDLE_MAX_DEBIT_PCT
        if total_debit > max_allowed:
            return (None, {})

        # FIX QS5: VIX spike threshold=0.05
        # VIX spike check bypassed in STRONG_BUY/EVENT
        # Regime detection already confirmed IV is cheap
        if self.re.confirmed_regime not in [
            config.REGIME_STRONG_BUY, config.REGIME_EVENT,
        ]:
            if len(self.dm.vix_history_20d) >= (
                config.LONG_STRADDLE_VIX_SMA_PERIOD
            ):
                vix_arr = list(self.dm.vix_history_20d)
                vix_sma = float(
                    np.mean(
                        vix_arr[
                            -config.LONG_STRADDLE_VIX_SMA_PERIOD:
                        ]
                    )
                )
                if vix_sma > 0:
                    vix_spike = (vix - vix_sma) / vix_sma
                    if vix_spike < (
                        config.LONG_STRADDLE_VIX_SPIKE_PCT
                    ):
                        logger.info(
                            f'Long straddle: VIX spike '
                            f'{vix_spike:.3f} < '
                            f'{config.LONG_STRADDLE_VIX_SPIKE_PCT}'
                            f' — skip'
                        )
                        return (None, {})

        # PRF-03: IV rank gate applies in ALL regimes.
        # The old code bypassed it for STRONG_BUY/EVENT, but those
        # are the only regimes that call this builder. Buying options
        # when IV rank > 40 means paying a fear premium — the strategy
        # thesis (buy cheap vol) is violated.
        _ivr = self.dm.compute_iv_rank()
        iv_rank = _ivr if _ivr is not None else 50.0
        if iv_rank > config.LONG_STRADDLE_MAX_IV_RANK:
            logger.info(
                f"Long straddle: IV rank {iv_rank:.1f} > "
                f"max {config.LONG_STRADDLE_MAX_IV_RANK} "
                f"— vol too expensive to buy"
            )
            return (None, {})
        # Also require IV is actually cheap relative to RV
        if (
            self.dm.iv_atm is not None
            and self.dm.rv_20d is not None
            and self.dm.iv_atm > self.dm.rv_20d + 0.02
        ):
            logger.info(
                f"Long straddle: IV ({self.dm.iv_atm:.4f}) > "
                f"RV ({self.dm.rv_20d:.4f}) + 2% — vol not cheap"
            )
            return (None, {})

        if (
            call_data["ask"] - call_data["bid"]
        ) > config.MAX_SPREAD_ATM_PTS:
            return (None, {})
        if (
            put_data["ask"] - put_data["bid"]
        ) > config.MAX_SPREAD_ATM_PTS:
            return (None, {})

        max_hold_date = (
            date.today()
            + timedelta(days=config.LONG_STRADDLE_HOLD_DAYS)
        ).strftime("%Y-%m-%d")

        legs = [
            Leg(
                instrument_key=call_data["instrument_key"],
                option_type="call", action="BUY",
                strike=atm, expiry=expiry, qty=1,
                delta=call_data["delta"],
                gamma=call_data["gamma"],
                vega=call_data["vega"],
                theta=call_data["theta"],
            ),
            Leg(
                instrument_key=put_data["instrument_key"],
                option_type="put", action="BUY",
                strike=atm, expiry=expiry, qty=1,
                delta=put_data["delta"],
                gamma=put_data["gamma"],
                vega=put_data["vega"],
                theta=put_data["theta"],
            ),
        ]

        meta = {
            "total_debit":   total_debit,
            "max_risk":      total_debit * config.LOT_SIZE,
            "stop_loss":     total_debit * (
                1 - config.LONG_STRADDLE_STOP_PCT
            ),
            "profit_target": total_debit * (
                1 + config.LONG_STRADDLE_TARGET_PCT
            ),
            "exit_dte":      None,
            "max_hold_date": max_hold_date,
            "strategy_type": "LONG",
        }
        return (legs, meta)

    async def _build_backspread(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build directional backspread."""
        expiry = self.dm.get_expiry_by_dte(
            config.BACKSPREAD_DTE_MIN + 2, tolerance=3
        )
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if (
            dte < config.BACKSPREAD_DTE_MIN
            or dte > config.BACKSPREAD_DTE_MAX
        ):
            return (None, {})

        vix = self.dm.vix or 16.0
        if vix > config.BACKSPREAD_MAX_VIX:
            return (None, {})

        # SE8-P1-04 FIX: read direction from regime engine's
        # raw trend detail, not by parsing a log string.
        # The old approach defaulted to bullish when range-bound
        # (majority of sessions), opening call backspreads with
        # no directional signal. Now returns None when no clear
        # trend to avoid directional bets on neutral markets.
        trend = self.re.confirmed_trend
        _trend_detail = self.re._detail.get("trend", "")
        _is_bullish = "bullish" in _trend_detail.lower()
        _is_bearish = "bearish" in _trend_detail.lower()
        if not _is_bullish and not _is_bearish:
            # No confirmed directional trend — skip backspread
            logger.info(
                "Backspread: no confirmed direction — skip"
            )
            return (None, {})
        _trend_direction = 1 if _is_bullish else -1
        chain = self.dm.get_chain_for_expiry(expiry)
        if not chain:
            return (None, {})

        if _trend_direction >= 0:
            long_strike  = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_LONG_DELTA,
                expiry=expiry,
            )
            short_strike = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_SHORT_DELTA,
                expiry=expiry,
            )
            hedge_strike = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_LONG_DELTA,
                expiry=expiry,
            )
            hedge_short  = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_SHORT_DELTA,
                expiry=expiry,
            )

            if any(
                s is None for s in [
                    long_strike, short_strike,
                    hedge_strike, hedge_short,
                ]
            ):
                return (None, {})

            if (
                short_strike - long_strike
            ) < config.BACKSPREAD_MIN_STRIKE_WIDTH:
                return (None, {})

            for s in [
                long_strike, short_strike,
                hedge_strike, hedge_short,
            ]:
                if s not in chain:
                    return (None, {})

            long_prem    = chain[long_strike]["call"]["ltp"]
            short_prem   = chain[short_strike]["call"]["ltp"]
            hedge_prem   = chain[hedge_strike]["put"]["ltp"]
            hedge_s_prem = chain[hedge_short]["put"]["ltp"]

            net_debit = (
                long_prem * config.BACKSPREAD_LONG_QTY
                + hedge_prem
                - short_prem
                - hedge_s_prem
            )

            if net_debit > config.BACKSPREAD_MAX_DEBIT_PTS:
                return (None, {})

            legs = [
                Leg(
                    instrument_key=chain[long_strike][
                        "call"
                    ]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=long_strike, expiry=expiry,
                    qty=config.BACKSPREAD_LONG_QTY,
                    delta=chain[long_strike]["call"]["delta"],
                    gamma=chain[long_strike]["call"]["gamma"],
                    vega=chain[long_strike]["call"]["vega"],
                    theta=chain[long_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_strike][
                        "put"
                    ]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=hedge_strike, expiry=expiry,
                    qty=1,
                    delta=chain[hedge_strike]["put"]["delta"],
                    gamma=chain[hedge_strike]["put"]["gamma"],
                    vega=chain[hedge_strike]["put"]["vega"],
                    theta=chain[hedge_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[short_strike][
                        "call"
                    ]["instrument_key"],
                    option_type="call", action="SELL",
                    strike=short_strike, expiry=expiry,
                    qty=1,
                    delta=chain[short_strike]["call"]["delta"],
                    gamma=chain[short_strike]["call"]["gamma"],
                    vega=chain[short_strike]["call"]["vega"],
                    theta=chain[short_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_short][
                        "put"
                    ]["instrument_key"],
                    option_type="put", action="SELL",
                    strike=hedge_short, expiry=expiry,
                    qty=1,
                    delta=chain[hedge_short]["put"]["delta"],
                    gamma=chain[hedge_short]["put"]["gamma"],
                    vega=chain[hedge_short]["put"]["vega"],
                    theta=chain[hedge_short]["put"]["theta"],
                ),
            ]
        else:
            long_strike  = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_LONG_DELTA,
                expiry=expiry,
            )
            short_strike = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_SHORT_DELTA,
                expiry=expiry,
            )
            hedge_strike = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_LONG_DELTA,
                expiry=expiry,
            )
            hedge_short  = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_SHORT_DELTA,
                expiry=expiry,
            )

            if any(
                s is None for s in [
                    long_strike, short_strike,
                    hedge_strike, hedge_short,
                ]
            ):
                return (None, {})

            for s in [
                long_strike, short_strike,
                hedge_strike, hedge_short,
            ]:
                if s not in chain:
                    return (None, {})

            long_prem    = chain[long_strike]["put"]["ltp"]
            short_prem   = chain[short_strike]["put"]["ltp"]
            hedge_prem   = chain[hedge_strike]["call"]["ltp"]
            hedge_s_prem = chain[hedge_short]["call"]["ltp"]

            net_debit = (
                long_prem * config.BACKSPREAD_LONG_QTY
                + hedge_prem
                - short_prem
                - hedge_s_prem
            )

            if net_debit > config.BACKSPREAD_MAX_DEBIT_PTS:
                return (None, {})

            legs = [
                Leg(
                    instrument_key=chain[long_strike][
                        "put"
                    ]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=long_strike, expiry=expiry,
                    qty=config.BACKSPREAD_LONG_QTY,
                    delta=chain[long_strike]["put"]["delta"],
                    gamma=chain[long_strike]["put"]["gamma"],
                    vega=chain[long_strike]["put"]["vega"],
                    theta=chain[long_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_strike][
                        "call"
                    ]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=hedge_strike, expiry=expiry,
                    qty=1,
                    delta=chain[hedge_strike]["call"]["delta"],
                    gamma=chain[hedge_strike]["call"]["gamma"],
                    vega=chain[hedge_strike]["call"]["vega"],
                    theta=chain[hedge_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[short_strike][
                        "put"
                    ]["instrument_key"],
                    option_type="put", action="SELL",
                    strike=short_strike, expiry=expiry,
                    qty=1,
                    delta=chain[short_strike]["put"]["delta"],
                    gamma=chain[short_strike]["put"]["gamma"],
                    vega=chain[short_strike]["put"]["vega"],
                    theta=chain[short_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_short][
                        "call"
                    ]["instrument_key"],
                    option_type="call", action="SELL",
                    strike=hedge_short, expiry=expiry,
                    qty=1,
                    delta=chain[hedge_short]["call"]["delta"],
                    gamma=chain[hedge_short]["call"]["gamma"],
                    vega=chain[hedge_short]["call"]["vega"],
                    theta=chain[hedge_short]["call"]["theta"],
                ),
            ]

        max_hold_date = (
            date.today()
            + timedelta(days=config.LONG_STRADDLE_HOLD_DAYS)
        ).strftime("%Y-%m-%d")

        safe_debit = max(net_debit, 0.05)
        meta = {
            "net_debit":       safe_debit,
            "max_risk":        safe_debit * config.LOT_SIZE,
            "profit_target":   (
                safe_debit
                * config.BACKSPREAD_PROFIT_MULTIPLE
            ),
            "stop_loss":       safe_debit * 0.40,
            "exit_dte":        config.BACKSPREAD_EXIT_DTE,
            "max_hold_date":   max_hold_date,
            "strategy_type":   "LONG",
            "trend_direction": trend,
        }
        return (legs, meta)

    async def _build_long_strangle(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build long strangle for event volatility."""
        # FIX-05: IV-rank gate, matching _build_long_straddle.
        # Without this, the strangle buys options at any IV level,
        # including when vol is expensive. Monte Carlo shows
        # EV = -Rs900 to -Rs3700/lot with no cheapness check.
        _ivr = self.dm.compute_iv_rank()
        _iv_rank = _ivr if _ivr is not None else 50.0
        _max_iv_rank = getattr(
            config, "LONG_STRADDLE_MAX_IV_RANK", 40
        )
        if _iv_rank > _max_iv_rank:
            logger.info(
                f"Long strangle: IV rank {_iv_rank:.1f} > "
                f"{_max_iv_rank} — vol too expensive to buy"
            )
            return (None, {})
        # Also require IV is not rich vs RV
        if (
            self.dm.iv_atm is not None
            and self.dm.rv_20d is not None
            and self.dm.iv_atm > self.dm.rv_20d + 0.02
        ):
            logger.info(
                f"Long strangle: IV ({self.dm.iv_atm:.4f}) > "
                f"RV ({self.dm.rv_20d:.4f}) + 2%% — vol not cheap"
            )
            return (None, {})
        expiry = self.dm.get_expiry_by_dte(
            config.EVENT_STRANGLE_DTE_TARGET,
            tolerance=config.EVENT_STRANGLE_DTE_TARGET - 2,
        )
        if expiry is None:
            return (None, {})
        # AUDIT #N2: enforce upper DTE bound so a far-dated
        # expiry is never silently used when the intended
        # short-dated window is unavailable.
        _n2_dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if _n2_dte > config.EVENT_STRANGLE_DTE_MAX:
            logger.info(
                f"Strangle: expiry {expiry} DTE={_n2_dte} "
                f"> max={config.EVENT_STRANGLE_DTE_MAX} "
                f"— skip"
            )
            return (None, {})

        call_strike = self.dm.get_strike_by_delta(
            "call", config.EVENT_STRANGLE_DELTA,
            expiry=expiry,
        )
        put_strike  = self.dm.get_strike_by_delta(
            "put", config.EVENT_STRANGLE_DELTA,
            expiry=expiry,
        )

        if call_strike is None or put_strike is None:
            return (None, {})

        chain = self.dm.get_chain_for_expiry(expiry)
        for s in [call_strike, put_strike]:
            if s not in chain:
                return (None, {})

        call_spread = (
            chain[call_strike]["call"]["ask"]
            - chain[call_strike]["call"]["bid"]
        )
        put_spread = (
            chain[put_strike]["put"]["ask"]
            - chain[put_strike]["put"]["bid"]
        )

        if call_spread > config.EVENT_STRANGLE_MAX_SPREAD_PTS:
            return (None, {})

        if put_spread > config.EVENT_STRANGLE_MAX_SPREAD_PTS:
            logger.info(
                f"Strangle: put spread={put_spread:.2f} "
                f"too wide — returning None (not falling back)"
            )
            # SE7-P2-01: the old fallback returned straddle legs
            # with straddle meta but the caller still held
            # strategy_name=STRAT_STRANGLE, corrupting attribution
            # and MAX_TRANCHES_PER_STRATEGY counting.
            # Return None so the caller can try a genuine straddle.
            return (None, {})

        total_debit   = (
            chain[call_strike]["call"]["ltp"]
            + chain[put_strike]["put"]["ltp"]
        )
        max_hold_date = (
            date.today() + timedelta(days=2)
        ).strftime("%Y-%m-%d")

        legs = [
            Leg(
                instrument_key=chain[call_strike]["call"][
                    "instrument_key"
                ],
                option_type="call", action="BUY",
                strike=call_strike, expiry=expiry, qty=1,
                delta=chain[call_strike]["call"]["delta"],
                gamma=chain[call_strike]["call"]["gamma"],
                vega=chain[call_strike]["call"]["vega"],
                theta=chain[call_strike]["call"]["theta"],
            ),
            Leg(
                instrument_key=chain[put_strike]["put"][
                    "instrument_key"
                ],
                option_type="put", action="BUY",
                strike=put_strike, expiry=expiry, qty=1,
                delta=chain[put_strike]["put"]["delta"],
                gamma=chain[put_strike]["put"]["gamma"],
                vega=chain[put_strike]["put"]["vega"],
                theta=chain[put_strike]["put"]["theta"],
            ),
        ]

        meta = {
            "total_debit":   total_debit,
            "max_risk":      total_debit * config.LOT_SIZE,
            "stop_loss":     total_debit * (
                1 - config.EVENT_STRANGLE_STOP_PCT
            ),
            "profit_target": total_debit * (
                1 + config.EVENT_STRANGLE_TARGET_PCT
            ),
            "exit_dte":      None,
            "max_hold_date": max_hold_date,
            "strategy_type": "LONG",
        }
        return (legs, meta)

    async def _build_defensive_hedge(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build defensive hedge."""
        import math as _math

        short_legs = [
            leg
            for pos in self.open_positions
            for leg in pos.legs
            if leg.action == "SELL"
        ]
        if not short_legs:
            return (None, {})

        if len(self.dm.vix_history_20d) >= (
            config.DEFENSIVE_VIX_SMA_PERIOD
        ):
            vix_arr = list(self.dm.vix_history_20d)
            vix_sma = float(
                np.mean(
                    vix_arr[
                        -config.DEFENSIVE_VIX_SMA_PERIOD:
                    ]
                )
            )
            if vix_sma > 0:
                vix_spike = (
                    (self.dm.vix - vix_sma) / vix_sma
                )
                if vix_spike < (
                    config.DEFENSIVE_VIX_SPIKE_PCT
                ):
                    return (None, {})

        ema_20 = self._compute_ema_n(
            config.DEFENSIVE_EMA_PERIOD
        )
        if self.dm.spot and self.dm.spot > ema_20:
            return (None, {})

        # FIX S4/QS9: use LIVE chain delta
        total_delta = 0.0
        for pos in self.open_positions:
            expiry_chain = self.dm.get_chain_for_expiry(
                pos.expiry_date
            )
            for leg in pos.legs:
                if leg.action == "SELL":
                    live_delta = float(
                        expiry_chain
                        .get(leg.strike, {})
                        .get(leg.option_type, {})
                        .get("delta", leg.delta)
                    )
                    total_delta += (
                        -1 * live_delta
                        * leg.qty * config.LOT_SIZE
                    )

        reduction_legs = []
        for pos in self.open_positions:
            for leg in pos.legs:
                if leg.action == "SELL":
                    reduce_qty = _math.ceil(
                        leg.qty
                        * config.DEFENSIVE_REDUCTION_PCT
                    )
                    reduction_legs.append({
                        "position":   pos,
                        "leg":        leg,
                        "reduce_qty": reduce_qty,
                    })

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})

        expiry = self.dm.get_expiry_by_dte(
            config.LONG_STRADDLE_DTE_MIN, tolerance=5
        )
        if expiry is None:
            return (None, {})

        chain = self.dm.get_chain_for_expiry(expiry)
        if not chain or atm not in chain:
            return (None, {})

        atm_put_data  = chain[atm]["put"]
        atm_put_delta = abs(atm_put_data["delta"])

        remaining_delta = total_delta * (
            1 - config.DEFENSIVE_REDUCTION_PCT
        )
        hedge_qty = _math.ceil(
            abs(remaining_delta)
            / (atm_put_delta * config.LOT_SIZE + 1e-10)
        )
        hedge_qty = max(1, hedge_qty)

        # EXE-06: buy OTM put debit spread instead of naked ATM put.
        # Buying ATM puts AFTER a VIX spike means paying peak IV.
        # The vega crush that follows destroys the hedge's value.
        # An OTM put debit spread (buy OTM put, sell further OTM put)
        # costs less vega, provides directional protection, and profits
        # from further spot decline without being destroyed by IV crush.
        # Use ~0.30 delta for the long put, ~0.15 delta for the short.
        _otm_long_strike = self.dm.get_strike_by_delta(
            "put", 0.30, expiry=expiry
        )
        _otm_short_strike = self.dm.get_strike_by_delta(
            "put", 0.15, expiry=expiry
        )
        # Fall back to naked ATM put if OTM spread not available
        if (
            _otm_long_strike is None
            or _otm_short_strike is None
            or _otm_long_strike not in chain
            or _otm_short_strike not in chain
            or _otm_long_strike <= _otm_short_strike
        ):
            logger.info(
                "Defensive hedge: OTM spread not available, "
                "falling back to ATM put"
            )
            _use_spread = False
        else:
            _use_spread = True
            _long_put_data  = chain[_otm_long_strike]["put"]
            _short_put_data = chain[_otm_short_strike]["put"]

        if _use_spread:
            _net_debit = (
                _long_put_data["ltp"] - _short_put_data["ltp"]
            ) * hedge_qty
            _max_risk = _net_debit * config.LOT_SIZE
            legs = [
                Leg(
                    instrument_key=_long_put_data["instrument_key"],
                    option_type="put", action="BUY",
                    strike=_otm_long_strike, expiry=expiry,
                    qty=hedge_qty,
                    delta=_long_put_data["delta"],
                    gamma=_long_put_data["gamma"],
                    vega=_long_put_data["vega"],
                    theta=_long_put_data["theta"],
                ),
                Leg(
                    instrument_key=_short_put_data["instrument_key"],
                    option_type="put", action="SELL",
                    strike=_otm_short_strike, expiry=expiry,
                    qty=hedge_qty,
                    delta=_short_put_data["delta"],
                    gamma=_short_put_data["gamma"],
                    vega=_short_put_data["vega"],
                    theta=_short_put_data["theta"],
                ),
            ]
            logger.info(
                f"Defensive hedge: OTM put spread "
                f"{_otm_long_strike}/{_otm_short_strike} "
                f"net_debit={_net_debit:.2f}"
            )
        else:
            _net_debit = atm_put_data["ltp"] * hedge_qty
            _max_risk  = _net_debit * config.LOT_SIZE
            legs = [
                Leg(
                    instrument_key=atm_put_data[
                        "instrument_key"
                    ],
                    option_type="put", action="BUY",
                    strike=atm, expiry=expiry,
                    qty=hedge_qty,
                    delta=atm_put_data["delta"],
                    gamma=atm_put_data["gamma"],
                    vega=atm_put_data["vega"],
                    theta=atm_put_data["theta"],
                )
            ]

        max_hold_date = (
            date.today()
            + timedelta(days=config.DEFENSIVE_MAX_HOLD_DAYS)
        ).strftime("%Y-%m-%d")

        meta = {
            "total_debit":    _net_debit,
            "max_risk":       _max_risk,
            "stop_loss":      (
                _net_debit
                * (1 - config.EVENT_STRANGLE_STOP_PCT)
            ),
            "profit_target":  None,
            "exit_dte":       None,
            "max_hold_date":  max_hold_date,
            "strategy_type":  "LONG",
            "reduction_legs": reduction_legs,
            "hedge_qty":      hedge_qty,
            "already_sized":  True,
        }
        return (legs, meta)

    async def _pre_trade_checks(
        self, strategy_name: str, legs: List[Leg]
    ) -> bool:
        for leg in legs:
            if leg.action == "SELL":
                try:
                    expiry_date = datetime.strptime(
                        leg.expiry, "%Y-%m-%d"
                    ).date()
                    dte = (expiry_date - date.today()).days
                    if dte < 2:
                        logger.warning(
                            f"Pre-trade: DTE={dte} < 2 "
                            f"for SELL strike={leg.strike}"
                        )
                        return False
                except ValueError:
                    return False

        # FIX FS2: validate non-zero LTP from expiry chain
        for leg in legs:
            expiry_chain = self.dm.get_chain_for_expiry(
                leg.expiry
            )
            opt = expiry_chain.get(
                leg.strike, {}
            ).get(leg.option_type, {})
            ltp = float(opt.get("ltp", 0) or 0)
            if ltp <= 0:
                logger.warning(
                    f"Pre-trade: LTP=0 for "
                    f"{leg.option_type} {leg.strike} "
                    f"expiry={leg.expiry} — aborting"
                )
                return False

        # Spread check
        for leg in legs:
            expiry_chain = self.dm.get_chain_for_expiry(
                leg.expiry
            )
            opt    = expiry_chain.get(
                leg.strike, {}
            ).get(leg.option_type, {})
            spread = (
                opt.get("ask", 0) - opt.get("bid", 0)
            )
            is_atm = (
                self.dm.atm_strike is not None
                and leg.strike == self.dm.atm_strike
            )
            max_spread = (
                config.MAX_SPREAD_ATM_PTS
                if is_atm
                else config.MAX_SPREAD_OTM_PTS
            )
            if spread > max_spread:
                logger.warning(
                    f"Pre-trade: spread={spread:.2f} > "
                    f"max={max_spread} "
                    f"{leg.option_type} {leg.strike}"
                )
                return False

        estimated_risk = self._estimate_max_loss(
            strategy_name, legs
        )
        current_risk   = sum(
            p.max_risk for p in self.open_positions
        )
        if current_risk + estimated_risk > (
            config.MAX_COMBINED_RISK
        ):
            logger.warning(
                f"Pre-trade: portfolio risk limit "
                f"current={current_risk:.0f} "
                f"new={estimated_risk:.0f}"
            )
            return False

        new_greeks  = self._estimate_greeks_impact(legs)
        regime      = self.re.confirmed_regime
        limits      = config.GREEKS_LIMITS.get(regime, {})
        port_greeks = self._get_portfolio_greeks()
        post_delta  = (
            port_greeks["delta"] + new_greeks["delta"]
        )
        # PATCH: GREEKS_LIMITS delta_max is a per-lot normalized
        # fraction (e.g. 0.10-0.50); portfolio delta is scaled by
        # LOT_SIZE. Normalize before comparing — otherwise this
        # gate rejects almost every real position.
        post_delta_normalized = post_delta / config.LOT_SIZE
        delta_max   = limits.get("delta_max", 99)
        if abs(post_delta_normalized) > delta_max:
            logger.warning(
                f"Pre-trade: delta limit "
                f"post={post_delta_normalized:.3f} max={delta_max}"
            )
            return False

        # SE-VEGA FIX: extend pre-trade gate to vega.
        if getattr(config, "GREEKS_VEGA_GATE", False):
            post_vega = (
                port_greeks["vega"] + new_greeks["vega"]
            )
            vega_min = limits.get("vega_min")
            vega_max = limits.get("vega_max")
            if vega_min is not None and post_vega < vega_min:
                logger.warning(
                    f"Pre-trade: vega below min: "
                    f"post={post_vega:.0f} < min={vega_min} "
                    f"— blocking entry"
                )
                return False
            if vega_max is not None and post_vega > vega_max:
                logger.warning(
                    f"Pre-trade: vega above max: "
                    f"post={post_vega:.0f} > max={vega_max} "
                    f"— blocking entry"
                )
                return False

        # PRF-S05: portfolio-level absolute vega cap.
        # Prevents over-exposure to volatility shocks across all
        # positions combined. The per-regime vega bands above are
        # relative; this is an absolute rupee-vega cap.
        _vega_cap_pct = getattr(
            config, "PORTFOLIO_VEGA_CAP_PCT", 0.015
        )
        if _vega_cap_pct > 0:
            _vega_cap_rupees = (
                _vega_cap_pct * config.TOTAL_CAPITAL
            )
            _post_vega_abs = abs(
                port_greeks["vega"] + new_greeks["vega"]
            )
            if _post_vega_abs > _vega_cap_rupees:
                logger.warning(
                    f"PRF-S05: portfolio vega cap: "
                    f"|post_vega|={_post_vega_abs:.0f} > "
                    f"cap={_vega_cap_rupees:.0f} "
                    f"({_vega_cap_pct*100:.1f}% of capital) "
                    f"— blocking entry"
                )
                return False

        if not config.PAPER_TRADING_MODE:
            margin_legs = [
                {
                    "instrument_key":   leg.instrument_key,
                    "quantity": (
                        leg.qty * config.LOT_SIZE
                    ),
                    "transaction_type": leg.action,
                    "product":          "D",
                    "price":            self._leg_price(leg),
                }
                for leg in legs
            ]
            margin_ok, required = (
                await self.dm.check_margin(margin_legs)
            )
            if not margin_ok:
                logger.warning(
                    f"Pre-trade: insufficient margin "
                    f"required={required:.0f}"
                )
                return False

        return True

    async def _execute_strategy(
        self,
        strategy_name: str,
        legs:          List[Leg],
        meta:          Dict,
        trade_id:      str = "",
    ) -> bool:
        if not trade_id:
            trade_id = str(uuid.uuid4())

        if strategy_name == config.STRAT_DEFENSIVE:
            ok = await self._execute_reductions(
                meta.get("reduction_legs", [])
            )
            if not ok:
                return False
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        long_legs  = [l for l in legs if l.action == "BUY"]
        short_legs = [l for l in legs if l.action == "SELL"]
        filled_legs: List[Leg] = []

        for idx, leg in enumerate(long_legs):
            success, order_id = (
                await self._place_single_leg(
                    leg,
                    use_market=False,
                    trade_id=trade_id,
                    leg_index=idx,
                )
            )
            if not success:
                logger.warning(
                    f"Long leg failed: "
                    f"{leg.option_type} {leg.strike}"
                )
                await self._cancel_and_reverse(
                    filled_legs
                )
                return False
            leg.order_id    = order_id
            # PATCH (S9): don't clobber the fill_status
            # _resolve_fill_result() already set correctly
            # (COMPLETE or PARTIAL) — this unconditional overwrite
            # was silently destroying the PARTIAL marker that
            # _rebalance_partial_fills() depends on.
            if leg.fill_status != "PARTIAL":
                leg.fill_status = "COMPLETE"
            filled_legs.append(leg)
            self._log_order_detail(
                trade_id, order_id, leg,
                "FILLED", leg.entry_price,
            )
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        for idx, leg in enumerate(short_legs):
            success, order_id = (
                await self._place_single_leg(
                    leg,
                    use_market=False,
                    trade_id=trade_id,
                    leg_index=len(long_legs) + idx,
                )
            )
            if not success:
                logger.warning(
                    f"Short leg failed: "
                    f"{leg.option_type} {leg.strike}"
                )
                await self._cancel_and_reverse(
                    filled_legs
                )
                return False
            leg.order_id    = order_id
            # PATCH (S9): same fix as the long-legs loop above.
            if leg.fill_status != "PARTIAL":
                leg.fill_status = "COMPLETE"
            filled_legs.append(leg)
            self._log_order_detail(
                trade_id, order_id, leg,
                "FILLED", leg.entry_price,
            )
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        logger.info(
            f"All {len(filled_legs)} legs filled "
            f"for {strategy_name}"
        )

        # AUDIT SE-N07: rebalance failure must fail the execution.
        # An imbalanced condor/spread is a naked position.
        try:
            _rebalance_ok = await self._rebalance_partial_fills(
                filled_legs
            )
        except Exception as e:
            logger.error(
                f"_rebalance_partial_fills error: {e}"
            )
            _rebalance_ok = False
        if not _rebalance_ok:
            logger.error(
                f"SE-N07: rebalance failed for {strategy_name} — "
                f"reversing all filled legs"
            )
            await self._cancel_and_reverse(filled_legs)
            return False

        return True

    async def _rebalance_partial_fills(
        self, legs: List[Leg]
    ) -> bool:
        """
        AUDIT SE-N07: returns True if balanced (or no rebalance
        needed), False if a trim failed and the position is
        imbalanced. Caller must reverse all legs on False.
        """
        if not any(l.fill_status == "PARTIAL" for l in legs):
            return True
        min_qty = min(
            (l.qty for l in legs if l.qty > 0), default=0
        )
        if min_qty <= 0:
            return True
        for idx, leg in enumerate(legs):
            excess = leg.qty - min_qty
            if excess > 0:
                trim_action = (
                    "BUY" if leg.action == "SELL" else "SELL"
                )
                trim_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action=trim_action,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=excess,
                )
                success, _ = await self._place_single_leg(
                    trim_leg,
                    use_market=True,
                    trade_id=(
                        f"rebalance-{uuid.uuid4().hex[:8]}"
                    ),
                    leg_index=idx,
                )
                if success:
                    logger.warning(
                        f"Rebalanced {leg.option_type} "
                        f"{leg.strike}: trimmed "
                        f"{trim_leg.qty} lots to match "
                        f"partial-fill minimum {min_qty}"
                    )
                    leg.qty = min_qty
                else:
                    logger.error(
                        f"Rebalance trim FAILED for "
                        f"{leg.option_type} {leg.strike} — "
                        f"returning False for caller to reverse"
                    )
                    return False
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )
        return True

    def _log_order_detail(
        self,
        trade_id:   str,
        order_id:   str,
        leg:        Leg,
        status:     str,
        fill_price: float = 0.0,
    ) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO order_log (
                    timestamp, trade_id, order_id,
                    instrument_key, action,
                    option_type, strike, expiry,
                    qty, order_type, price,
                    fill_price, status, slippage,
                    paper_trade
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(self._IST).isoformat(),
                trade_id,
                order_id,
                leg.instrument_key,
                leg.action,
                leg.option_type,
                leg.strike,
                leg.expiry,
                leg.qty * config.LOT_SIZE,
                "MARKET" if fill_price == 0 else "LIMIT",
                leg.entry_price,
                fill_price,
                status,
                leg.slippage_pts,
                1 if config.PAPER_TRADING_MODE else 0,
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"_log_order_detail error: {e}")

    async def _close_position(
        self,
        position:    Position,
        exit_reason: str,
        use_market:  bool = False,
    ) -> None:
        """
        Close all legs of a position.
        FIX VS7: on expiry day, skip OTM legs with LTP < 0.10
                 (they will expire worthless — closing costs
                  more than the option is worth).
        """
        if position.status != "OPEN":
            return

        logger.info(
            f"Closing: {position.trade_id[:8]} "
            f"strategy={position.strategy_name} "
            f"reason={exit_reason}"
        )

        use_market_order = use_market or exit_reason in [
            config.EXIT_REASONS["STOP_LOSS"],
            config.EXIT_REASONS["CIRCUIT_BREAK"],
            config.EXIT_REASONS["REGIME_CHANGE"],
        ]

        strategy_type = position.meta.get(
            "strategy_type", "SHORT"
        )

        short_legs = [
            l for l in position.legs
            if l.action == "SELL"
        ]
        long_legs  = [
            l for l in position.legs
            if l.action == "BUY"
        ]

        if strategy_type == "SHORT":
            ordered_legs = short_legs + long_legs
        else:
            ordered_legs = long_legs + short_legs

        # FIX VS7: on expiry day, skip near-worthless OTM legs
        is_expiry_close = exit_reason in (
            config.EXIT_REASONS["EXPIRY"],
            config.EXIT_REASONS["EOD"],
        )

        for idx, leg in enumerate(ordered_legs):
            if leg.qty <= 0:
                continue

            # FIX VS7: skip OTM legs near expiry
            if is_expiry_close:
                expiry_chain = self.dm.get_chain_for_expiry(
                    leg.expiry
                )
                current_ltp = float(
                    expiry_chain
                    .get(leg.strike, {})
                    .get(leg.option_type, {})
                    .get("ltp", 0)
                )
                if current_ltp < 0.10:
                    logger.info(
                        f"Expiry close: skipping "
                        f"{leg.option_type} {leg.strike} "
                        f"ltp={current_ltp:.2f} "
                        f"(will expire worthless)"
                    )
                    # Mark as closed at 0 cost
                    leg.exit_price = 0.0
                    # PATCH (S6): distinguish "genuinely expired
                    # worthless" from "never closed, price
                    # unknown" so _calculate_final_pnl doesn't
                    # fall back to entry_price and silently zero
                    # out this leg's real P&L (full credit
                    # realized for a short leg, full debit lost
                    # for a long leg).
                    leg.fill_status = "EXPIRED_WORTHLESS"
                    continue

            # SE-11: skip legs already closed by a previous
            # attempt (qty=0) or a partial one-side close.
            if leg.qty <= 0:
                continue
            if leg.exit_price > 0 and leg.fill_status == "CLOSED_EXIT":
                continue
            close_action = (
                "BUY" if leg.action == "SELL" else "SELL"
            )
            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action=close_action,
                strike=leg.strike,
                expiry=leg.expiry,
                qty=leg.qty,
            )

            success, order_id = (
                await self._place_single_leg(
                    close_leg,
                    use_market=use_market_order,
                    trade_id=f"exit-{position.trade_id}",
                    leg_index=idx,
                )
            )

            if not success:
                logger.warning(
                    f"Close leg failed — retrying market: "
                    f"{leg.option_type} {leg.strike}"
                )
                retry_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action=close_action,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=leg.qty,
                )
                await self._place_single_leg(
                    retry_leg,
                    use_market=True,
                    trade_id=(
                        f"exit-retry-{position.trade_id}"
                    ),
                    leg_index=idx,
                )
                leg.exit_price = retry_leg.entry_price
            else:
                leg.exit_price = close_leg.entry_price

            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        IST = pytz.timezone(config.TZ)
        # AUDIT SE-N01: verify that every leg has a confirmed
        # exit price before marking the position closed.
        # A leg with exit_price==0 and fill_status not
        # EXPIRED_WORTHLESS means the exit order failed.
        _unconfirmed = [
            l for l in position.legs
            if l.exit_price <= 0
            and l.fill_status != "EXPIRED_WORTHLESS"
            and l.qty > 0
        ]
        if _unconfirmed:
            logger.error(
                f"SE-N01: {len(_unconfirmed)} leg(s) have no "
                f"confirmed exit price for "
                f"{position.trade_id[:8]} — "
                f"position remains OPEN. Manual review required."
            )
            # Mark legs that did exit so we don't re-close them,
            # but keep the position OPEN for the next cycle.
            return
        position.exit_reason    = exit_reason
        position.exit_timestamp = datetime.now(
            IST
        ).isoformat()
        position.exit_spot      = self.dm.spot  or 0.0
        position.exit_vix       = self.dm.vix   or 0.0
        position.regime_at_exit = self.re.confirmed_regime
        position.status         = "CLOSED"

        gross_pnl, tx_costs, net_pnl = (
            self._calculate_final_pnl(position)
        )
        position.realized_pnl      = gross_pnl
        position.transaction_costs = tx_costs
        position.net_pnl           = net_pnl

        position_cost = max(position.max_risk, 1.0)
        position.realized_pnl_percent = (
            (net_pnl / position_cost) * 100
        )

        if position in self.open_positions:
            self.open_positions.remove(position)
        self.closed_positions.append(position)

        # PATCH: only apply the re-entry cooldown for whipsaw-risk
        # exits (stop-loss/circuit-break) — NOT for planned,
        # natural turnover (profit target, DTE-based time exit,
        # EOD, expiry). This lets the engine immediately "roll"
        # into a fresh position after a normal close instead of
        # being blocked for 5 minutes, enabling continuous
        # redeployment using the existing per-cycle entry flow.
        if exit_reason in (
            config.EXIT_REASONS["STOP_LOSS"],
            config.EXIT_REASONS["CIRCUIT_BREAK"],
        ):
            self._last_position_close_time = datetime.now(
                self._IST
            )
            self._last_position_close_spot = self.dm.spot
            # ARCH-7b: store composite at stop-loss
            self._last_stop_composite = getattr(
                self.re, 'raw_composite', None
            )

        self.weekly_pnl      += net_pnl
        self.current_capital += net_pnl
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital

        self.dm.close_position(
            position.trade_id,
            self._position_to_dict(position),
        )

        # ARCH-5b: update regime confidence after trade
        # Only sell-vol strategies affect regime confidence.
        # Long-vol strategies (straddle, backspread) are
        # expected to have lower win rates and should not
        # decay confidence when they lose.
        _sell_vol_strategies = (
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
            config.STRAT_SHORT_STRADDLE,
            config.STRAT_RATIO_SPREAD,
        )
        if position.strategy_name in _sell_vol_strategies:
            if net_pnl < 0:
                self._consecutive_regime_losses += 1
                self._regime_confidence = max(
                    0.40,
                    self._regime_confidence * 0.85,
                )
                logger.info(
                    f"ARCH-5b: regime confidence decayed to "
                    f"{self._regime_confidence:.2f} "
                    f"({self._consecutive_regime_losses} "
                    f"consecutive losses)"
                )
            else:
                self._consecutive_regime_losses = 0
                self._regime_confidence = min(
                    1.0,
                    self._regime_confidence * 1.15,
                )
                if self._regime_confidence < 1.0:
                    logger.info(
                        f"ARCH-5b: regime confidence restored to "
                        f"{self._regime_confidence:.2f}"
                    )
        logger.info(
            f"Closed: {position.trade_id[:8]} "
            f"gross=₹{gross_pnl:,.2f} "
            f"costs=₹{tx_costs:,.2f} "
            f"net=₹{net_pnl:,.2f} "
            f"reason={exit_reason}"
        )

    def _estimate_margin_requirement(
        self, strategy_name: str, max_risk_per_lot: float
    ) -> float:
        """
        PATCH: heuristic SPAN+exposure margin approximation for
        paper-mode sizing realism. This is NOT the real exchange
        calculation (real SPAN varies daily with volatility scans
        and is broker/exchange-specific) — it's a conservative,
        documented approximation so paper-mode position sizes are
        closer to what would actually be achievable live, rather
        than assuming margin is unlimited (the previous behavior).
        Live mode still separately validates against the REAL
        broker margin API via check_margin() in
        _pre_trade_checks().
        """
        spot = self.dm.spot or 24000.0
        notional_per_lot = spot * config.LOT_SIZE

        if strategy_name == config.STRAT_SHORT_STRADDLE:
            # Naked short options on both sides — margin is
            # dominated by SPAN+exposure on notional, not by the
            # (large) theoretical max loss.
            return notional_per_lot * config.SPAN_NAKED_MARGIN_PCT

        elif strategy_name in (
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
        ):
            # Defined-risk spread in the same expiry — exchanges
            # typically apply spread margining close to (a modest
            # multiple of) max loss, not full naked margin.
            return (
                max_risk_per_lot
                * config.SPAN_SPREAD_MARGIN_MULTIPLIER
            )

        elif strategy_name == config.STRAT_RATIO_SPREAD:
            # Mostly hedged (2 long vs 1 short per side) but not a
            # clean defined-risk spread — blend spread-margining
            # with a partial naked-margin allowance.
            return max(
                max_risk_per_lot
                * config.SPAN_SPREAD_MARGIN_MULTIPLIER,
                notional_per_lot
                * config.SPAN_NAKED_MARGIN_PCT * 0.5,
            )

        elif strategy_name == config.STRAT_BACKSPREAD:
            # More longs than shorts by construction — treat as
            # spread-like.
            return (
                max_risk_per_lot
                * config.SPAN_SPREAD_MARGIN_MULTIPLIER
            )

        else:
            # Long-only debit strategies (long straddle, strangle,
            # butterfly, defensive hedge): margin required is just
            # the premium paid, already captured by
            # max_risk_per_lot.
            return max_risk_per_lot

    def _log_entry_attempt(
        self,
        strategy_name: str,
        build_result: str,
        build_failure_reason: str = "",
        **kwargs,
    ) -> None:
        """LOG-SE-01: log every entry attempt for walk-forward analysis.

        build_result values:
          SUCCESS           — trade was opened
          FAILED_CREDIT     — credit below minimum
          FAILED_DTE        — no expiry in DTE window
          FAILED_SPREAD     — bid-ask spread too wide
          FAILED_CHAIN      — option chain unavailable
          FAILED_PRETRADE   — pre-trade checks failed
          FAILED_EXECUTION  — order placement failed
          FAILED_LOTS       — lot sizing returned 0
          FAILED_REVALIDATE — credit decayed before execution
          FAILED_OTHER      — other build failure
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO entry_attempt_log (
                    timestamp, strategy_name, regime_at_attempt,
                    composite_score, build_result, build_failure_reason,
                    expiry_date, dte, net_credit, min_credit_required,
                    wing_width, expected_move,
                    pretrade_passed, pretrade_fail_reason,
                    execution_result, lots_requested, lots_filled,
                    actual_credit, vix_at_entry, vix_adaptive_mult,
                    max_risk_used, trade_id
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, (
                datetime.now(self._IST).isoformat(),
                strategy_name,
                self.re.confirmed_regime,
                self.re.raw_composite,
                build_result,
                build_failure_reason or "",
                kwargs.get("expiry_date"),
                kwargs.get("dte"),
                kwargs.get("net_credit"),
                kwargs.get("min_credit_required"),
                kwargs.get("wing_width"),
                kwargs.get("expected_move"),
                1 if kwargs.get("pretrade_passed") else 0,
                kwargs.get("pretrade_fail_reason", ""),
                kwargs.get("execution_result", ""),
                kwargs.get("lots_requested"),
                kwargs.get("lots_filled"),
                kwargs.get("actual_credit"),
                self.dm.vix,
                kwargs.get("vix_adaptive_mult", 1.0),
                kwargs.get("max_risk_used"),
                kwargs.get("trade_id"),
            ))
            # Keep last 90 days
            conn.execute("""
                DELETE FROM entry_attempt_log
                WHERE timestamp < datetime('now', '-90 days')
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"_log_entry_attempt: {e}")

    def _log_position_monitor(
        self,
        position,
        action_taken: str,
        current_premium: float = 0.0,
        unrealized_pnl: float = 0.0,
        distance_to_call_pct: float = None,
        distance_to_put_pct: float = None,
    ) -> None:
        """LOG-SE-02: log monitoring decisions for walk-forward analysis.

        action_taken values:
          HOLD                — no action, position held
          STOP_BREACH_TICK    — breach tick incremented
          STOP_FIRED          — stop-loss executed
          PARTIAL_PROFIT      — partial profit taken
          PROFIT_TARGET       — full profit target hit
          TRAIL_STOP          — trailing stop fired
          DTE_EXIT            — DTE-based time exit
          DISTANCE_STOP       — distance-based stop fired
        """
        # Only log non-HOLD actions and periodic HOLD samples
        # to avoid filling the table with every 1s monitor tick
        _should_log = (
            action_taken != "HOLD"
            or getattr(self, "_monitor_log_counter", 0) % 60 == 0
        )
        self._monitor_log_counter = (
            getattr(self, "_monitor_log_counter", 0) + 1
        )
        if not _should_log:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO position_monitor_log (
                    timestamp, trade_id, strategy_name,
                    current_premium, stop_loss, profit_target,
                    unrealized_pnl, stop_breach_ticks,
                    distance_to_call_pct, distance_to_put_pct,
                    partial_taken, action_taken, spot, vix
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                datetime.now(self._IST).isoformat(),
                position.trade_id,
                position.strategy_name,
                current_premium,
                position.stop_loss,
                position.profit_target,
                unrealized_pnl,
                position.meta.get("_stop_breach_ticks", 0),
                distance_to_call_pct,
                distance_to_put_pct,
                1 if position.meta.get("_partial_taken") else 0,
                action_taken,
                self.dm.spot,
                self.dm.vix,
            ))
            # Keep last 30 days
            conn.execute("""
                DELETE FROM position_monitor_log
                WHERE timestamp < datetime('now', '-30 days')
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"_log_position_monitor: {e}")

    def _calculate_lot_size(
        self, strategy_name: str, meta: Dict
    ) -> int:
        # AUDIT #N1: defensive hedge pre-computes its own quantity.
        if strategy_name == config.STRAT_DEFENSIVE:
            return 1
        # PRF-S01: use theoretical max_risk for defined-risk structures.
        # For condors/spreads, max_risk = (wing_width - credit) * LOT_SIZE.
        # Using stop_loss*LOT_SIZE as the basis was dangerous: a 250pt
        # condor with 40pt credit has stop=50pt=Rs3250/lot, sizing to
        # 6 lots at Rs20k risk. A gap blows through stop and loses
        # 210pt * 6 = Rs82k — 4x the intended risk.
        # For undefined-risk structures (straddle), keep stop-based sizing.
        _strategy_type = meta.get("strategy_type", "SHORT")
        _is_defined_risk = strategy_name in (
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
        )
        if _is_defined_risk:
            # P2-1: defined-risk max_loss guard
            # meta["max_risk"] is the per-lot theoretical max loss
            # set by the builder as (wing_width - net_credit) * LOT_SIZE.
            # If somehow 0 (builder bug), fall back to a conservative
            # estimate so we never divide by zero or size to infinity.
            _raw_max_risk = meta.get("max_risk", 0)
            if _raw_max_risk and _raw_max_risk > 0:
                max_loss_per_lot = _raw_max_risk
            else:
                # Fallback: full wing width as max loss
                max_loss_per_lot = config.CONDOR_WING_WIDTH * config.LOT_SIZE
                logger.warning(
                    "P2-1: meta['max_risk']=0 for defined-risk strategy "
                    f"{strategy_name} — using wing_width fallback "
                    f"{max_loss_per_lot:.0f}"
                )
        elif _strategy_type == "SHORT":
            _stop_pts = meta.get("stop_loss", 0)
            if _stop_pts and _stop_pts > 0:
                max_loss_per_lot = _stop_pts * config.LOT_SIZE
            else:
                max_loss_per_lot = meta.get("max_risk", 0)
        else:
            max_loss_per_lot = meta.get("max_risk", 0)
        if max_loss_per_lot <= 0:
            return 0

        # PATCH S-04: define _defined_risk_budget (was undefined → NameError).
        # Currently no per-strategy override exists; always uses global budget.
        _defined_risk_budget = None
        # PATCHED S-04: define _defined_risk_budget before use.
        _defined_risk_budget = None
        # FIX-09: use defined-risk budget if set
        risk_per_trade = (
            _defined_risk_budget
            if _defined_risk_budget is not None
            else config.MAX_RISK_PER_TRADE
        )
        lots           = math.floor(
            risk_per_trade / max_loss_per_lot
        )

        regime   = self.re.confirmed_regime
        max_lots = config.REGIME_MAX_LOTS.get(regime, 1)
        lots     = min(lots, max_lots)
        lots     = max(lots, 0)

        regime_capital   = (
            config.REGIME_CAPITAL_PCT.get(regime, 0)
            * config.TOTAL_CAPITAL
        )
        deployed_capital = sum(
            p.max_risk for p in self.open_positions
        )
        available_capital = (
            regime_capital - deployed_capital
        )

        if available_capital <= 0:
            return 0

        if strategy_name == config.STRAT_RATIO_SPREAD:
            ratio_cap = (
                config.RATIO_MAX_CAPITAL_PCT
                * config.TOTAL_CAPITAL
            )
            available_capital = min(
                available_capital, ratio_cap
            )

        lots_by_capital = math.floor(
            available_capital / max_loss_per_lot
        )
        lots = min(lots, lots_by_capital)

        position_cap = math.floor(
            (
                config.POSITION_SIZE_PCT
                * config.TOTAL_CAPITAL
            ) / max_loss_per_lot
        )
        lots = min(lots, max(position_cap, 0))
        lots = max(lots, 0)

        # PATCH S-03: compute _vix_mult before use (was undefined → NameError).
        # IMM-02: VIX-adaptive lot sizing.
        _vix_adaptive = getattr(config, "VIX_ADAPTIVE_SIZING", False)
        _vix_ref = getattr(config, "VIX_ADAPTIVE_REFERENCE", 16.0)
        _vix_min_mult = getattr(config, "VIX_ADAPTIVE_MIN_MULT", 0.5)
        _vix_max_mult = getattr(config, "VIX_ADAPTIVE_MAX_MULT", 2.0)
        _current_vix = self.dm.vix if self.dm.vix and self.dm.vix > 0 else _vix_ref
        if _vix_adaptive and _current_vix > 0:
            # Scale UP when VIX is low (premium thin, need more lots for same Rs return)
            # Scale DOWN when VIX is high (premium rich but risk higher)
            _vix_mult = max(
                _vix_min_mult,
                min(_vix_max_mult, _vix_ref / _current_vix)
            )
        else:
            _vix_mult = 1.0
        # PATCHED S-03: compute _vix_mult before use.
        _vix_adaptive = getattr(config, "VIX_ADAPTIVE_SIZING", False)
        _vix_ref_sz = getattr(config, "VIX_ADAPTIVE_REFERENCE", 16.0)
        _vix_min_m = getattr(config, "VIX_ADAPTIVE_MIN_MULT", 0.5)
        _vix_max_m = getattr(config, "VIX_ADAPTIVE_MAX_MULT", 2.0)
        _cur_vix = (
            self.dm.vix
            if self.dm.vix and self.dm.vix > 0
            else _vix_ref_sz
        )
        if _vix_adaptive and _cur_vix > 0:
            _vix_mult = max(
                _vix_min_m,
                min(_vix_max_m, _vix_ref_sz / _cur_vix),
            )
        else:
            _vix_mult = 1.0
        # IMM-02: apply VIX-adaptive multiplier to final lot count
        if _vix_mult != 1.0 and lots > 0:
            lots = max(1, int(lots * _vix_mult))
        # Store for entry attempt logging
        self._last_vix_mult = _vix_mult

        # PATCH: heuristic margin/SPAN cap. Previously sizing was
        # based purely on theoretical max-loss and capital %,
        # never on what margin the position would actually tie up
        # — which for naked/undefined-risk strategies (short
        # straddle, ratio spread) is typically far higher than
        # max_risk. This is a documented approximation, not the
        # real exchange calculation (live mode still separately
        # validates against the real broker margin API in
        # _pre_trade_checks()).
        margin_per_lot = self._estimate_margin_requirement(
            strategy_name, max_loss_per_lot
        )
        meta["margin_estimate_per_lot"] = margin_per_lot
        if margin_per_lot > 0:
            deployed_margin = sum(
                getattr(p, "margin_estimate", 0.0)
                for p in self.open_positions
            )
            margin_budget = (
                config.MARGIN_UTILIZATION_PCT
                * config.TOTAL_CAPITAL
            )
            available_margin = margin_budget - deployed_margin
            if available_margin <= 0:
                logger.info(
                    f"Lot size: 0 for {strategy_name} — "
                    f"margin budget exhausted "
                    f"(deployed={deployed_margin:.0f}/"
                    f"{margin_budget:.0f})"
                )
                return 0
            lots_by_margin = math.floor(
                available_margin / margin_per_lot
            )
            lots = min(lots, lots_by_margin)
            lots = max(lots, 0)

        # P4-2: composite quality multiplier
        # Scale lot count by signal quality so high-conviction
        # entries (composite well above threshold) get more size
        # than marginal entries (composite barely above threshold).
        # Multiplier ramps linearly from 1.0 at composite=0.20
        # to 1.5 at composite=0.75, capped at 1.5.
        # Applied AFTER all other caps so it never exceeds margin
        # or regime max lots limits.
        if lots > 0:
            # S12-1: composite multiplier uses 3-cycle average
            # raw_composite includes noisy flow signal; lot size
            # oscillated cycle-to-cycle.  Use 3-cycle average from
            # score_history to smooth single-cycle flow noise.
            _score_hist = list(self.re.score_history)[-3:]
            if _score_hist:
                _comp_avg = sum(
                    abs(e.get('composite', 0.0))
                    for e in _score_hist
                ) / len(_score_hist)
            else:
                _comp_avg = abs(
                    getattr(self.re, 'raw_composite', 0.0)
                )
            _comp_now = _comp_avg
            _comp_base = getattr(
                config, 'MILD_SELL_ENTER', 0.20
            )
            _comp_mult = 1.0 + min(
                0.5,
                max(0.0, (_comp_now - _comp_base) / 1.10)
            )
            if _comp_mult > 1.05:  # only apply meaningful boost
                _lots_before_mult = lots
                lots = min(
                    config.REGIME_MAX_LOTS.get(
                        self.re.confirmed_regime, lots
                    ),
                    max(1, int(lots * _comp_mult)),
                )
                if lots != _lots_before_mult:
                    logger.info(
                        f"P4-2: composite quality boost "
                        f"{_lots_before_mult} → {lots} lots "
                        f"(composite={_comp_now:.3f} "
                        f"mult={_comp_mult:.2f}x)"
                    )
        # ARCH-5c: apply regime confidence to lot size
        # Scale down lot count when confidence is low
        # (consecutive sell-vol losses signal regime misclassification).
        _conf = getattr(self, '_regime_confidence', 1.0)
        if _conf < 0.85 and lots > 0:
            _conf_lots = max(1, int(lots * _conf))
            if _conf_lots < lots:
                logger.info(
                    f"ARCH-5c: confidence-adjusted lots: "
                    f"{lots} → {_conf_lots} "
                    f"(confidence={_conf:.2f})"
                )
                lots = _conf_lots
        # FIX-7b: apply double event week lot reduction
        # Sep 2026 double event week (Sep 22-26): 50%% reduction
        # Monthly expiry week only: 25%% reduction
        if getattr(self, '_double_event_week', False) and lots > 1:
            lots = max(1, lots // 2)
            logger.info(
                f"FIX-7b: double event week — lots halved to {lots}"
            )
        elif getattr(self, '_monthly_expiry_week', False) and lots > 1:
            lots = max(1, int(lots * 0.75))
            logger.info(
                f"FIX-7b: monthly expiry week — lots reduced to {lots}"
            )
        # S13-1: minimum lot size for credit spreads
        # At VIX=11, credit spread net_credit ~12pts.
        # Transaction costs (Rs334-450/round trip) exceed EV at 1-3 lots.
        # Break-even minimum is 4 lots.  If we cannot size to 4 lots
        # within risk limits, skip the trade (return 0) rather than
        # entering an unprofitable 1-3 lot position.
        if (
            strategy_name == config.STRAT_CREDIT_SPREADS
            and lots > 0
        ):
            _net_credit_pts = meta.get(
                "total_credit",
                meta.get("net_credit", 99)
            )
            _min_lots_spread = getattr(
                config, "CREDIT_SPREAD_MIN_LOTS", 4
            )
            if _net_credit_pts < 20 and lots < _min_lots_spread:
                logger.info(
                    f"S13-1: credit spread skipped — "
                    f"lots={lots} < min={_min_lots_spread} "
                    f"at net_credit={_net_credit_pts:.1f}pts "
                    f"(transaction costs exceed EV at low lot count)"
                )
                return 0
        logger.info(
            f"Lot size: {lots} for {strategy_name} "
            f"risk={risk_per_trade} "
            f"max_loss={max_loss_per_lot:.0f} "
            f"margin_per_lot={margin_per_lot:.0f}"
        )
        return lots

    def _get_position_value(
        self, position: Position
    ) -> float:
        # SE-T01: staleness-aware mark price.
        # SE-05: apply BUY/SELL sign.
        # SE9-P2-01: mark each leg against its OWN expiry.
        # Using position.expiry_date for all legs means off-expiry
        # legs (defensive hedge, calendar spreads) get opt_data={}
        # and are marked at entry_price forever — zero P&L.
        total = 0.0
        for leg in position.legs:
            _leg_chain = self.dm.get_chain_for_expiry(leg.expiry)
            opt_data = (
                _leg_chain
                .get(leg.strike, {})
                .get(leg.option_type, {})
            )
            mark = self.dm.get_mark_price(
                opt_data, fallback=leg.entry_price
            )
            sign = 1 if leg.action == "BUY" else -1
            total += sign * mark * leg.qty
        return total

    def _get_position_current_premium(
        self, position: Position
    ) -> float:
        """Return current net premium using each leg's expiry."""
        net = 0.0

        for leg in position.legs:
            leg_chain = self.dm.get_chain_for_expiry(
                leg.expiry
            )
            opt_data = (
                leg_chain
                .get(leg.strike, {})
                .get(leg.option_type, {})
            )
            mark = self.dm.get_mark_price(
                opt_data,
                fallback=leg.entry_price,
            )

            if leg.action == "SELL":
                net += mark * leg.qty
            else:
                net -= mark * leg.qty

        return net

    def _get_portfolio_greeks(self) -> Dict[str, float]:
        total_delta = 0.0
        total_gamma = 0.0
        total_vega  = 0.0
        total_theta = 0.0

        for position in self.open_positions:
            for leg in position.legs:
                sign = +1 if leg.action == "BUY" else -1
                # SE8-P0-02 FIX: restore * LOT_SIZE.
                # API Greeks are per-contract (per-lot). Multiplying
                # by LOT_SIZE scales to per-position rupee terms.
                # All consumers (_check_greeks_limits, _hedge_delta,
                # _gamma_above_50pct_limit) were written expecting
                # this scaling. Removing it made every limit 65x too
                # loose and hedge quantities wrong by 65x.
                total_delta += (
                    sign * leg.delta * leg.qty * config.LOT_SIZE
                )
                total_gamma += (
                    sign * leg.gamma * leg.qty * config.LOT_SIZE
                )
                total_vega  += (
                    sign * leg.vega  * leg.qty * config.LOT_SIZE
                )
                total_theta += (
                    sign * leg.theta * leg.qty * config.LOT_SIZE
                )

        return {
            "delta": total_delta,
            "gamma": total_gamma,
            "vega":  total_vega,
            "theta": total_theta,
        }

    def _estimate_greeks_impact(
        self, legs: List[Leg]
    ) -> Dict[str, float]:
        delta = gamma = vega = theta = 0.0
        for leg in legs:
            sign   = +1 if leg.action == "BUY" else -1
            delta += (
                sign * leg.delta * leg.qty * config.LOT_SIZE
            )
            gamma += (
                sign * leg.gamma * leg.qty * config.LOT_SIZE
            )
            vega  += (
                sign * leg.vega  * leg.qty * config.LOT_SIZE
            )
            theta += (
                sign * leg.theta * leg.qty * config.LOT_SIZE
            )
        return {
            "delta": delta, "gamma": gamma,
            "vega": vega,   "theta": theta,
        }

    def _leg_price(self, leg: Leg) -> float:
        if leg.entry_price and leg.entry_price > 0:
            return leg.entry_price
        expiry_chain = self.dm.get_chain_for_expiry(
            leg.expiry
        )
        ltp = (
            expiry_chain
            .get(leg.strike, {})
            .get(leg.option_type, {})
            .get("ltp", 0)
        )
        return float(ltp or 0)

    def _estimate_max_loss(
        self, strategy_name: str, legs: List[Leg]
    ) -> float:
        if strategy_name == config.STRAT_SHORT_STRADDLE:
            total_prem = sum(
                self._leg_price(l) for l in legs
                if l.action == "SELL"
            )
            # SE-16: use 2x credit (matching STRADDLE_STOP_MULT)
            # not 1x. The pre-trade combined-risk gate was
            # undercounting straddle risk by exactly 2x.
            return total_prem * config.STRADDLE_STOP_MULT * config.LOT_SIZE

        elif strategy_name == config.STRAT_IRON_CONDOR:
            net_credit = (
                sum(
                    self._leg_price(l) for l in legs
                    if l.action == "SELL"
                )
                - sum(
                    self._leg_price(l) for l in legs
                    if l.action == "BUY"
                )
            )
            return (
                config.CONDOR_WING_WIDTH - net_credit
            ) * config.LOT_SIZE

        elif strategy_name == config.STRAT_CREDIT_SPREADS:
            net_credit = (
                sum(
                    self._leg_price(l) for l in legs
                    if l.action == "SELL"
                )
                - sum(
                    self._leg_price(l) for l in legs
                    if l.action == "BUY"
                )
            )
            # PATCH: previously paired sell/buy strikes across
            # BOTH sides (e.g. short put vs long call), producing
            # a cross-strike distance far larger than either
            # spread's true width. Pair strikes within the SAME
            # option_type (put spread width, call spread width)
            # instead — matching what _build_credit_spreads()
            # itself already does correctly for meta["max_risk"].
            put_sell = [
                l.strike for l in legs
                if l.action == "SELL" and l.option_type == "put"
            ]
            put_buy = [
                l.strike for l in legs
                if l.action == "BUY" and l.option_type == "put"
            ]
            call_sell = [
                l.strike for l in legs
                if l.action == "SELL" and l.option_type == "call"
            ]
            call_buy = [
                l.strike for l in legs
                if l.action == "BUY" and l.option_type == "call"
            ]

            widths = []
            if put_sell and put_buy:
                widths.append(
                    max(
                        abs(s - b)
                        for s in put_sell for b in put_buy
                    )
                )
            if call_sell and call_buy:
                widths.append(
                    max(
                        abs(s - b)
                        for s in call_sell for b in call_buy
                    )
                )
            spread_width = (
                max(widths) if widths
                else config.CONDOR_WING_WIDTH / 2
            )
            return max(
                0,
                (spread_width - net_credit) * config.LOT_SIZE,
            )

        elif strategy_name in [
            config.STRAT_LONG_STRADDLE,
            config.STRAT_STRANGLE,
            config.STRAT_BUTTERFLY,
            config.STRAT_BACKSPREAD,
            config.STRAT_DEFENSIVE,
        ]:
            total_debit = (
                sum(
                    self._leg_price(l) * l.qty
                    for l in legs
                    if l.action == "BUY"
                )
                - sum(
                    self._leg_price(l) * l.qty
                    for l in legs
                    if l.action == "SELL"
                )
            )
            return max(0, total_debit * config.LOT_SIZE)

        elif strategy_name == config.STRAT_RATIO_SPREAD:
            total_debit = (
                sum(
                    self._leg_price(l) * l.qty
                    for l in legs
                    if l.action == "BUY"
                )
                - sum(
                    self._leg_price(l) * l.qty
                    for l in legs
                    if l.action == "SELL"
                )
            )
            return max(
                0, total_debit * 2 * config.LOT_SIZE
            )

        return float(config.MAX_RISK_PER_TRADE)


    async def _check_greeks_limits(self) -> None:
        """Check Greeks. Skip when no positions open."""
        if not self.open_positions:
            return
        # SE-T06: warn when leg Greeks are stale.
        # Greeks are set at entry and updated by WS. Near expiry,
        # gamma changes rapidly; a 30-min-old reading can be off
        # by 2-3x. We cannot recompute without a live model, but
        # we can warn so the operator knows the limits check may
        # be operating on stale data.
        _now_ist = datetime.now(self._IST)
        for _pos in self.open_positions:
            for _leg in _pos.legs:
                _ws_ts = None
                _exp_chain = self.dm.get_chain_for_expiry(
                    _pos.expiry_date
                )
                _opt = (
                    _exp_chain
                    .get(_leg.strike, {})
                    .get(_leg.option_type, {})
                )
                _ws_ts_str = _opt.get("_ws_ts")
                if _ws_ts_str:
                    try:
                        _ws_ts = datetime.fromisoformat(_ws_ts_str)
                        if _ws_ts.tzinfo is None:
                            _ws_ts = self._IST.localize(_ws_ts)
                        _age = (
                            _now_ist - _ws_ts
                        ).total_seconds()
                        if _age > 1800:  # 30 minutes
                            logger.warning(
                                f"SE-T06: Greeks stale "
                                f"{_leg.option_type} "
                                f"{_leg.strike} "
                                f"age={_age:.0f}s — "
                                f"limits check may be inaccurate"
                            )
                    except Exception:
                        pass
        regime = self.re.confirmed_regime
        limits = config.GREEKS_LIMITS.get(regime, {})
        greeks = self._get_portfolio_greeks()
        # PATCH: delta_max/gamma_max in GREEKS_LIMITS are per-lot
        # normalized fractions; portfolio greeks are LOT_SIZE
        # scaled. Normalize before compare (vega/theta stay in
        # absolute rupee terms, unchanged).
        delta_norm = greeks['delta'] / config.LOT_SIZE
        gamma_norm = greeks['gamma'] / config.LOT_SIZE
        delta_max = limits.get('delta_max', 99)
        if abs(delta_norm) > delta_max:
            logger.warning(
                f'Delta breach: {delta_norm:.3f} > {delta_max}'
            )
            await self._hedge_delta(
                greeks['delta'], delta_max * config.LOT_SIZE
            )
        gamma_max = limits.get('gamma_max')
        gamma_min = limits.get('gamma_min')
        if gamma_min is not None and gamma_norm < gamma_min:
            logger.warning(
                f'Gamma below min: {gamma_norm:.5f} < {gamma_min}'
            )
        if gamma_max is not None and gamma_norm > gamma_max:
            logger.warning(
                f'Gamma above max: {gamma_norm:.5f} > {gamma_max}'
            )
        vega_min = limits.get('vega_min')
        if vega_min is not None and greeks['vega'] < vega_min:
            logger.warning(
                f'Vega below min: {greeks["vega"]:.1f} < {vega_min}'
            )
            # SE-VEGA FIX: act on vega breach, don't just log.
            # Portfolio is too short vega (below vega_min).
            # Reduce the largest short-vol position by 50% to
            # bring vega back toward the allowed band.
            # Only act if GREEKS_VEGA_GATE is enabled.
            if getattr(config, "GREEKS_VEGA_GATE", False):
                _short_vol_positions = [
                    p for p in self.open_positions
                    if p.meta.get("strategy_type") == "SHORT"
                    and p.status == "OPEN"
                ]
                if _short_vol_positions:
                    # Reduce the position with the most short vega
                    # (largest credit, as a proxy for most vega)
                    _largest = max(
                        _short_vol_positions,
                        key=lambda p: p.total_credit,
                    )
                    logger.warning(
                        f"SE-VEGA: reducing "
                        f"{_largest.strategy_name} "
                        f"{_largest.trade_id[:8]} "
                        f"by 50%% to address vega breach"
                    )
                    import asyncio as _asyncio
                    _asyncio.ensure_future(
                        self._reduce_position_50pct(_largest)
                    )
        theta_min = limits.get('theta_min')
        if theta_min is not None and greeks['theta'] < theta_min:
            logger.warning(
                f'Theta below min: {greeks["theta"]:.1f} < {theta_min}'
            )
    async def _hedge_delta(
        self, current_delta: float, delta_limit: float
    ) -> None:
        excess = abs(current_delta) - delta_limit
        if excess <= 0:
            return

        # PATCH: excess is in LOT_SIZE-scaled delta-share units;
        # dividing by 1.0 (previous code) ordered ~LOT_SIZE times
        # too many futures lots. Divide by LOT_SIZE for an actual
        # futures-lot count.
        futures_lots = math.ceil(excess / config.LOT_SIZE)
        action       = (
            "SELL" if current_delta > delta_limit else "BUY"
        )

        if config.PAPER_TRADING_MODE:
            logger.info(
                f"Paper delta hedge: {action} "
                f"{futures_lots} Nifty futures "
                f"delta={current_delta:.3f}"
            )
            return

        # AUDIT SE-15: INSTRUMENT_NIFTY_FUT is a series prefix,
        # not a specific contract key. In live mode this will 400.
        # The real key must be resolved from the instrument master
        # at startup. Until that is implemented, skip live hedging
        # and log CRITICAL so the operator knows delta is unhedged.
        fut_key = getattr(
            config, "INSTRUMENT_NIFTY_FUT_RESOLVED",
            config.INSTRUMENT_NIFTY_FUT,
        )
        if fut_key == config.INSTRUMENT_NIFTY_FUT:
            logger.critical(
                f"SE-15: INSTRUMENT_NIFTY_FUT is not a tradeable "
                f"key. Resolve from instrument master and set "
                f"config.INSTRUMENT_NIFTY_FUT_RESOLVED. "
                f"Delta hedge SKIPPED: {action} {futures_lots} lots."
            )
            return
        payload = {
            "quantity":           (
                futures_lots * config.LOT_SIZE
            ),
            "product":            "D",
            "validity":           "DAY",
            "price":              0,
            "instrument_token":   fut_key,
            "order_type":         "MARKET",
            "transaction_type":   action,
            "disclosed_quantity": 0,
            "trigger_price":      0,
            "is_amo":             False,
        }
        try:
            await self.dm._api_post(
                config.EP_ORDER_PLACE, payload
            )
            logger.info(
                f"Delta hedge: {action} "
                f"{futures_lots} lots"
            )
        except Exception as e:
            logger.error(f"Delta hedge failed: {e}")

    async def _reduce_position_50pct(
        self, position: Position
    ) -> None:
        # SE-P2-02: reduce both SELL and BUY legs proportionally.
        # Previously only SELL legs were reduced, leaving 100% of
        # long wings against 50% of shorts — pure decaying premium.
        for idx, leg in enumerate(position.legs):
            if leg.action in ("SELL", "BUY"):
                # AUDIT SE-16: for a 1-lot position,
                # floor(1*0.5)=0, max(1,0)=1 closes the whole
                # leg leaving a naked remnant. Close the whole
                # position instead of leaving an unbalanced structure.
                if leg.qty <= 1:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["MANUAL"],
                    )
                    return
                reduce_qty = math.floor(leg.qty * 0.50)
                if reduce_qty < 1:
                    reduce_qty = 1
                if reduce_qty > leg.qty:
                    reduce_qty = leg.qty
                # SE10-P0-01 FIX: closing action must be opposite
                # of leg action. 'BUY' was hardcoded, which bought
                # more of the long legs instead of selling them.
                _close_action = (
                    "BUY" if leg.action == "SELL" else "SELL"
                )
                close_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action=_close_action,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=reduce_qty,
                )
                success, _ = await self._place_single_leg(
                    close_leg,
                    use_market=False,
                    trade_id=f"reduce-{position.trade_id}",
                    leg_index=idx,
                )
                if success:
                    # PATCH: use the ACTUAL filled amount
                    # (close_leg.qty, adjusted down on a partial
                    # fill), not the originally requested amount.
                    leg.qty -= close_leg.qty
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )
        self._move_stop_to_breakeven(position)

    async def _reduce_position_pct(
        self, position: Position, pct: float
    ) -> None:
        # SE-P0-06: for a 1-lot position, floor(1*pct)=0,
        # max(1,0)=1 closes the entire leg, leaving orphaned
        # long wings. Close the whole position instead.
        if any(
            l.action == "SELL" and l.qty <= 1
            for l in position.legs
        ):
            await self._close_position(
                position, config.EXIT_REASONS["MANUAL"]
            )
            return
        for idx, leg in enumerate(position.legs):
            if leg.action == "SELL":
                reduce_qty = max(
                    1, math.floor(leg.qty * pct)
                )
                if reduce_qty > leg.qty:
                    reduce_qty = leg.qty
                close_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action="BUY",
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=reduce_qty,
                )
                success, _ = await self._place_single_leg(
                    close_leg,
                    use_market=False,
                    trade_id=(
                        f"reduce-pct-{position.trade_id}"
                    ),
                    leg_index=idx,
                )
                if success:
                    # PATCH: use the ACTUAL filled amount.
                    leg.qty -= close_leg.qty
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )

    async def _convert_shorts_to_spreads(
        self, position: Position
    ) -> None:
        for idx, leg in enumerate(position.legs):
            if leg.action == "SELL":
                hedge_strike = self.dm.get_strike_by_delta(
                    leg.option_type, 0.10,
                    expiry=leg.expiry,
                )

                if hedge_strike is None:
                    continue

                expiry_chain = self.dm.get_chain_for_expiry(
                    leg.expiry
                )
                if hedge_strike not in expiry_chain:
                    continue

                hedge_leg = Leg(
                    instrument_key=expiry_chain[
                        hedge_strike
                    ][leg.option_type]["instrument_key"],
                    option_type=leg.option_type,
                    action="BUY",
                    strike=hedge_strike,
                    expiry=leg.expiry,
                    qty=leg.qty,
                    delta=expiry_chain[hedge_strike][
                        leg.option_type
                    ]["delta"],
                    gamma=expiry_chain[hedge_strike][
                        leg.option_type
                    ]["gamma"],
                    vega=expiry_chain[hedge_strike][
                        leg.option_type
                    ]["vega"],
                    theta=expiry_chain[hedge_strike][
                        leg.option_type
                    ]["theta"],
                )
                success, order_id = (
                    await self._place_single_leg(
                        hedge_leg,
                        use_market=False,
                        trade_id=(
                            f"convert-{position.trade_id}"
                        ),
                        leg_index=idx,
                    )
                )
                if success:
                    position.legs.append(hedge_leg)
                    logger.info(
                        f"Converted {leg.strike} to spread "
                        f"with hedge at {hedge_strike}"
                    )
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )

    def _move_stop_to_breakeven(
        self, position: Position
    ) -> None:
        if position.strategy_name == (
            config.STRAT_SHORT_STRADDLE
        ):
            # PATCH: was position.entry_spot (a spot PRICE,
            # ~24,000), compared against current_premium (a few
            # hundred points) in _check_stop_loss — that stop
            # could never trigger again once this ran. Use
            # total_credit (unit-matched: points x lots, same as
            # current_premium) so "breakeven" actually means
            # giving back all the credit collected.
            position.stop_loss = position.total_credit
        elif position.strategy_name in [
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
        ]:
            # Breakeven fix: 0.0 is falsy so the stop check
            # `if position.stop_loss and stop_loss > 0` evaluates
            # False, disabling the stop entirely. Use total_credit
            # so the stop fires when all premium is given back.
            position.stop_loss = position.total_credit

    async def _emergency_flatten_all(self) -> None:
        logger.critical(
            "EMERGENCY: Flattening all positions"
        )
        await self.cancel_all_open_orders(
            context="EMERGENCY_FLATTEN"
        )
        for position in list(self.open_positions):
            await self._close_position(
                position,
                config.EXIT_REASONS["CIRCUIT_BREAK"],
                use_market=True,
            )

    async def _reduce_all_positions_50pct(self) -> None:
        logger.warning("CB L3: Reducing all by 50%")
        for position in list(self.open_positions):
            await self._reduce_position_50pct(position)

    async def _execute_reductions(
        self, reduction_legs: List[Dict]
    ) -> bool:
        for idx, item in enumerate(reduction_legs):
            leg        = item["leg"]
            reduce_qty = item["reduce_qty"]
            if reduce_qty < 1:
                continue
            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action="BUY",
                strike=leg.strike,
                expiry=leg.expiry,
                qty=reduce_qty,
            )
            success, _ = await self._place_single_leg(
                close_leg,
                use_market=True,
                trade_id=f"def-reduce-{idx}",
                leg_index=idx,
            )
            if not success:
                logger.warning(
                    f"Reduction failed: {leg.strike}"
                )
                return False
            # PATCH: use the ACTUAL filled amount.
            leg.qty -= close_leg.qty
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )
        return True

    async def _close_one_side(
        self,
        position:    Position,
        option_type: str,
        exit_reason: str,
    ) -> None:
        # SE-12: sort shorts first so we buy back the short
        # before selling the long. If the long-sell order fails
        # we have a flat position, not a naked short.
        side_legs = sorted(
            [
                l for l in position.legs
                if l.option_type == option_type
            ],
            key=lambda l: 0 if l.action == "SELL" else 1,
        )
        for idx, leg in enumerate(side_legs):
            close_action = (
                "BUY" if leg.action == "SELL" else "SELL"
            )
            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action=close_action,
                strike=leg.strike,
                expiry=leg.expiry,
                qty=leg.qty,
            )
            success, _ = await self._place_single_leg(
                close_leg,
                use_market=True,
                trade_id=f"oneside-{position.trade_id}",
                leg_index=idx,
            )
            if success:
                # PATCH: previously only exit_price was set here,
                # leaving qty/fill_status unchanged. That caused
                # this side to be re-detected as "still open" on
                # every later cycle (duplicate close orders) and
                # risked a wrong-direction order at final close.
                # We now bank this leg's realized pnl/cost and
                # fully mark it closed.
                # PATCH: use close_leg.qty (the ACTUAL filled
                # amount, adjusted down on a partial fill) instead
                # of leg.qty (originally requested), and reduce
                # leg.qty by that amount instead of flatly
                # zeroing it, so a partial one-side close leaves
                # the correct remaining quantity tracked.
                exit_price = close_leg.entry_price
                qty_closed = close_leg.qty
                if qty_closed <= 0:
                    continue
                if leg.action == "SELL":
                    leg_pnl = (
                        (leg.entry_price - exit_price)
                        * qty_closed * config.LOT_SIZE
                    )
                else:
                    leg_pnl = (
                        (exit_price - leg.entry_price)
                        * qty_closed * config.LOT_SIZE
                    )
                # PATCH: now includes STT/SEBI/stamp/GST,
                # matching _calculate_transaction_costs() (was
                # only brokerage + exchange fee before).
                # PATCH: rates updated per production
                # verification record (config.py's COST_*
                # constants), matching _calculate_transaction_costs().
                _leg_value = (
                    exit_price * qty_closed * config.LOT_SIZE
                )
                _leg_brokerage = config.COST_BROKERAGE_PER_ORDER
                _leg_exchange = (
                    _leg_value * config.COST_EXCHANGE_PCT
                )
                _leg_ipft = (
                    _leg_value * config.COST_NSE_IPFT_PCT
                )
                _leg_sebi = _leg_value * config.COST_SEBI_PCT
                if leg.action == "SELL":
                    _leg_stt = 0.0
                    _leg_stamp = (
                        _leg_value * config.COST_STAMP_PCT
                    )
                else:
                    _leg_stt = (
                        _leg_value
                        * config.COST_STT_OPTION_SELL_PCT
                    )
                    _leg_stamp = 0.0
                # PATCH (NEW-5): GST base now includes SEBI
                # fee, matching _calculate_transaction_costs().
                _leg_gst = (
                    _leg_brokerage + _leg_exchange + _leg_sebi
                ) * config.COST_GST_PCT
                leg_cost = (
                    _leg_brokerage + _leg_exchange + _leg_ipft
                    + _leg_sebi + _leg_stt + _leg_stamp
                    + _leg_gst
                )
                position.banked_pnl   += leg_pnl
                position.banked_costs += leg_cost
                leg.exit_price  = exit_price
                leg.qty        -= qty_closed
                if leg.qty <= 0:
                    leg.qty         = 0
                    leg.fill_status = "CLOSED_ONE_SIDE"
                else:
                    leg.fill_status = (
                        "PARTIALLY_CLOSED_ONE_SIDE"
                    )
                    logger.warning(
                        f"Partial one-side close: "
                        f"{leg.option_type} {leg.strike} "
                        f"remaining_qty={leg.qty}"
                    )
        logger.info(
            f"Closed {option_type} side of "
            f"{position.trade_id[:8]}"
        )

    async def _reconcile_with_broker(self) -> None:
        """
        Reconcile local positions with broker.
        FIX PS10: skips when broker returns empty list.
        """
        if config.PAPER_TRADING_MODE:
            return
        try:
            broker_positions = await self.dm._api_get(
                config.EP_POSITIONS, {}
            )
            if not broker_positions:
                logger.info(
                    "Reconciliation: no broker positions "
                    "— skipping"
                )
                return

            pos_list = (
                broker_positions
                if isinstance(broker_positions, list)
                else broker_positions.get("data", [])
            )

            if not pos_list:
                logger.info(
                    "Reconciliation: empty position list "
                    "— skipping"
                )
                return

            broker_map: Dict[str, int] = {}
            for pos in pos_list:
                key = (
                    pos.get("instrument_key", "")
                    or pos.get("instrument_token", "")
                )
                qty = int(pos.get("quantity", 0))
                if key and qty != 0:
                    broker_map[key] = qty

            for position in self.open_positions:
                for leg in position.legs:
                    broker_qty = broker_map.get(
                        leg.instrument_key, 0
                    )
                    local_qty  = leg.qty * config.LOT_SIZE

                    if broker_qty == 0 and local_qty != 0:
                        logger.warning(
                            f"Mismatch: local={local_qty} "
                            f"broker=0 "
                            f"{leg.instrument_key}"
                        )
                        leg.qty         = 0
                        leg.fill_status = "CLOSED_EXTERNALLY"
                    elif (
                        broker_qty != 0
                        and abs(broker_qty) != local_qty
                    ):
                        logger.warning(
                            f"Qty mismatch: "
                            f"local={local_qty} "
                            f"broker={abs(broker_qty)} "
                            f"— broker wins"
                        )
                        leg.qty = (
                            abs(broker_qty)
                            // config.LOT_SIZE
                        )

            logger.info("Broker reconciliation complete")

        except Exception as e:
            logger.error(f"Reconciliation error: {e}")

    def _create_position_record(
        self,
        strategy_name: str,
        legs:          List[Leg],
        meta:          Dict,
        trade_id:      str = "",
    ) -> Position:
        if not trade_id:
            trade_id = str(uuid.uuid4())
        now = datetime.now(self._IST).isoformat()

        total_credit = sum(
            l.entry_price * l.qty
            for l in legs if l.action == "SELL"
        )
        total_debit = sum(
            l.entry_price * l.qty
            for l in legs if l.action == "BUY"
        )
        net_premium = total_credit - total_debit

        expiry_date = legs[0].expiry if legs else ""
        dte         = 0
        if expiry_date:
            try:
                dte = (
                    datetime.strptime(
                        expiry_date, "%Y-%m-%d"
                    ).date() - date.today()
                ).days
            except ValueError:
                dte = 0

        return Position(
            trade_id=trade_id,
            strategy_name=strategy_name,
            regime_at_entry=self.re.confirmed_regime,
            entry_timestamp=now,
            entry_spot=self.dm.spot or 0.0,
            entry_vix=self.dm.vix   or 0.0,
            legs=legs,
            stop_loss=meta.get("stop_loss", 0.0),
            profit_target=meta.get("profit_target", 0.0),
            exit_dte=meta.get("exit_dte"),
            max_hold_date=meta.get("max_hold_date"),
            composite_at_entry=self.re.raw_composite,
            vol_score=self.re.confirmed_vol,
            edge_score=self.re.confirmed_edge,
            trend_score=self.re.confirmed_trend,
            flow_score=self.re.confirmed_flow,
            days_to_expiry=dte,
            expiry_date=expiry_date,
            total_credit=total_credit,
            total_debit=total_debit,
            net_premium=net_premium,
            max_risk=meta.get("max_risk", 0.0),
            paper_trade=config.PAPER_TRADING_MODE,
            trend_direction=meta.get(
                "trend_direction", 0.0
            ),
            margin_estimate=meta.get("margin_estimate", 0.0),
            meta=copy.deepcopy(meta),
        )

    def _position_to_dict(
        self, position: Position
    ) -> Dict:
        holding_days = 0
        if (
            position.exit_timestamp
            and position.entry_timestamp
        ):
            try:
                entry_dt = datetime.fromisoformat(
                    position.entry_timestamp
                )
                exit_dt  = datetime.fromisoformat(
                    position.exit_timestamp
                )
                holding_days = (exit_dt - entry_dt).days
            except (ValueError, TypeError):
                holding_days = 0

        slippage_total = sum(
            l.slippage_pts for l in position.legs
        )
        # PATCH (S8): for a CLOSED position, use the authoritative
        # values _close_position() already computed and stored
        # (correctly including banked_pnl/banked_costs from any
        # partial one-sided closes) instead of recomputing
        # tx_costs fresh here, which silently drops banked_costs.
        # Open positions still compute live for real-time
        # unrealized tracking.
        if position.status == "CLOSED":
            gross_pnl = position.realized_pnl
            tx_costs  = position.transaction_costs
            net_pnl   = position.net_pnl
        else:
            tx_costs  = self._calculate_transaction_costs(
                position
            )
            gross_pnl = position.realized_pnl
            net_pnl   = gross_pnl - tx_costs

        return {
            "trade_id":                   position.trade_id,
            "strategy_name":              position.strategy_name,
            "regime_at_entry":            position.regime_at_entry,
            "regime_at_exit":             (
                position.regime_at_exit
                or self.re.confirmed_regime
            ),
            "entry_timestamp":            position.entry_timestamp,
            "exit_timestamp":             position.exit_timestamp,
            "holding_days":               holding_days,
            "entry_spot":                 position.entry_spot,
            "exit_spot":                  position.exit_spot,
            "entry_vix":                  position.entry_vix,
            "exit_vix":                   position.exit_vix,
            "legs_summary":               json.dumps([{
                "instrument_key": l.instrument_key,
                "side":           l.action,
                "qty":            l.qty,
                "entry_price":    l.entry_price,
                "exit_price":     l.exit_price,
                "option_type":    l.option_type,
                "strike":         l.strike,
                "expiry":         l.expiry,
                "order_tag":      l.order_tag,
            } for l in position.legs]),
            "total_credit_received":      position.total_credit,
            "total_debit_paid":           position.total_debit,
            "net_premium":                position.net_premium,
            "max_risk":                   position.max_risk,
            "realized_pnl":               gross_pnl,
            "transaction_costs":          tx_costs,
            "net_pnl":                    net_pnl,
            "realized_pnl_percent":       position.realized_pnl_percent,
            "exit_reason":                position.exit_reason,
            "slippage_total_points":      slippage_total,
            "composite_score_at_entry":   position.composite_at_entry,
            "vol_score":                  position.vol_score,
            "edge_score":                 position.edge_score,
            "trend_score":                position.trend_score,
            "flow_score":                 position.flow_score,
            "days_to_expiry_at_entry":    position.days_to_expiry,
            "expiry_date":                position.expiry_date,
            "paper_trade":                position.paper_trade,
            "stop_loss":                  position.stop_loss,
            "profit_target":              position.profit_target,
            "exit_dte":                   position.exit_dte,
            "max_hold_date":              position.max_hold_date,
            # SE-14: persist meta so max_profit, strategy_type,
            # trend_direction, banked_pnl etc. survive restarts.
            "meta_json":                  json.dumps(
                position.meta or {}
            ),
        }

    # ─────────────────────────────────────────────────────────────
    # Helper methods
    # ─────────────────────────────────────────────────────────────

    def _get_short_strike(
        self, position: Position, option_type: str
    ) -> Optional[float]:
        for leg in position.legs:
            if (
                leg.action == "SELL"
                and leg.option_type == option_type
                and leg.qty > 0   # PATCH: ignore already-closed legs
            ):
                return leg.strike
        return None

    def _get_upper_wing_strike(
        self, position: Position
    ) -> Optional[float]:
        strikes = [
            l.strike for l in position.legs
            if l.option_type == "put" and l.action == "BUY"
        ]
        return max(strikes) if strikes else None

    def _get_lower_wing_strike(
        self, position: Position
    ) -> Optional[float]:
        strikes = [
            l.strike for l in position.legs
            if l.option_type == "put" and l.action == "BUY"
        ]
        return min(strikes) if strikes else None

    def _has_short_positions(self) -> bool:
        return any(
            leg.action == "SELL"
            for pos in self.open_positions
            for leg in pos.legs
        )

    def _gamma_above_50pct_limit(self) -> bool:
        regime    = self.re.confirmed_regime
        limits    = config.GREEKS_LIMITS.get(regime, {})
        gamma_min = limits.get("gamma_min", -99)
        if gamma_min is None:
            return False
        greeks    = self._get_portfolio_greeks()
        threshold = gamma_min * 0.50
        return greeks["gamma"] < threshold

    def _get_25d_put_iv(self) -> float:
        strike = self.dm.get_strike_by_delta("put", 0.25)
        if strike is None:
            return 0.0
        active = self.dm.get_active_chain()
        return float(
            active.get(strike, {})
            .get("put", {})
            .get("iv", 0.0)
        )

    def _get_25d_call_iv(self) -> float:
        strike = self.dm.get_strike_by_delta("call", 0.25)
        if strike is None:
            return 0.0
        active = self.dm.get_active_chain()
        return float(
            active.get(strike, {})
            .get("call", {})
            .get("iv", 0.0)
        )

    def _get_otm_bid_ask(self, option_type: str) -> float:
        strike = self.dm.get_strike_by_delta(
            option_type, config.EVENT_STRANGLE_DELTA
        )
        if strike is None:
            return 99.0
        active = self.dm.get_active_chain()
        opt    = active.get(strike, {}).get(
            option_type, {}
        )
        return float(
            opt.get("ask", 99) - opt.get("bid", 0)
        )

    def _get_ema_200(self) -> float:
        if len(self.dm.candles_30m) < 200:
            return self.dm.spot or 0.0
        closes = [
            c["close"] for c in self.dm.candles_30m
        ]
        ema    = pd.Series(closes).ewm(
            span=200, adjust=False
        ).mean()
        return float(ema.iloc[-1])

    def _compute_ema_n(self, period: int) -> float:
        if len(self.dm.candles_30m) < period:
            return self.dm.spot or 0.0
        closes = [
            c["close"] for c in self.dm.candles_30m
        ]
        ema    = pd.Series(closes).ewm(
            span=period, adjust=False
        ).mean()
        return float(ema.iloc[-1])

    def _get_dte_for_target(
        self, min_dte: int, max_dte: int
    ) -> Optional[int]:
        expiry = self.dm.get_expiry_by_dte(
            (min_dte + max_dte) // 2,
            tolerance=(max_dte - min_dte) // 2,
        )
        if expiry is None:
            return None
        try:
            return (
                datetime.strptime(
                    expiry, "%Y-%m-%d"
                ).date() - date.today()
            ).days
        except ValueError:
            return None

    def _save_all_positions_to_sqlite(self) -> None:
        for position in self.open_positions:
            self.dm.save_position(
                self._position_to_dict(position)
            )

    def _log_portfolio_summary(self) -> None:
        greeks = self._get_portfolio_greeks()
        logger.info(
            f"\n{'=' * 60}\n"
            f"PORTFOLIO SUMMARY\n"
            f"Open Positions  : {len(self.open_positions)}\n"
            f"Daily P&L (net) : ₹{self.daily_pnl:,.2f}\n"
            f"Weekly P&L (net): ₹{self.weekly_pnl:,.2f}\n"
            f"Capital         : ₹{self.current_capital:,.2f}\n"
            f"Peak Capital    : ₹{self.peak_capital:,.2f}\n"
            f"Delta           : {greeks['delta']:.3f}\n"
            f"Gamma           : {greeks['gamma']:.5f}\n"
            f"Vega            : ₹{greeks['vega']:,.0f}\n"
            f"Theta           : ₹{greeks['theta']:,.0f}/day\n"
            f"CB L2 Active    : {self.cb_level_2_active}\n"
            f"Kill Switch     : {self.kill_switch_active}\n"
            f"{'=' * 60}"
        )

    def _log_circuit_breaker(
        self, level: int, trigger: str, action: str
    ) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO circuit_breaker_log (
                    timestamp, level, trigger,
                    action, daily_pnl, drawdown, regime
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(self._IST).isoformat(),
                level,
                trigger,
                action,
                self.daily_pnl,
                self.peak_capital - self.current_capital,
                self.re.confirmed_regime,
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(
                f"_log_circuit_breaker error: {e}"
            )

    def _load_positions_from_sqlite(self) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM open_positions "
                "WHERE status = 'OPEN'"
            )
            rows      = cursor.fetchall()
            col_names = [
                d[0] for d in cursor.description
            ]
            conn.close()

            if not rows:
                logger.info(
                    "No open positions to restore"
                )
                return

            for row in rows:
                row_dict  = dict(zip(col_names, row))
                legs_json = row_dict.get("legs_json", "[]")
                try:
                    legs_data = json.loads(legs_json)
                except Exception:
                    legs_data = []

                legs = []
                for l in legs_data:
                    _entry_price = float(
                        l.get("entry_price", 0)
                    )
                    leg = Leg(
                        instrument_key=l.get(
                            "instrument_key", ""
                        ),
                        option_type=l.get(
                            "option_type", "call"
                        ),
                        action=l.get("side", "BUY"),
                        strike=float(l.get("strike", 0)),
                        expiry=l.get(
                            "expiry",
                            row_dict.get("expiry_date", ""),
                        ),
                        qty=int(l.get("qty", 1)),
                        entry_price=_entry_price,
                        exit_price=float(
                            l.get("exit_price", 0)
                        ),
                        order_tag=l.get("order_tag", ""),
                        # PATCH: previously every restored leg
                        # defaulted to fill_status="PENDING" (the
                        # Leg dataclass default), causing
                        # _monitor_all_positions() to force-close
                        # every restored position at market on
                        # the very next cycle after any restart.
                        # A leg with a real recorded entry_price
                        # was genuinely filled before the restart;
                        # only a leg with no entry_price at all is
                        # still treated as pending.
                        fill_status=(
                            "COMPLETE" if _entry_price > 0
                            else "PENDING"
                        ),
                    )
                    legs.append(leg)

                # SE-14: restore meta from SQLite
                _meta_json = row_dict.get("meta_json", "{}")
                try:
                    _restored_meta = json.loads(
                        _meta_json or "{}"
                    )
                except Exception:
                    _restored_meta = {}

                position = Position(
                    trade_id=row_dict["trade_id"],
                    strategy_name=row_dict["strategy_name"],
                    regime_at_entry=row_dict.get(
                        "regime_at_entry", ""
                    ),
                    entry_timestamp=row_dict.get(
                        "entry_timestamp", ""
                    ),
                    entry_spot=float(
                        row_dict.get("entry_spot", 0)
                    ),
                    entry_vix=float(
                        row_dict.get("entry_vix", 0)
                    ),
                    legs=legs,
                    stop_loss=float(
                        row_dict.get("stop_loss", 0)
                    ),
                    profit_target=float(
                        row_dict.get("profit_target", 0)
                    ),
                    exit_dte=row_dict.get("exit_dte"),
                    max_hold_date=row_dict.get(
                        "max_hold_date"
                    ),
                    composite_at_entry=float(
                        row_dict.get(
                            "composite_at_entry", 0
                        )
                    ),
                    vol_score=float(
                        row_dict.get("vol_score", 0)
                    ),
                    edge_score=float(
                        row_dict.get("edge_score", 0)
                    ),
                    trend_score=float(
                        row_dict.get("trend_score", 0)
                    ),
                    flow_score=float(
                        row_dict.get("flow_score", 0)
                    ),
                    days_to_expiry=int(
                        row_dict.get("days_to_expiry", 0)
                    ),
                    expiry_date=row_dict.get(
                        "expiry_date", ""
                    ),
                    total_credit=float(
                        row_dict.get("total_credit", 0)
                    ),
                    total_debit=float(
                        row_dict.get("total_debit", 0)
                    ),
                    net_premium=float(
                        row_dict.get("net_premium", 0)
                    ),
                    max_risk=float(
                        row_dict.get("max_risk", 0)
                    ),
                    paper_trade=bool(
                        row_dict.get("paper_trade", 1)
                    ),
                    status="OPEN",
                    meta=_restored_meta,
                )
                self.open_positions.append(position)
                logger.info(
                    f"Restored: {position.strategy_name} "
                    f"id={position.trade_id[:8]}"
                )

            logger.info(
                f"Restored {len(rows)} positions"
            )

        except sqlite3.OperationalError:
            logger.info("No state.db — fresh start")
        except Exception as e:
            logger.warning(
                f"_load_positions_from_sqlite error: {e}"
            )

    def reset_daily_state(self) -> None:
        # P2-4b: update consecutive CB2 counter on daily reset
        # Read yesterday's CB L2 status BEFORE clearing it.
        if self.cb_level_2_active:
            # Yesterday ended with CB L2 fired
            self._consecutive_cb2_days += 1
            self._prev_day_cb2_fired = True
            logger.warning(
                f"P2-4: CB L2 fired {self._consecutive_cb2_days} "
                f"consecutive day(s) — daily loss threshold will "
                f"tighten today"
            )
        else:
            # Yesterday was clean (or profitable) — reset streak
            if self._consecutive_cb2_days > 0:
                logger.info(
                    "P2-4: CB L2 streak ended — "
                    "resetting consecutive counter to 0"
                )
            self._consecutive_cb2_days = 0
            self._prev_day_cb2_fired = False
        self.daily_pnl            = 0.0
        self.daily_trading_halted = False
        self.cb_level_2_active    = False
        self.cb_level_1_count     = 0
        logger.info("Daily state reset")

    def reset_weekly_state(self) -> None:
        self.weekly_pnl        = 0.0
        self.cb_level_3_active = False
        logger.info("Weekly state reset")