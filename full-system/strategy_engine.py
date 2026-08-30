# ============ FILE: strategy_engine.py ============
"""
Strategy selection, construction, execution, position management,
risk management, circuit breakers, and trade logging.

FIXES IN THIS VERSION:
  CRITICAL FIX 1: Idempotent order placement via deterministic tags
  CRITICAL FIX 2: Cancel sweep for all open orders (EOD + shutdown)
  CRITICAL FIX 3: Order tag deduplication system
  HIGH FIX 4:     Transaction cost deduction from performance tracking
"""

import asyncio
import sqlite3
import csv
import uuid
import json
import math
import hashlib
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


@dataclass
class Leg:
    """Represents a single option leg in a strategy."""
    instrument_key: str
    option_type:    str
    action:         str
    strike:         float
    expiry:         str
    qty:            int
    entry_price:    float = 0.0
    exit_price:     float = 0.0
    order_id:       str = ""
    order_tag:      str = ""      # CRITICAL FIX 3: tag for deduplication
    fill_status:    str = "PENDING"
    delta:          float = 0.0
    gamma:          float = 0.0
    vega:           float = 0.0
    theta:          float = 0.0
    slippage_pts:   float = 0.0


@dataclass
class Position:
    """Represents a complete multi-leg options position."""
    trade_id:           str
    strategy_name:      str
    regime_at_entry:    str
    entry_timestamp:    str
    entry_spot:         float
    entry_vix:          float
    legs:               List[Leg]
    stop_loss:          float
    profit_target:      float
    exit_dte:           Optional[int]
    max_hold_date:      Optional[str]
    composite_at_entry: float
    vol_score:          float
    edge_score:         float
    trend_score:        float
    flow_score:         float
    days_to_expiry:     int
    expiry_date:        str
    status:             str = "OPEN"
    total_credit:       float = 0.0
    total_debit:        float = 0.0
    net_premium:        float = 0.0
    max_risk:           float = 0.0
    realized_pnl:       float = 0.0
    realized_pnl_percent: float = 0.0
    exit_reason:        str = ""
    exit_timestamp:     str = ""
    exit_spot:          float = 0.0
    exit_vix:           float = 0.0
    paper_trade:        bool = True
    trend_direction:    float = 0.0
    meta:               Dict = field(default_factory=dict)
    transaction_costs:  float = 0.0   # HIGH FIX 4: track costs
    net_pnl:            float = 0.0   # HIGH FIX 4: pnl after costs


class StrategyEngine:
    """
    Manages strategy selection, construction, execution,
    position monitoring, risk management, and circuit breakers.

    CRITICAL FIXES:
      - All orders use deterministic tags for idempotency
      - Cancel sweep runs at EOD and on shutdown
      - Order deduplication prevents double-fills
      - Transaction costs tracked and deducted from PnL
    """

    # CRITICAL FIX 3: Order tag prefix for identification
    ORDER_TAG_PREFIX = "nao"

    def __init__(
        self,
        data_manager: DataManager,
        regime_engine: RegimeEngine,
        db_path: str,
    ) -> None:
        """Initialize StrategyEngine."""
        self.dm = data_manager
        self.re = regime_engine
        self.db_path = db_path
        self._IST = pytz.timezone(config.TZ)

        self.open_positions: List[Position] = []
        self.closed_positions: List[Position] = []

        self.daily_pnl: float = 0.0
        self.weekly_pnl: float = 0.0
        self.peak_capital: float = float(config.TOTAL_CAPITAL)
        self.current_capital: float = float(config.TOTAL_CAPITAL)
        self.daily_trading_halted: bool = False
        self.kill_switch_active: bool = False
        self.cooling_period_end: Optional[datetime] = None

        self.cb_level_1_count: int = 0
        self.cb_level_2_active: bool = False
        self.cb_level_3_active: bool = False
        self.cb_level_4_active: bool = False

        self._last_trading_date: Optional[date] = None

        # CRITICAL FIX 3: Session order registry
        # Tracks every order placed this session for dedup + sweep
        self._session_orders: Dict[str, Dict[str, Any]] = {}
        self._session_orders_lock = asyncio.Lock()

        # CRITICAL FIX 1: In-flight order tracking
        # Prevents concurrent duplicate placements
        self._inflight_tags: set = set()
        self._inflight_lock = asyncio.Lock()

        # Initialize session orders table in SQLite
        self._init_session_orders_table()

    # =========================================================================
    # CRITICAL FIX 1+3: Idempotent Order Tag System
    # =========================================================================

    def _generate_order_tag(
        self,
        trade_id: str,
        instrument_key: str,
        action: str,
        leg_index: int = 0,
    ) -> str:
        """
        CRITICAL FIX 1+3: Generate deterministic order tag.

        Same inputs ALWAYS produce the same tag.
        Upstox uses this tag to detect and reject duplicate orders.
        If engine crashes and retries, the same tag is generated,
        and the existing order is found instead of placing a new one.

        Format: nao-{8char_hash} (max 20 chars, alphanumeric)
        """
        raw = (
            f"{trade_id[:12]}-"
            f"{instrument_key[-8:]}-"
            f"{action}-"
            f"{leg_index}-"
            f"{date.today().isoformat()}"
        )
        tag_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"{self.ORDER_TAG_PREFIX}-{tag_hash}"

    async def _register_order(
        self,
        order_id: str,
        tag: str,
        instrument_key: str,
        action: str,
        qty: int,
        price: float,
        trade_id: str,
    ) -> None:
        """
        CRITICAL FIX 3: Register every placed order.
        Used for deduplication check and EOD cancel sweep.
        """
        order_record = {
            "order_id":       order_id,
            "tag":            tag,
            "instrument_key": instrument_key,
            "action":         action,
            "qty":            qty,
            "price":          price,
            "trade_id":       trade_id,
            "placed_at":      datetime.now(self._IST).isoformat(),
            "session_date":   date.today().isoformat(),
            "cancelled":      False,
            "filled":         False,
        }
        async with self._session_orders_lock:
            self._session_orders[tag] = order_record

        # Persist to SQLite for crash recovery
        self._persist_order_to_sqlite(order_record)

    def _persist_order_to_sqlite(
        self, order_record: Dict[str, Any]
    ) -> None:
        """Persist order record to SQLite for crash recovery."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO session_orders (
                    order_id, tag, instrument_key, action,
                    qty, price, trade_id, placed_at,
                    session_date, cancelled, filled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_record["order_id"],
                order_record["tag"],
                order_record["instrument_key"],
                order_record["action"],
                order_record["qty"],
                order_record["price"],
                order_record["trade_id"],
                order_record["placed_at"],
                order_record["session_date"],
                0,
                0,
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Order persist SQLite error: {e}")

    async def _mark_order_filled(self, tag: str) -> None:
        """Mark order as filled in registry and SQLite."""
        async with self._session_orders_lock:
            if tag in self._session_orders:
                self._session_orders[tag]["filled"] = True
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE session_orders SET filled=1 WHERE tag=?",
                (tag,)
            )
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Mark filled SQLite error: {e}")

    async def _mark_order_cancelled(self, tag: str) -> None:
        """Mark order as cancelled in registry and SQLite."""
        async with self._session_orders_lock:
            if tag in self._session_orders:
                self._session_orders[tag]["cancelled"] = True
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE session_orders SET cancelled=1 WHERE tag=?",
                (tag,)
            )
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Mark cancelled SQLite error: {e}")

    async def _check_existing_order_by_tag(
        self, tag: str
    ) -> Optional[Tuple[str, float, str]]:
        """
        CRITICAL FIX 1: Check if order with this tag already exists.

        Returns (order_id, fill_price, status) if found.
        Returns None if no order with this tag exists.

        This is the core idempotency check — if we crashed and
        are retrying, we find the existing order instead of
        placing a duplicate.
        """
        # Check in-memory registry first (fast path)
        async with self._session_orders_lock:
            existing = self._session_orders.get(tag)
            if existing:
                if existing.get("filled"):
                    return (
                        existing["order_id"],
                        existing.get("fill_price", 0.0),
                        "complete",
                    )

        # Check broker via API (authoritative source)
        if config.PAPER_TRADING_MODE:
            return None

        try:
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"tag": tag}
            )
            orders = (
                response if isinstance(response, list)
                else response.get("data", []) or []
            )
            if not orders:
                return None

            last = orders[-1] if isinstance(orders, list) else orders
            if not isinstance(last, dict):
                return None

            status = str(last.get("status", "")).lower()
            order_id = str(last.get("order_id", ""))
            fill_price = float(
                last.get("average_price", 0) or 0
            )

            if status in ("complete", "filled", "traded"):
                logger.info(
                    f"Idempotency: tag={tag} already filled "
                    f"@ {fill_price:.2f} order_id={order_id}"
                )
                return (order_id, fill_price, "complete")

            if status in ("open", "pending", "trigger pending"):
                logger.info(
                    f"Idempotency: tag={tag} already open "
                    f"order_id={order_id}"
                )
                return (order_id, 0.0, "open")

            if status in ("rejected", "cancelled", "canceled"):
                logger.info(
                    f"Idempotency: tag={tag} was {status} "
                    f"— will place fresh order"
                )
                return None

            return None

        except Exception as e:
            logger.warning(
                f"Idempotency check failed for tag={tag}: {e}"
            )
            return None

    def _init_session_orders_table(self) -> None:
        """Initialize session_orders table in SQLite."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS session_orders (
                    tag           TEXT PRIMARY KEY,
                    order_id      TEXT,
                    instrument_key TEXT,
                    action        TEXT,
                    qty           INTEGER,
                    price         REAL,
                    fill_price    REAL DEFAULT 0,
                    trade_id      TEXT,
                    placed_at     TEXT,
                    session_date  TEXT,
                    cancelled     INTEGER DEFAULT 0,
                    filled        INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_orders_date
                ON session_orders(session_date)
            """)
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(
                f"session_orders table init error: {e}"
            )

    async def startup_cancel_stale_orders(self) -> int:
        """
        CRITICAL FIX 2: On startup, cancel any orders left from
        a previous crashed session.

        Called BEFORE any new orders are placed.
        Prevents ghost orders from previous runs filling unexpectedly.
        """
        if config.PAPER_TRADING_MODE:
            logger.info(
                "STARTUP: Paper mode — stale order check skipped"
            )
            return 0

        logger.info(
            "STARTUP: Checking for stale orders from "
            "previous session..."
        )
        cancelled = 0

        try:
            # Step 1: Load uncancelled orders from previous sessions
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tag, order_id, instrument_key,
                       action, placed_at
                FROM session_orders
                WHERE cancelled = 0
                AND filled = 0
                AND session_date < ?
            """, (date.today().isoformat(),))
            stale_orders = cursor.fetchall()
            conn.close()

            if not stale_orders:
                logger.info(
                    "STARTUP: No stale orders found"
                )
                return 0

            logger.warning(
                f"STARTUP: Found {len(stale_orders)} potentially "
                f"stale orders from previous sessions"
            )

            # Step 2: Check each stale order at broker
            for tag, order_id, instrument_key, action, placed_at in stale_orders:
                try:
                    response = await self.dm._api_get(
                        config.EP_ORDER_HISTORY,
                        {"order_id": order_id}
                    )
                    orders = (
                        response if isinstance(response, list)
                        else response.get("data", []) or []
                    )
                    if not orders:
                        # Order not found — mark as cancelled
                        await self._mark_order_cancelled(tag)
                        continue

                    last = orders[-1]
                    status = str(
                        last.get("status", "")
                    ).lower()

                    if status in (
                        "complete", "filled", "traded",
                        "cancelled", "rejected", "day_closed"
                    ):
                        # Already terminal — just mark it
                        await self._mark_order_cancelled(tag)
                        logger.info(
                            f"STARTUP: Stale order tag={tag} "
                            f"already {status}"
                        )
                        continue

                    # Order is still open — cancel it
                    logger.warning(
                        f"STARTUP: Cancelling stale order "
                        f"tag={tag} order_id={order_id} "
                        f"instrument={instrument_key} "
                        f"action={action} "
                        f"placed_at={placed_at}"
                    )
                    await self.dm._api_delete(
                        f"{config.EP_ORDER_CANCEL}/{order_id}"
                    )
                    await self._mark_order_cancelled(tag)
                    cancelled += 1
                    await asyncio.sleep(
                        config.ORDER_BETWEEN_LEGS_DELAY_SEC
                    )

                except Exception as e:
                    logger.error(
                        f"STARTUP: Failed to handle stale "
                        f"order tag={tag}: {e}"
                    )

        except sqlite3.Error as e:
            logger.warning(
                f"STARTUP: Stale order check DB error: {e}"
            )

        if cancelled > 0:
            logger.warning(
                f"STARTUP: Cancelled {cancelled} stale "
                f"orders from previous session"
            )
        else:
            logger.info(
                "STARTUP: All previous orders were terminal — "
                "clean start"
            )

        return cancelled

    # =========================================================================
    # CRITICAL FIX 2: Cancel Sweep
    # =========================================================================

    async def cancel_all_open_orders(
        self, context: str = "EOD_SWEEP"
    ) -> int:
        """
        CRITICAL FIX 2: Cancel ALL open orders placed this session.

        Called at:
          - 15:15 (TIME_EXIT_NORMAL) before position close
          - Graceful shutdown
          - Kill switch activation
          - Any unhandled exception in main loop

        This ensures NOTHING is left open at the broker after
        the engine stops. Equivalent to Engine 2's 15:29 sweep.
        """
        if config.PAPER_TRADING_MODE:
            logger.info(
                f"[PAPER] {context}: cancel sweep skipped "
                f"(no real orders)"
            )
            return 0

        logger.info(
            f"{context}: Starting order cancel sweep..."
        )
        cancelled_count = 0
        failed_count = 0
        already_terminal = 0

        # Step 1: Get all currently open orders from broker
        try:
            response = await self.dm._api_get(
                "/order/open-orders", {}
            )
            open_orders = (
                response if isinstance(response, list)
                else response.get("data", []) or []
            )
        except Exception as e:
            logger.error(
                f"{context}: Cannot fetch open orders: {e} "
                f"— attempting cancel by session registry"
            )
            open_orders = []

        # Step 2: Cancel open orders that belong to us
        our_prefixes = (self.ORDER_TAG_PREFIX,)
        for order in open_orders:
            order_id = str(order.get("order_id", ""))
            tag = str(order.get("tag", ""))
            status = str(order.get("status", "")).lower()
            instrument = str(
                order.get("instrument_token", "")
                or order.get("instrument_key", "")
            )

            # Only cancel orders placed by this engine
            is_ours = any(
                tag.startswith(p) for p in our_prefixes
            )
            if not is_ours:
                logger.debug(
                    f"{context}: Skipping non-algo order "
                    f"order_id={order_id} tag={tag}"
                )
                continue

            if status in (
                "complete", "filled", "traded",
                "cancelled", "rejected", "day_closed"
            ):
                already_terminal += 1
                continue

            try:
                await self.dm._api_delete(
                    f"{config.EP_ORDER_CANCEL}/{order_id}"
                )
                await self._mark_order_cancelled(tag)
                cancelled_count += 1
                logger.info(
                    f"{context}: Cancelled order_id={order_id} "
                    f"tag={tag} instrument={instrument}"
                )
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )
            except Exception as e:
                failed_count += 1
                logger.error(
                    f"{context}: Failed to cancel "
                    f"order_id={order_id} tag={tag}: {e}"
                )

        # Step 3: Also sweep session registry for any orders
        # not returned by the open-orders endpoint
        async with self._session_orders_lock:
            registry_snapshot = dict(self._session_orders)

        for tag, record in registry_snapshot.items():
            if record.get("cancelled") or record.get("filled"):
                continue
            order_id = record.get("order_id", "")
            if not order_id:
                continue

            # Check current status
            try:
                response = await self.dm._api_get(
                    config.EP_ORDER_HISTORY,
                    {"order_id": order_id}
                )
                orders = (
                    response if isinstance(response, list)
                    else response.get("data", []) or []
                )
                if orders:
                    last = orders[-1]
                    status = str(
                        last.get("status", "")
                    ).lower()
                    if status in (
                        "complete", "filled", "traded"
                    ):
                        await self._mark_order_filled(tag)
                        continue
                    if status in (
                        "cancelled", "rejected", "day_closed"
                    ):
                        await self._mark_order_cancelled(tag)
                        continue
                    # Still open — cancel it
                    await self.dm._api_delete(
                        f"{config.EP_ORDER_CANCEL}/{order_id}"
                    )
                    await self._mark_order_cancelled(tag)
                    cancelled_count += 1
                    logger.info(
                        f"{context}: Registry sweep cancelled "
                        f"order_id={order_id} tag={tag}"
                    )
                    await asyncio.sleep(
                        config.ORDER_BETWEEN_LEGS_DELAY_SEC
                    )
            except Exception as e:
                logger.warning(
                    f"{context}: Registry sweep check failed "
                    f"for tag={tag}: {e}"
                )

        # Step 4: Verify broker has no open algo orders
        await asyncio.sleep(1.0)
        try:
            response = await self.dm._api_get(
                "/order/open-orders", {}
            )
            remaining = (
                response if isinstance(response, list)
                else response.get("data", []) or []
            )
            our_remaining = [
                o for o in remaining
                if any(
                    str(o.get("tag", "")).startswith(p)
                    for p in our_prefixes
                )
            ]
            if our_remaining:
                logger.critical(
                    f"{context}: {len(our_remaining)} algo "
                    f"orders STILL OPEN after sweep: "
                    f"{[o.get('order_id') for o in our_remaining]}"
                )
            else:
                logger.info(
                    f"{context}: Verified — broker has "
                    f"zero open algo orders"
                )
        except Exception as e:
            logger.warning(
                f"{context}: Post-sweep verification "
                f"failed: {e}"
            )

        logger.info(
            f"{context}: Sweep complete — "
            f"cancelled={cancelled_count} "
            f"failed={failed_count} "
            f"already_terminal={already_terminal}"
        )
        return cancelled_count

    # =========================================================================
    # CRITICAL FIX 1+3: Fixed Order Placement with Idempotency
    # =========================================================================

    async def _place_single_leg(
        self, leg: Leg, use_market: bool = False,
        trade_id: str = "", leg_index: int = 0,
    ) -> Tuple[bool, str]:
        """
        Place a single option order with full idempotency protection.

        CRITICAL FIX 1: Generates deterministic tag, checks for
        existing order before placing new one.
        CRITICAL FIX 3: Registers every order for dedup + sweep.

        Flow:
          1. Generate deterministic tag
          2. Check if order with this tag already exists
          3. If exists and filled → return existing fill
          4. If exists and open → wait for fill
          5. If not exists → place new order
          6. Register order in session registry
        """
        if config.PAPER_TRADING_MODE:
            return await self._simulate_fill(leg)

        # CRITICAL FIX 1: Generate deterministic tag
        tag = self._generate_order_tag(
            trade_id or leg.instrument_key[:12],
            leg.instrument_key,
            leg.action,
            leg_index,
        )
        leg.order_tag = tag

        # CRITICAL FIX 3: Prevent concurrent duplicate placement
        async with self._inflight_lock:
            if tag in self._inflight_tags:
                logger.warning(
                    f"Concurrent duplicate prevented: "
                    f"tag={tag} already in-flight"
                )
                return (False, "")
            self._inflight_tags.add(tag)

        try:
            # CRITICAL FIX 1: Check for existing order
            existing = await self._check_existing_order_by_tag(tag)
            if existing is not None:
                order_id, fill_price, status = existing

                if status == "complete" and fill_price > 0:
                    # Already filled — reuse fill
                    leg.entry_price = fill_price
                    leg.order_id = order_id
                    leg.fill_status = "COMPLETE"
                    leg.slippage_pts = 0.0
                    logger.info(
                        f"Idempotency reuse: tag={tag} "
                        f"order_id={order_id} "
                        f"fill={fill_price:.2f}"
                    )
                    await self._mark_order_filled(tag)
                    return (True, order_id)

                if status == "open":
                    # Order exists but not filled — wait for it
                    logger.info(
                        f"Idempotency wait: tag={tag} "
                        f"order_id={order_id} still open"
                    )
                    filled = await self._wait_for_fill(
                        order_id,
                        config.CORE_FILL_TIMEOUT_SEC,
                    )
                    if filled:
                        fill_price = await self._get_fill_price(
                            order_id
                        )
                        leg.entry_price = (
                            fill_price if fill_price > 0 else 0
                        )
                        leg.order_id = order_id
                        leg.fill_status = "COMPLETE"
                        await self._mark_order_filled(tag)
                        return (True, order_id)
                    else:
                        await self._cancel_order(order_id)
                        await self._mark_order_cancelled(tag)
                        return (False, order_id)

            # No existing order — place new one
            chain = self.dm.option_chain
            opt_data = chain.get(leg.strike, {}).get(
                leg.option_type, {}
            )

            if use_market:
                order_type = "MARKET"
                price = 0
            else:
                order_type = "LIMIT"
                if leg.action == "BUY":
                    price = (
                        opt_data.get("ask", 0) +
                        config.ORDER_AGGRESSION_TICKS
                        * config.TICK_SIZE
                    )
                else:
                    price = (
                        opt_data.get("bid", 0) -
                        config.ORDER_AGGRESSION_TICKS
                        * config.TICK_SIZE
                    )
                price = max(
                    config.TICK_SIZE,
                    round(
                        price / config.TICK_SIZE
                    ) * config.TICK_SIZE,
                )

            payload = {
                "quantity":            leg.qty * config.LOT_SIZE,
                "product":             "D",
                "validity":            "DAY",
                "price":               price,
                "tag":                 tag,
                "instrument_token":    leg.instrument_key,
                "order_type":          order_type,
                "transaction_type":    leg.action,
                "disclosed_quantity":  0,
                "trigger_price":       0,
                "is_amo":              False,
            }

            try:
                response = await self.dm._api_post(
                    config.EP_ORDER_PLACE, payload
                )
                order_id = (
                    response.get("data", {}).get("order_id", "")
                    or response.get("order_id", "")
                )
                if not order_id:
                    logger.warning(
                        f"No order_id for tag={tag} "
                        f"{leg.action} {leg.option_type} "
                        f"{leg.strike}"
                    )
                    return (False, "")

                logger.info(
                    f"Order placed: tag={tag} "
                    f"order_id={order_id} "
                    f"{leg.action} {leg.option_type} "
                    f"{leg.strike} "
                    f"qty={leg.qty * config.LOT_SIZE} "
                    f"price={price}"
                )

                # CRITICAL FIX 3: Register in session registry
                await self._register_order(
                    order_id=order_id,
                    tag=tag,
                    instrument_key=leg.instrument_key,
                    action=leg.action,
                    qty=leg.qty * config.LOT_SIZE,
                    price=price,
                    trade_id=trade_id,
                )

                filled = await self._wait_for_fill(
                    order_id, config.CORE_FILL_TIMEOUT_SEC
                )
                if not filled:
                    await self._cancel_order(order_id)
                    await self._mark_order_cancelled(tag)
                    return (False, order_id)

                fill_price = await self._get_fill_price(order_id)
                leg.entry_price = (
                    fill_price if fill_price > 0 else price
                )
                leg.order_id = order_id
                leg.fill_status = "COMPLETE"

                expected = (
                    price if order_type == "LIMIT"
                    else opt_data.get("ltp", price)
                )
                slippage = abs(leg.entry_price - expected)
                leg.slippage_pts = slippage
                if slippage > 2:
                    logger.warning(
                        f"High slippage: {slippage:.2f} pts "
                        f"for {leg.action} {leg.option_type} "
                        f"{leg.strike}"
                    )

                await self._mark_order_filled(tag)
                return (True, order_id)

            except Exception as e:
                logger.error(
                    f"Order placement error tag={tag}: {e}"
                )
                return (False, "")

        finally:
            # CRITICAL FIX 3: Always remove from in-flight set
            async with self._inflight_lock:
                self._inflight_tags.discard(tag)

    async def _simulate_fill(
        self, leg: Leg
    ) -> Tuple[bool, str]:
        """Simulate order fill for paper trading with slippage."""
        chain = self.dm.option_chain
        opt_data = chain.get(leg.strike, {}).get(
            leg.option_type, {}
        )
        ltp = opt_data.get("ltp", 0)

        if ltp == 0 or ltp is None:
            logger.warning(
                f"Paper fill: LTP=0 for {leg.option_type} "
                f"strike={leg.strike}"
            )
            return (False, "")

        if leg.action == "SELL":
            slippage = (
                config.PAPER_SLIPPAGE_SHORT_TICKS
                * config.TICK_SIZE
            )
            fill_price = ltp - slippage
        else:
            slippage = (
                config.PAPER_SLIPPAGE_HEDGE_TICKS
                * config.TICK_SIZE
            )
            fill_price = ltp + slippage

        fill_price = max(
            config.TICK_SIZE,
            round(
                fill_price / config.TICK_SIZE
            ) * config.TICK_SIZE,
        )

        leg.entry_price = fill_price
        leg.slippage_pts = slippage
        leg.fill_status = "COMPLETE"

        order_id = f"PAPER_{uuid.uuid4().hex[:8]}"
        leg.order_id = order_id
        leg.order_tag = f"paper_{order_id}"

        logger.info(
            f"Paper fill: {leg.action} {leg.option_type} "
            f"strike={leg.strike} ltp={ltp:.2f} "
            f"fill={fill_price:.2f} slippage={slippage:.2f}"
        )
        return (True, order_id)

    async def _wait_for_fill(
        self, order_id: str, timeout_sec: int
    ) -> bool:
        """Poll order status until filled or timeout."""
        start = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout_sec:
                logger.warning(
                    f"Fill timeout after {timeout_sec}s: "
                    f"order_id={order_id}"
                )
                return False
            await asyncio.sleep(config.ORDER_POLL_INTERVAL_SEC)
            status = await self._get_order_status(order_id)
            if status == "complete":
                return True
            elif status in ["rejected", "cancelled"]:
                logger.warning(
                    f"Order {status}: order_id={order_id}"
                )
                return False

    async def _get_order_status(
        self, order_id: str
    ) -> str:
        """Fetch current order status from broker."""
        try:
            await asyncio.sleep(
                config.ORDER_STATUS_POLL_DELAY_SEC
            )
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"order_id": order_id},
            )
            orders = (
                response if isinstance(response, list) else []
            )
            if orders:
                return str(
                    orders[-1].get("status", "unknown")
                ).lower()
            return "unknown"
        except Exception as e:
            logger.warning(
                f"_get_order_status error for "
                f"{order_id}: {e}"
            )
            return "unknown"

    async def _get_fill_price(
        self, order_id: str
    ) -> float:
        """Fetch average fill price for completed order."""
        try:
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"order_id": order_id},
            )
            orders = (
                response if isinstance(response, list) else []
            )
            if orders:
                return float(
                    orders[-1].get("average_price", 0)
                )
            return 0.0
        except Exception as e:
            logger.warning(
                f"_get_fill_price error for {order_id}: {e}"
            )
            return 0.0

    async def _cancel_order(self, order_id: str) -> None:
        """Cancel a pending order."""
        try:
            await self.dm._api_delete(
                f"{config.EP_ORDER_CANCEL}/{order_id}"
            )
            logger.info(f"Order cancelled: {order_id}")
        except Exception as e:
            logger.warning(
                f"Cancel failed for {order_id}: {e}"
            )

    async def _cancel_and_reverse(
        self, filled_legs: List[Leg]
    ) -> None:
        """Reverse all filled legs at market to abort partial position."""
        logger.warning(
            f"Aborting strategy — reversing "
            f"{len(filled_legs)} filled legs"
        )
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
            try:
                await self._place_single_leg(
                    reverse_leg, use_market=True
                )
                logger.info(
                    f"Reversed: {reverse_action} "
                    f"{leg.option_type} strike={leg.strike}"
                )
            except Exception as e:
                logger.error(
                    f"Reverse failed for {leg.option_type} "
                    f"strike={leg.strike}: {e}"
                )
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

    # =========================================================================
    # HIGH FIX 4: Transaction Cost Calculation
    # =========================================================================

    def _calculate_transaction_costs(
        self, position: Position
    ) -> float:
        """
        HIGH FIX 4: Calculate actual NSE transaction costs.

        Includes:
          - Brokerage: ₹20 per order (flat)
          - STT: 0.1% on sell side (delivery)
          - Exchange charges: 0.00325% on turnover
          - SEBI charges: 0.0001% on turnover
          - Stamp duty: 0.015% on buy side
          - GST: 18% on brokerage + exchange

        Returns total costs in rupees.
        """
        if not position.legs:
            return 0.0

        buy_value = 0.0
        sell_value = 0.0
        num_orders = 0

        for leg in position.legs:
            if leg.entry_price > 0 and leg.qty > 0:
                value = leg.entry_price * leg.qty * config.LOT_SIZE
                num_orders += 1
                if leg.action == "BUY":
                    buy_value += value
                else:
                    sell_value += value

            if leg.exit_price > 0 and leg.qty > 0:
                value = leg.exit_price * leg.qty * config.LOT_SIZE
                num_orders += 1
                # At exit: BUY to close short, SELL to close long
                if leg.action == "SELL":
                    buy_value += value   # closing a short = BUY
                else:
                    sell_value += value  # closing a long = SELL

        total_turnover = buy_value + sell_value
        if total_turnover <= 0:
            return 0.0

        brokerage    = min(20.0, total_turnover * 0.0003) * num_orders
        stt          = sell_value * 0.001
        exchange_fee = total_turnover * 0.0000325
        sebi         = total_turnover * 0.000001
        stamp        = buy_value * 0.00015
        gst          = (brokerage + exchange_fee) * 0.18

        total = brokerage + stt + exchange_fee + sebi + stamp + gst
        return round(total, 2)

    def _calculate_final_pnl(
        self, position: Position
    ) -> Tuple[float, float, float]:
        """
        HIGH FIX 4: Calculate gross PnL, transaction costs,
        and net PnL separately.

        Returns (gross_pnl, transaction_costs, net_pnl)
        """
        gross_pnl = 0.0
        for leg in position.legs:
            if leg.exit_price == 0:
                continue
            if leg.action == "SELL":
                leg_pnl = (
                    (leg.entry_price - leg.exit_price)
                    * leg.qty * config.LOT_SIZE
                )
            else:
                leg_pnl = (
                    (leg.exit_price - leg.entry_price)
                    * leg.qty * config.LOT_SIZE
                )
            gross_pnl += leg_pnl

        tx_costs = self._calculate_transaction_costs(position)
        net_pnl = gross_pnl - tx_costs

        return gross_pnl, tx_costs, net_pnl

    def _estimate_costs(self, position: Position) -> float:
        """Estimate transaction costs for position dict serialization."""
        return self._calculate_transaction_costs(position)

    # =========================================================================
    # MAIN CYCLE
    # =========================================================================

    async def run_cycle(self) -> None:
        """Main method called every 5 minutes after regime refresh."""
        logger.info("Strategy cycle started")

        if self.kill_switch_active:
            logger.info(
                "Kill switch active — no action this cycle"
            )
            return

        today = date.today()
        if self._last_trading_date != today:
            self.reset_daily_state()
            self._last_trading_date = today
            if today.weekday() == 0:
                self.reset_weekly_state()

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

        self._check_greeks_limits()
        self._save_all_positions_to_sqlite()
        self._log_portfolio_summary()

        logger.info("Strategy cycle complete")

    async def _update_all_pnls(self) -> None:
        """Update MTM P&L for all open positions from live chain LTP."""
        today_str = date.today().isoformat()

        for position in self.open_positions:
            position_value = 0.0
            for leg in position.legs:
                ltp = (
                    self.dm.option_chain
                    .get(leg.strike, {})
                    .get(leg.option_type, {})
                    .get("ltp", 0)
                )
                if ltp == 0 or ltp is None:
                    ltp = leg.entry_price
                    logger.warning(
                        f"LTP=0 for {leg.option_type} "
                        f"strike={leg.strike} — "
                        f"using entry_price={leg.entry_price}"
                    )

                if leg.action == "SELL":
                    leg_pnl = (
                        (leg.entry_price - ltp)
                        * leg.qty * config.LOT_SIZE
                    )
                else:
                    leg_pnl = (
                        (ltp - leg.entry_price)
                        * leg.qty * config.LOT_SIZE
                    )
                position_value += leg_pnl

            position.realized_pnl = position_value

        self.daily_pnl = sum(
            p.realized_pnl
            for p in self.open_positions + self.closed_positions
            if p.entry_timestamp
            and p.entry_timestamp[:10] == today_str
        )

    async def _check_circuit_breakers(self) -> None:
        """Check and enforce all 5 circuit breaker levels."""

        # LEVEL 1 — Single position loss
        for position in list(self.open_positions):
            if position.realized_pnl < -(
                config.CB_LEVEL_1_PCT * config.TOTAL_CAPITAL
            ):
                logger.critical(
                    f"CB L1 TRIGGERED: "
                    f"position={position.trade_id} "
                    f"pnl={position.realized_pnl:.2f} "
                    f"action={config.CB_LEVEL_1_ACTION}"
                )
                self._log_circuit_breaker(
                    1,
                    f"position_loss={position.realized_pnl:.2f}",
                    config.CB_LEVEL_1_ACTION,
                )
                await self._close_position(
                    position,
                    config.EXIT_REASONS["CIRCUIT_BREAK"],
                )
                self.cb_level_1_count += 1

        # LEVEL 2 — Daily loss
        if self.daily_pnl < -(
            config.CB_LEVEL_2_PCT * config.TOTAL_CAPITAL
        ):
            if not self.cb_level_2_active:
                logger.critical(
                    f"CB L2 TRIGGERED: "
                    f"daily_pnl={self.daily_pnl:.2f} "
                    f"action={config.CB_LEVEL_2_ACTION}"
                )
                self._log_circuit_breaker(
                    2,
                    f"daily_pnl={self.daily_pnl:.2f}",
                    config.CB_LEVEL_2_ACTION,
                )
                self.daily_trading_halted = True
                self.cb_level_2_active = True

        # LEVEL 3 — Weekly loss
        if self.weekly_pnl < -(
            config.CB_LEVEL_3_PCT * config.TOTAL_CAPITAL
        ):
            if not self.cb_level_3_active:
                logger.critical(
                    f"CB L3 TRIGGERED: "
                    f"weekly_pnl={self.weekly_pnl:.2f} "
                    f"action={config.CB_LEVEL_3_ACTION}"
                )
                self._log_circuit_breaker(
                    3,
                    f"weekly_pnl={self.weekly_pnl:.2f}",
                    config.CB_LEVEL_3_ACTION,
                )
                await self._reduce_all_positions_50pct()
                self.cb_level_3_active = True

        # LEVEL 4 — Max drawdown
        drawdown = self.peak_capital - self.current_capital
        if drawdown > config.CB_LEVEL_4_PCT * config.TOTAL_CAPITAL:
            logger.critical(
                f"CB L4 TRIGGERED: drawdown={drawdown:.2f} "
                f"action={config.CB_LEVEL_4_ACTION}"
            )
            self._log_circuit_breaker(
                4,
                f"drawdown={drawdown:.2f}",
                config.CB_LEVEL_4_ACTION,
            )
            await self._emergency_flatten_all()
            self.kill_switch_active = True
            self.cb_level_4_active = True

        # LEVEL 5 — IV spike
        if (
            self.dm.prev_vix
            and self.dm.vix
            and self.dm.prev_vix > 0
        ):
            iv_change = (
                (self.dm.vix - self.dm.prev_vix)
                / self.dm.prev_vix
            )
            if iv_change > config.CB_LEVEL_5_IV_SPIKE_PCT:
                logger.critical(
                    f"CB L5 TRIGGERED: "
                    f"vix_change={iv_change * 100:.1f}% "
                    f"action={config.CB_LEVEL_5_ACTION}"
                )
                self._log_circuit_breaker(
                    5,
                    f"iv_spike={iv_change * 100:.1f}%",
                    config.CB_LEVEL_5_ACTION,
                )
                self.re.previous_regime = (
                    self.re.confirmed_regime
                )
                self.re.confirmed_regime = (
                    config.REGIME_STRONG_BUY
                )
                self.re.regime_changed = True

    async def _monitor_all_positions(self) -> None:
        """Monitor all open positions for exit conditions."""
        for position in list(self.open_positions):
            if position.status != "OPEN":
                continue

            stop_hit = await self._check_stop_loss(position)
            if stop_hit:
                continue

            target_hit = await self._check_profit_target(
                position
            )
            if target_hit:
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
                    continue

    async def _check_stop_loss(
        self, position: Position
    ) -> bool:
        """Check and trigger stop loss based on strategy type."""
        strategy = position.strategy_name

        if strategy == config.STRAT_SHORT_STRADDLE:
            stop_up = position.entry_spot * (
                1 + config.STRADDLE_STOP_PCT
            )
            stop_down = position.entry_spot * (
                1 - config.STRADDLE_STOP_PCT
            )
            if self.dm.spot is None:
                return False
            if (
                self.dm.spot >= stop_up
                or self.dm.spot <= stop_down
            ):
                logger.info(
                    f"Straddle spot stop hit: "
                    f"spot={self.dm.spot:.2f} "
                    f"up={stop_up:.2f} down={stop_down:.2f}"
                )
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True

        elif strategy in [
            config.STRAT_LONG_STRADDLE,
            config.STRAT_STRANGLE,
        ]:
            stop_val = position.total_debit * (
                1 - config.LONG_STRADDLE_STOP_PCT
            )
            current_val = self._get_position_value(position)
            if current_val <= stop_val:
                logger.info(
                    f"Long straddle premium stop hit: "
                    f"current={current_val:.2f} "
                    f"stop={stop_val:.2f}"
                )
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True

        elif strategy == config.STRAT_BACKSPREAD:
            trend = position.trend_direction
            if self.dm.spot is None:
                return False
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
            short_call = self._get_short_strike(
                position, "call"
            )
            short_put = self._get_short_strike(
                position, "put"
            )
            if short_call and self.dm.spot >= short_call:
                logger.info(
                    f"Condor call side breached: "
                    f"spot={self.dm.spot:.2f}"
                )
                await self._close_one_side(
                    position,
                    "call",
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return False
            if short_put and self.dm.spot <= short_put:
                logger.info(
                    f"Condor put side breached: "
                    f"spot={self.dm.spot:.2f}"
                )
                await self._close_one_side(
                    position,
                    "put",
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return False

        elif strategy == config.STRAT_BUTTERFLY:
            if self.dm.spot is None:
                return False
            upper_wing = self._get_upper_wing_strike(position)
            lower_wing = self._get_lower_wing_strike(position)
            if upper_wing and self.dm.spot > (
                upper_wing + config.BUTTERFLY_WING_BUFFER_PTS
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True
            if lower_wing and self.dm.spot < (
                lower_wing - config.BUTTERFLY_WING_BUFFER_PTS
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["STOP_LOSS"],
                )
                return True

        return False

    async def _check_profit_target(
        self, position: Position
    ) -> bool:
        """Check and trigger profit target based on strategy type."""
        strategy = position.strategy_name

        if strategy in [
            config.STRAT_SHORT_STRADDLE,
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
            config.STRAT_RATIO_SPREAD,
        ]:
            current_value = (
                self._get_position_current_premium(position)
            )
            target_credit = (
                position.total_credit
                * (1 - config.PROFIT_TARGET_PCT)
            )
            if (
                current_value <= target_credit
                and position.total_credit > 0
            ):
                logger.info(
                    f"Profit target hit: {strategy} "
                    f"current={current_value:.2f} "
                    f"target={target_credit:.2f}"
                )
                await self._close_position(
                    position,
                    config.EXIT_REASONS["PROFIT_TARGET"],
                )
                return True

        elif strategy in [
            config.STRAT_LONG_STRADDLE,
            config.STRAT_STRANGLE,
        ]:
            current_val = self._get_position_value(position)
            target_val = position.total_debit * (
                1 + config.LONG_STRADDLE_TARGET_PCT
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
            current_val = self._get_position_value(position)
            target_val = (
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
            current_val = self._get_position_value(position)
            max_profit = position.meta.get("max_profit", 0)
            if max_profit > 0 and current_val >= (
                max_profit * config.BUTTERFLY_PROFIT_PCT
            ):
                await self._close_position(
                    position,
                    config.EXIT_REASONS["PROFIT_TARGET"],
                )
                return True

        return False

    async def _check_dte_exit(
        self, position: Position
    ) -> bool:
        """Check and trigger DTE-based exit."""
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
            config.STRAT_SHORT_STRADDLE:  config.STRADDLE_EXIT_DTE,
            config.STRAT_IRON_CONDOR:     config.CONDOR_EXIT_DTE,
            config.STRAT_CREDIT_SPREADS:  config.SPREAD_EXIT_DTE,
            config.STRAT_RATIO_SPREAD:    config.RATIO_EXIT_DTE,
            config.STRAT_BUTTERFLY:       config.BUTTERFLY_EXIT_DTE,
            config.STRAT_BACKSPREAD:      config.BACKSPREAD_EXIT_DTE,
        }

        exit_dte = exit_dte_map.get(position.strategy_name)
        if exit_dte is not None and dte <= exit_dte:
            logger.info(
                f"DTE exit triggered: {position.strategy_name} "
                f"dte={dte} exit_dte={exit_dte}"
            )
            await self._close_position(
                position, config.EXIT_REASONS["TIME_EXIT"]
            )
            return True

        return False

    def _check_max_hold(self, position: Position) -> bool:
        """Check if position has exceeded maximum hold date."""
        if not position.max_hold_date:
            return False
        try:
            max_date = datetime.strptime(
                position.max_hold_date, "%Y-%m-%d"
            ).date()
            if date.today() >= max_date:
                logger.info(
                    f"Max hold date reached: "
                    f"{position.trade_id} "
                    f"max_hold_date={position.max_hold_date}"
                )
                return True
        except ValueError:
            pass
        return False

    async def _handle_regime_transition(self) -> None:
        """Apply regime transition rules A through F."""
        from_regime = self.re.previous_regime
        to_regime = self.re.confirmed_regime

        logger.info(
            f"Regime transition: {from_regime} -> {to_regime}"
        )

        # RULE A — ANY -> STRONG_BUY_VOL
        if to_regime == config.REGIME_STRONG_BUY:
            logger.info(
                "RULE A: Flatten ALL shorts immediately"
            )
            for position in list(self.open_positions):
                has_shorts = any(
                    leg.action == "SELL"
                    for leg in position.legs
                )
                if has_shorts:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["REGIME_CHANGE"],
                        use_market=True,
                    )
            self.cooling_period_end = datetime.now(
                self._IST
            ) + timedelta(minutes=30)
            return

        # RULE B — STRONG_SELL -> MILD_SELL or MILD_SELL -> NEUTRAL
        if (
            (
                from_regime == config.REGIME_STRONG_SELL
                and to_regime == config.REGIME_MILD_SELL
            )
            or (
                from_regime == config.REGIME_MILD_SELL
                and to_regime == config.REGIME_NEUTRAL
            )
        ):
            logger.info("RULE B: Close 50% of shorts")
            for position in list(self.open_positions):
                await self._reduce_position_50pct(position)
            return

        # RULE C — STRONG_SELL -> NEUTRAL
        if (
            from_regime == config.REGIME_STRONG_SELL
            and to_regime == config.REGIME_NEUTRAL
        ):
            logger.info("RULE C: Close 75% of shorts")
            for position in list(self.open_positions):
                await self._reduce_position_pct(position, 0.75)
            return

        # RULE D — MILD_SELL -> BUY_VOL
        if (
            from_regime == config.REGIME_MILD_SELL
            and to_regime == config.REGIME_BUY_VOL
        ):
            logger.info("RULE D: Convert shorts to spreads")
            for position in list(self.open_positions):
                await self._convert_shorts_to_spreads(position)
            return

        # RULE E — NEUTRAL -> STRONG regime
        if from_regime == config.REGIME_NEUTRAL and to_regime in [
            config.REGIME_STRONG_SELL,
            config.REGIME_STRONG_BUY,
        ]:
            if self.re.persistence_count < 3:
                logger.info(
                    f"RULE E: Waiting for 3 confirmations "
                    f"(current={self.re.persistence_count})"
                )
                return
            else:
                logger.info(
                    "RULE E: 3 confirmations — allowing entry"
                )
                return

        # RULE F — Same category
        if self._same_category(from_regime, to_regime):
            logger.info("RULE F: Move stops to breakeven")
            for position in self.open_positions:
                self._move_stop_to_breakeven(position)
            return

    def _same_category(self, r1: str, r2: str) -> bool:
        """Check if two regimes are in the same category."""
        sell = {config.REGIME_STRONG_SELL, config.REGIME_MILD_SELL}
        buy = {config.REGIME_BUY_VOL, config.REGIME_STRONG_BUY}
        if r1 in sell and r2 in sell:
            return True
        if r1 in buy and r2 in buy:
            return True
        if r1 == config.REGIME_NEUTRAL and r2 == config.REGIME_NEUTRAL:
            return True
        return False

    def _should_enter_new_position(self) -> bool:
        """Check all gates before allowing new position entry."""
        if self.kill_switch_active:
            return False
        if self.daily_trading_halted:
            return False

        now = datetime.now(self._IST)
        now_time = now.time()

        if now_time < config.EXEC_START_TIME:
            return False
        if now_time > config.EXEC_END_TIME:
            return False
        if now_time > config.REGIME_FREEZE_TIME:
            return False

        regime = self.re.confirmed_regime
        if regime == config.REGIME_NEUTRAL:
            iv_rank = self.dm.compute_iv_rank()
            adx = self.dm.adx or 99
            if iv_rank <= 50 or adx >= 20:
                return False

        if self.cooling_period_end:
            if now < self.cooling_period_end:
                logger.info("Entry gate: cooling period active")
                return False
            else:
                self.cooling_period_end = None

        if (
            self.re.previous_regime == config.REGIME_NEUTRAL
            and self.re.confirmed_regime in [
                config.REGIME_STRONG_SELL,
                config.REGIME_STRONG_BUY,
            ]
        ):
            if self.re.persistence_count < 3:
                return False

        if len(self.open_positions) >= 2:
            return False

        deployed = sum(
            p.max_risk for p in self.open_positions
        )
        regime_capital = (
            config.REGIME_CAPITAL_PCT.get(regime, 0)
            * config.TOTAL_CAPITAL
        )
        if deployed >= regime_capital:
            return False

        return True

    async def _enter_new_position(self) -> None:
        """Select, build, validate, and execute a new strategy."""
        regime = self.re.confirmed_regime
        strategy_name = self._select_strategy(regime)
        if strategy_name is None:
            logger.info(
                f"No strategy selected for regime={regime}"
            )
            return

        logger.info(
            f"Selected strategy: {strategy_name} "
            f"for regime={regime}"
        )

        legs, meta = await self._build_strategy(strategy_name)
        if legs is None:
            logger.info(
                f"Strategy build failed for {strategy_name}"
            )
            return

        if not await self._pre_trade_checks(
            strategy_name, legs
        ):
            logger.info(
                f"Pre-trade checks failed for {strategy_name}"
            )
            return

        lots = self._calculate_lot_size(strategy_name, meta)
        if lots < 1:
            logger.info(
                f"Lot size=0 for {strategy_name} — skipping"
            )
            return

        for leg in legs:
            leg.qty = leg.qty * lots

        # Generate trade_id BEFORE execution for tag generation
        trade_id = str(uuid.uuid4())

        success = await self._execute_strategy(
            strategy_name, legs, meta, trade_id=trade_id
        )
        if not success:
            logger.warning(
                f"Strategy execution failed for {strategy_name}"
            )
            return

        position = self._create_position_record(
            strategy_name, legs, meta, trade_id=trade_id
        )
        self.open_positions.append(position)
        self.dm.save_position(self._position_to_dict(position))
        logger.info(
            f"New position entered: {strategy_name} "
            f"trade_id={trade_id[:8]} lots={lots}"
        )

    def _select_strategy(self, regime: str) -> Optional[str]:
        """Select appropriate strategy based on regime."""
        adx = self.dm.adx or 0
        atr_contract = self.dm.is_atr_contracting()
        put_iv = self._get_25d_put_iv()
        call_iv = self._get_25d_call_iv()
        skew_diff = put_iv - call_iv
        term_spread = (
            (self.dm.forward_iv or 0) - (self.dm.vix or 0)
        )
        trend_score = self.re.confirmed_trend
        iv_rank = self.dm.compute_iv_rank()
        has_shorts = self._has_short_positions()
        spot = self.dm.spot or 0
        ema_200 = self._get_ema_200()

        if regime == config.REGIME_STRONG_SELL:
            dte = self._get_dte_for_target(
                config.STRADDLE_DTE_MIN,
                config.STRADDLE_DTE_MAX,
            )
            if adx < config.ADX_RANGE_THRESHOLD and atr_contract:
                return config.STRAT_SHORT_STRADDLE
            elif (
                config.ADX_RANGE_THRESHOLD <= adx <= 28
                or skew_diff >= config.SPREAD_SKEW_THRESHOLD
            ):
                return config.STRAT_IRON_CONDOR
            elif dte and dte > 30:
                return config.STRAT_IRON_CONDOR
            else:
                return config.STRAT_SHORT_STRADDLE

        elif regime == config.REGIME_MILD_SELL:
            if skew_diff >= config.SPREAD_SKEW_THRESHOLD:
                return config.STRAT_CREDIT_SPREADS
            elif (
                skew_diff < config.RATIO_SKEW_FLAT_THRESHOLD
                and term_spread > config.RATIO_CONTANGO_THRESHOLD
            ):
                return config.STRAT_RATIO_SPREAD
            else:
                return config.STRAT_CREDIT_SPREADS

        elif regime == config.REGIME_NEUTRAL:
            if iv_rank > 50 and adx < 20:
                return config.STRAT_IRON_CONDOR
            return None

        elif regime == config.REGIME_BUY_VOL:
            if not has_shorts and spot > ema_200:
                return config.STRAT_BUTTERFLY
            elif has_shorts and self._gamma_above_50pct_limit():
                return config.STRAT_DEFENSIVE
            else:
                return config.STRAT_BUTTERFLY

        elif regime == config.REGIME_STRONG_BUY:
            if self.dm.vix and self.dm.vix > config.BACKSPREAD_MAX_VIX:
                return config.STRAT_LONG_STRADDLE
            if trend_score == 0:
                return config.STRAT_LONG_STRADDLE
            elif (
                abs(trend_score) == 1
                and skew_diff < config.RATIO_SKEW_FLAT_THRESHOLD
            ):
                return config.STRAT_BACKSPREAD
            else:
                return config.STRAT_LONG_STRADDLE

        elif regime == config.REGIME_EVENT:
            call_spread = self._get_otm_bid_ask("call")
            put_spread = self._get_otm_bid_ask("put")
            if (
                call_spread < config.EVENT_STRANGLE_MAX_SPREAD_PTS
                and put_spread
                < config.EVENT_STRANGLE_MAX_SPREAD_PTS
            ):
                return config.STRAT_STRANGLE
            else:
                return config.STRAT_LONG_STRADDLE

        return None

    async def _build_strategy(
        self, strategy_name: str
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Dispatch to appropriate strategy builder."""
        builders = {
            config.STRAT_SHORT_STRADDLE: self._build_short_straddle,
            config.STRAT_IRON_CONDOR:    self._build_iron_condor,
            config.STRAT_CREDIT_SPREADS: self._build_credit_spreads,
            config.STRAT_RATIO_SPREAD:   self._build_ratio_spread,
            config.STRAT_BUTTERFLY:      self._build_butterfly,
            config.STRAT_DEFENSIVE:      self._build_defensive_hedge,
            config.STRAT_LONG_STRADDLE:  self._build_long_straddle,
            config.STRAT_BACKSPREAD:     self._build_backspread,
            config.STRAT_STRANGLE:       self._build_long_strangle,
        }
        builder = builders.get(strategy_name)
        if builder is None:
            logger.warning(
                f"No builder for strategy: {strategy_name}"
            )
            return (None, {})
        return await builder()

    async def _build_short_straddle(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build ATM short straddle legs."""
        expiry = self.dm.get_expiry_by_dte(
            config.STRADDLE_DTE_MIN + 5, tolerance=5
        )
        if expiry is None:
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})

        chain = self.dm.option_chain
        if atm not in chain:
            return (None, {})

        call_data = chain[atm]["call"]
        put_data = chain[atm]["put"]

        if (
            call_data["ask"] - call_data["bid"]
        ) > config.MAX_SPREAD_ATM_PTS:
            return (None, {})
        if (
            put_data["ask"] - put_data["bid"]
        ) > config.MAX_SPREAD_ATM_PTS:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte < 5:
            return (None, {})

        total_premium = call_data["ltp"] + put_data["ltp"]

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

        meta = {
            "total_premium":  total_premium,
            "stop_loss_up":   (self.dm.spot or 0) * (
                1 + config.STRADDLE_STOP_PCT
            ),
            "stop_loss_down": (self.dm.spot or 0) * (
                1 - config.STRADDLE_STOP_PCT
            ),
            "profit_target":  total_premium * (
                1 - config.STRADDLE_TARGET_PCT
            ),
            "stop_loss":      total_premium * config.STRADDLE_STOP_PCT,
            "exit_dte":       config.STRADDLE_EXIT_DTE,
            "max_hold_date":  None,
            "max_risk":       total_premium * config.LOT_SIZE,
            "strategy_type":  "SHORT",
        }

        return (legs, meta)

    async def _build_iron_condor(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build wide iron condor legs."""
        expiry = self.dm.get_expiry_by_dte(
            config.CONDOR_DTE_MIN + 7, tolerance=7
        )
        if expiry is None:
            return (None, {})

        spot = self.dm.spot
        if spot is None:
            return (None, {})

        chain = self.dm.option_chain
        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days

        if dte < 5:
            return (None, {})

        vix = self.dm.vix or 16.0
        expected_move = spot * (vix / 100) * ((dte / 365) ** 0.5)

        short_call = (
            round(
                (spot + 1.5 * expected_move)
                / config.NIFTY_STRIKE_STEP
            )
            * config.NIFTY_STRIKE_STEP
        )
        short_put = (
            round(
                (spot - 1.5 * expected_move)
                / config.NIFTY_STRIKE_STEP
            )
            * config.NIFTY_STRIKE_STEP
        )
        long_call = short_call + config.CONDOR_WING_WIDTH
        long_put = short_put - config.CONDOR_WING_WIDTH

        for strike in [short_call, short_put, long_call, long_put]:
            if strike not in chain:
                logger.warning(
                    f"Iron condor: strike {strike} not in chain"
                )
                return (None, {})

        for strike, opt_type, max_spread in [
            (short_call, "call", config.MAX_SPREAD_ATM_PTS),
            (short_put, "put", config.MAX_SPREAD_ATM_PTS),
            (long_call, "call", config.MAX_SPREAD_OTM_PTS),
            (long_put, "put", config.MAX_SPREAD_OTM_PTS),
        ]:
            spread = (
                chain[strike][opt_type]["ask"]
                - chain[strike][opt_type]["bid"]
            )
            if spread > max_spread:
                logger.warning(
                    f"Iron condor: spread too wide at "
                    f"{strike} {opt_type}: {spread:.2f}"
                )
                return (None, {})

        sc_prem = chain[short_call]["call"]["ltp"]
        sp_prem = chain[short_put]["put"]["ltp"]
        lc_prem = chain[long_call]["call"]["ltp"]
        lp_prem = chain[long_put]["put"]["ltp"]

        net_credit = sc_prem + sp_prem - lc_prem - lp_prem

        if net_credit < config.CONDOR_MIN_CREDIT:
            logger.warning(
                f"Iron condor: net_credit={net_credit:.2f} "
                f"< min={config.CONDOR_MIN_CREDIT}"
            )
            return (None, {})

        max_risk = config.CONDOR_WING_WIDTH - net_credit

        legs = [
            Leg(
                instrument_key=chain[long_call]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=long_call, expiry=expiry, qty=1,
                delta=chain[long_call]["call"]["delta"],
                gamma=chain[long_call]["call"]["gamma"],
                vega=chain[long_call]["call"]["vega"],
                theta=chain[long_call]["call"]["theta"],
            ),
            Leg(
                instrument_key=chain[long_put]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=long_put, expiry=expiry, qty=1,
                delta=chain[long_put]["put"]["delta"],
                gamma=chain[long_put]["put"]["gamma"],
                vega=chain[long_put]["put"]["vega"],
                theta=chain[long_put]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[short_call]["call"]["instrument_key"],
                option_type="call", action="SELL",
                strike=short_call, expiry=expiry, qty=1,
                delta=chain[short_call]["call"]["delta"],
                gamma=chain[short_call]["call"]["gamma"],
                vega=chain[short_call]["call"]["vega"],
                theta=chain[short_call]["call"]["theta"],
            ),
            Leg(
                instrument_key=chain[short_put]["put"]["instrument_key"],
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
            "max_risk":      max_risk * config.LOT_SIZE,
            "short_call":    short_call,
            "short_put":     short_put,
            "long_call":     long_call,
            "long_put":      long_put,
            "profit_target": net_credit * (
                1 - config.CONDOR_TARGET_PCT
            ),
            "stop_loss":     net_credit * config.STRADDLE_STOP_PCT,
            "exit_dte":      config.CONDOR_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
            "total_credit":  net_credit,
        }

        logger.info(
            f"Iron condor built: sc={short_call} sp={short_put} "
            f"lc={long_call} lp={long_put} "
            f"credit={net_credit:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_credit_spreads(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build bull put + bear call credit spreads."""
        expiry = self.dm.get_expiry_by_dte(
            config.CONDOR_DTE_MIN + 7, tolerance=7
        )
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte < 5:
            return (None, {})

        short_put_strike = self.dm.get_strike_by_delta(
            "put", config.SPREAD_DELTA_SHORT
        )
        long_put_strike = self.dm.get_strike_by_delta(
            "put", config.SPREAD_DELTA_LONG
        )
        short_call_strike = self.dm.get_strike_by_delta(
            "call", config.SPREAD_DELTA_SHORT
        )
        long_call_strike = self.dm.get_strike_by_delta(
            "call", config.SPREAD_DELTA_LONG
        )

        if any(
            s is None
            for s in [
                short_put_strike, long_put_strike,
                short_call_strike, long_call_strike,
            ]
        ):
            return (None, {})

        if short_put_strike <= long_put_strike:
            return (None, {})
        if short_call_strike >= long_call_strike:
            return (None, {})

        chain = self.dm.option_chain
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

        total_credit = (sp_prem - lp_prem) + (sc_prem - lc_prem)

        if total_credit < config.SPREAD_MIN_CREDIT:
            return (None, {})

        put_spread_width = short_put_strike - long_put_strike
        call_spread_width = long_call_strike - short_call_strike
        max_risk = (
            max(put_spread_width, call_spread_width) - total_credit
        ) * config.LOT_SIZE

        legs = [
            Leg(
                instrument_key=chain[long_put_strike]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=long_put_strike, expiry=expiry, qty=1,
                delta=chain[long_put_strike]["put"]["delta"],
                gamma=chain[long_put_strike]["put"]["gamma"],
                vega=chain[long_put_strike]["put"]["vega"],
                theta=chain[long_put_strike]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[long_call_strike]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=long_call_strike, expiry=expiry, qty=1,
                delta=chain[long_call_strike]["call"]["delta"],
                gamma=chain[long_call_strike]["call"]["gamma"],
                vega=chain[long_call_strike]["call"]["vega"],
                theta=chain[long_call_strike]["call"]["theta"],
            ),
            Leg(
                instrument_key=chain[short_put_strike]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=short_put_strike, expiry=expiry, qty=1,
                delta=chain[short_put_strike]["put"]["delta"],
                gamma=chain[short_put_strike]["put"]["gamma"],
                vega=chain[short_put_strike]["put"]["vega"],
                theta=chain[short_put_strike]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[short_call_strike]["call"]["instrument_key"],
                option_type="call", action="SELL",
                strike=short_call_strike, expiry=expiry, qty=1,
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
            "stop_loss":     total_credit * config.STRADDLE_STOP_PCT,
            "exit_dte":      config.SPREAD_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
        }

        return (legs, meta)

    async def _build_ratio_spread(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build 1x2 ratio spread with separate qty=1 orders."""
        expiry = self.dm.get_expiry_by_dte(
            config.CONDOR_DTE_MIN + 7, tolerance=7
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

        chain = self.dm.option_chain
        call_short = atm
        call_long = atm + config.RATIO_ATM_OFFSET_PTS
        put_short = atm
        put_long = atm - config.RATIO_ATM_OFFSET_PTS

        for s in [call_short, call_long, put_short, put_long]:
            if s not in chain:
                return (None, {})

        cs_prem = chain[call_short]["call"]["ltp"]
        cl_prem = chain[call_long]["call"]["ltp"]
        ps_prem = chain[put_short]["put"]["ltp"]
        pl_prem = chain[put_long]["put"]["ltp"]

        total_credit = (
            (cs_prem - 2 * cl_prem) + (ps_prem - 2 * pl_prem)
        )

        if total_credit <= 0:
            return (None, {})

        legs = [
            Leg(
                instrument_key=chain[call_long]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=call_long, expiry=expiry, qty=1,
            ),
            Leg(
                instrument_key=chain[call_long]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=call_long, expiry=expiry, qty=1,
            ),
            Leg(
                instrument_key=chain[put_long]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=put_long, expiry=expiry, qty=1,
            ),
            Leg(
                instrument_key=chain[put_long]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=put_long, expiry=expiry, qty=1,
            ),
            Leg(
                instrument_key=chain[call_short]["call"]["instrument_key"],
                option_type="call", action="SELL",
                strike=call_short, expiry=expiry, qty=1,
            ),
            Leg(
                instrument_key=chain[put_short]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=put_short, expiry=expiry, qty=1,
            ),
        ]

        meta = {
            "total_credit":  total_credit,
            "max_risk":      total_credit * 2 * config.LOT_SIZE,
            "profit_target": total_credit * (
                1 - config.RATIO_TARGET_PCT
            ),
            "stop_loss":     total_credit * config.STRADDLE_STOP_PCT,
            "exit_dte":      config.RATIO_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
        }

        return (legs, meta)

    async def _build_butterfly(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build long put butterfly."""
        expiry = self.dm.get_expiry_by_dte(4, tolerance=3)
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte > config.BUTTERFLY_DTE_MAX:
            return (None, {})

        strike_a = self.dm.get_strike_by_delta(
            "put", config.BUTTERFLY_DELTA_A
        )
        strike_b = self.dm.get_strike_by_delta(
            "put", config.BUTTERFLY_DELTA_B
        )
        strike_c = self.dm.get_strike_by_delta(
            "put", config.BUTTERFLY_DELTA_C
        )

        if any(s is None for s in [strike_a, strike_b, strike_c]):
            return (None, {})

        chain = self.dm.option_chain
        width_ab = strike_b - strike_a
        width_bc = strike_c - strike_b
        if abs(width_ab - width_bc) > config.NIFTY_STRIKE_STEP:
            strike_c = strike_b + width_ab
            strike_c = (
                round(strike_c / config.NIFTY_STRIKE_STEP)
                * config.NIFTY_STRIKE_STEP
            )
            if strike_c not in chain:
                return (None, {})

        for s in [strike_a, strike_b, strike_c]:
            if s not in chain:
                return (None, {})

        prem_a = chain[strike_a]["put"]["ltp"]
        prem_b = chain[strike_b]["put"]["ltp"]
        prem_c = chain[strike_c]["put"]["ltp"]

        net_debit = (prem_a + prem_c) - (2 * prem_b)

        if net_debit > config.BUTTERFLY_MAX_DEBIT_PTS:
            return (None, {})
        if net_debit <= 0:
            return (None, {})

        max_profit = (strike_b - strike_a) - net_debit
        rr_ratio = max_profit / net_debit if net_debit > 0 else 0

        if rr_ratio < config.BUTTERFLY_MIN_RR_RATIO:
            return (None, {})

        legs = [
            Leg(
                instrument_key=chain[strike_a]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=strike_a, expiry=expiry, qty=1,
                delta=chain[strike_a]["put"]["delta"],
                gamma=chain[strike_a]["put"]["gamma"],
                vega=chain[strike_a]["put"]["vega"],
                theta=chain[strike_a]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[strike_c]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=strike_c, expiry=expiry, qty=1,
                delta=chain[strike_c]["put"]["delta"],
                gamma=chain[strike_c]["put"]["gamma"],
                vega=chain[strike_c]["put"]["vega"],
                theta=chain[strike_c]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[strike_b]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=strike_b, expiry=expiry, qty=1,
                delta=chain[strike_b]["put"]["delta"],
                gamma=chain[strike_b]["put"]["gamma"],
                vega=chain[strike_b]["put"]["vega"],
                theta=chain[strike_b]["put"]["theta"],
            ),
            Leg(
                instrument_key=chain[strike_b]["put"]["instrument_key"],
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
            "profit_target": max_profit * config.BUTTERFLY_PROFIT_PCT,
            "stop_loss":     net_debit * config.LOT_SIZE,
            "exit_dte":      config.BUTTERFLY_EXIT_DTE,
            "max_hold_date": (
                date.today() + timedelta(days=dte - 1)
            ).strftime("%Y-%m-%d"),
            "strategy_type": "LONG",
        }

        return (legs, meta)

    async def _build_long_straddle(
        self,
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Build ATM long straddle."""
        expiry = self.dm.get_expiry_by_dte(
            config.LONG_STRADDLE_DTE_MIN + 5, tolerance=5
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

        chain = self.dm.option_chain
        if atm not in chain:
            return (None, {})

        spot = self.dm.spot or 0
        vix = self.dm.vix or 16.0
        call_data = chain[atm]["call"]
        put_data = chain[atm]["put"]

        total_debit = call_data["ltp"] + put_data["ltp"]
        max_allowed = spot * config.LONG_STRADDLE_MAX_DEBIT_PCT
        if total_debit > max_allowed:
            return (None, {})

        if len(self.dm.vix_history_20d) >= config.LONG_STRADDLE_VIX_SMA_PERIOD:
            vix_arr = list(self.dm.vix_history_20d)
            vix_sma = float(
                np.mean(vix_arr[-config.LONG_STRADDLE_VIX_SMA_PERIOD:])
            )
            if vix_sma > 0:
                vix_spike = (vix - vix_sma) / vix_sma
                if vix_spike < config.LONG_STRADDLE_VIX_SPIKE_PCT:
                    return (None, {})

        iv_rank = self.dm.compute_iv_rank()
        if iv_rank > config.LONG_STRADDLE_MAX_IV_RANK:
            return (None, {})

        if (call_data["ask"] - call_data["bid"]) > config.MAX_SPREAD_ATM_PTS:
            return (None, {})
        if (put_data["ask"] - put_data["bid"]) > config.MAX_SPREAD_ATM_PTS:
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
            config.BACKSPREAD_DTE_MIN + 2, tolerance=2
        )
        if expiry is None:
            return (None, {})

        dte = (
            datetime.strptime(expiry, "%Y-%m-%d").date()
            - date.today()
        ).days
        if dte < config.BACKSPREAD_DTE_MIN or dte > config.BACKSPREAD_DTE_MAX:
            return (None, {})

        vix = self.dm.vix or 16.0
        if vix > config.BACKSPREAD_MAX_VIX:
            return (None, {})

        trend = self.re.confirmed_trend
        chain = self.dm.option_chain
        spot = self.dm.spot or 0

        if trend >= 0:
            long_strike = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_LONG_DELTA
            )
            short_strike = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_SHORT_DELTA
            )
            hedge_strike = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_LONG_DELTA
            )
            hedge_short = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_SHORT_DELTA
            )

            if any(
                s is None
                for s in [
                    long_strike, short_strike,
                    hedge_strike, hedge_short,
                ]
            ):
                return (None, {})

            if (
                short_strike - long_strike
            ) < config.BACKSPREAD_MIN_STRIKE_WIDTH:
                return (None, {})

            for s in [long_strike, short_strike, hedge_strike, hedge_short]:
                if s not in chain:
                    return (None, {})

            long_prem = chain[long_strike]["call"]["ltp"]
            short_prem = chain[short_strike]["call"]["ltp"]
            hedge_prem = chain[hedge_strike]["put"]["ltp"]
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
                    instrument_key=chain[long_strike]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=long_strike, expiry=expiry, qty=1,
                    delta=chain[long_strike]["call"]["delta"],
                    gamma=chain[long_strike]["call"]["gamma"],
                    vega=chain[long_strike]["call"]["vega"],
                    theta=chain[long_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[long_strike]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=long_strike, expiry=expiry, qty=1,
                    delta=chain[long_strike]["call"]["delta"],
                    gamma=chain[long_strike]["call"]["gamma"],
                    vega=chain[long_strike]["call"]["vega"],
                    theta=chain[long_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[long_strike]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=long_strike, expiry=expiry, qty=1,
                    delta=chain[long_strike]["call"]["delta"],
                    gamma=chain[long_strike]["call"]["gamma"],
                    vega=chain[long_strike]["call"]["vega"],
                    theta=chain[long_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_strike]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=hedge_strike, expiry=expiry, qty=1,
                    delta=chain[hedge_strike]["put"]["delta"],
                    gamma=chain[hedge_strike]["put"]["gamma"],
                    vega=chain[hedge_strike]["put"]["vega"],
                    theta=chain[hedge_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[short_strike]["call"]["instrument_key"],
                    option_type="call", action="SELL",
                    strike=short_strike, expiry=expiry, qty=1,
                    delta=chain[short_strike]["call"]["delta"],
                    gamma=chain[short_strike]["call"]["gamma"],
                    vega=chain[short_strike]["call"]["vega"],
                    theta=chain[short_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_short]["put"]["instrument_key"],
                    option_type="put", action="SELL",
                    strike=hedge_short, expiry=expiry, qty=1,
                    delta=chain[hedge_short]["put"]["delta"],
                    gamma=chain[hedge_short]["put"]["gamma"],
                    vega=chain[hedge_short]["put"]["vega"],
                    theta=chain[hedge_short]["put"]["theta"],
                ),
            ]
        else:
            long_strike = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_LONG_DELTA
            )
            short_strike = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_SHORT_DELTA
            )
            hedge_strike = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_LONG_DELTA
            )
            hedge_short = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_SHORT_DELTA
            )

            if any(
                s is None
                for s in [
                    long_strike, short_strike,
                    hedge_strike, hedge_short,
                ]
            ):
                return (None, {})

            for s in [long_strike, short_strike, hedge_strike, hedge_short]:
                if s not in chain:
                    return (None, {})

            long_prem = chain[long_strike]["put"]["ltp"]
            short_prem = chain[short_strike]["put"]["ltp"]
            hedge_prem = chain[hedge_strike]["call"]["ltp"]
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
                    instrument_key=chain[long_strike]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=long_strike, expiry=expiry, qty=1,
                    delta=chain[long_strike]["put"]["delta"],
                    gamma=chain[long_strike]["put"]["gamma"],
                    vega=chain[long_strike]["put"]["vega"],
                    theta=chain[long_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[long_strike]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=long_strike, expiry=expiry, qty=1,
                    delta=chain[long_strike]["put"]["delta"],
                    gamma=chain[long_strike]["put"]["gamma"],
                    vega=chain[long_strike]["put"]["vega"],
                    theta=chain[long_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[long_strike]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=long_strike, expiry=expiry, qty=1,
                    delta=chain[long_strike]["put"]["delta"],
                    gamma=chain[long_strike]["put"]["gamma"],
                    vega=chain[long_strike]["put"]["vega"],
                    theta=chain[long_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_strike]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=hedge_strike, expiry=expiry, qty=1,
                    delta=chain[hedge_strike]["call"]["delta"],
                    gamma=chain[hedge_strike]["call"]["gamma"],
                    vega=chain[hedge_strike]["call"]["vega"],
                    theta=chain[hedge_strike]["call"]["theta"],
                ),
                Leg(
                    instrument_key=chain[short_strike]["put"]["instrument_key"],
                    option_type="put", action="SELL",
                    strike=short_strike, expiry=expiry, qty=1,
                    delta=chain[short_strike]["put"]["delta"],
                    gamma=chain[short_strike]["put"]["gamma"],
                    vega=chain[short_strike]["put"]["vega"],
                    theta=chain[short_strike]["put"]["theta"],
                ),
                Leg(
                    instrument_key=chain[hedge_short]["call"]["instrument_key"],
                    option_type="call", action="SELL",
                    strike=hedge_short, expiry=expiry, qty=1,
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

        meta = {
            "net_debit":       max(net_debit, 0.05),
            "max_risk":        max(net_debit, 0.05) * config.LOT_SIZE,
            "profit_target":   max(net_debit, 0.05) * config.BACKSPREAD_PROFIT_MULTIPLE,
            "stop_loss":       max(net_debit, 0.05) * 0.40,
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
        expiry = self.dm.get_expiry_by_dte(7, tolerance=3)
        if expiry is None:
            return (None, {})

        call_strike = self.dm.get_strike_by_delta(
            "call", config.EVENT_STRANGLE_DELTA
        )
        put_strike = self.dm.get_strike_by_delta(
            "put", config.EVENT_STRANGLE_DELTA
        )

        if call_strike is None or put_strike is None:
            return (None, {})

        chain = self.dm.option_chain
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
            return await self._build_long_straddle()

        total_debit = (
            chain[call_strike]["call"]["ltp"]
            + chain[put_strike]["put"]["ltp"]
        )
        max_hold_date = (
            date.today() + timedelta(days=2)
        ).strftime("%Y-%m-%d")

        legs = [
            Leg(
                instrument_key=chain[call_strike]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=call_strike, expiry=expiry, qty=1,
                delta=chain[call_strike]["call"]["delta"],
                gamma=chain[call_strike]["call"]["gamma"],
                vega=chain[call_strike]["call"]["vega"],
                theta=chain[call_strike]["call"]["theta"],
            ),
            Leg(
                instrument_key=chain[put_strike]["put"]["instrument_key"],
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

        if len(self.dm.vix_history_20d) >= config.DEFENSIVE_VIX_SMA_PERIOD:
            vix_arr = list(self.dm.vix_history_20d)
            vix_sma = float(
                np.mean(vix_arr[-config.DEFENSIVE_VIX_SMA_PERIOD:])
            )
            if vix_sma > 0:
                vix_spike = (self.dm.vix - vix_sma) / vix_sma
                if vix_spike < config.DEFENSIVE_VIX_SPIKE_PCT:
                    return (None, {})

        ema_20 = self._compute_ema_n(config.DEFENSIVE_EMA_PERIOD)
        if self.dm.spot and self.dm.spot > ema_20:
            return (None, {})

        total_delta = sum(
            leg.delta * leg.qty * config.LOT_SIZE
            for pos in self.open_positions
            for leg in pos.legs
            if leg.action == "SELL"
        )

        reduction_legs = []
        for pos in self.open_positions:
            for leg in pos.legs:
                if leg.action == "SELL":
                    reduce_qty = _math.ceil(
                        leg.qty * config.DEFENSIVE_REDUCTION_PCT
                    )
                    reduction_legs.append({
                        "position":   pos,
                        "leg":        leg,
                        "reduce_qty": reduce_qty,
                    })

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})

        chain = self.dm.option_chain
        if atm not in chain:
            return (None, {})

        expiry = self.dm.get_expiry_by_dte(
            config.LONG_STRADDLE_DTE_MIN, tolerance=5
        )
        if expiry is None:
            return (None, {})

        atm_put_data = chain[atm]["put"]
        atm_put_delta = abs(atm_put_data["delta"])

        remaining_delta = total_delta * (
            1 - config.DEFENSIVE_REDUCTION_PCT
        )
        hedge_qty = _math.ceil(
            abs(remaining_delta)
            / (atm_put_delta * config.LOT_SIZE + 1e-10)
        )
        hedge_qty = max(1, hedge_qty)

        legs = [
            Leg(
                instrument_key=atm_put_data["instrument_key"],
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
            "total_debit":    atm_put_data["ltp"] * hedge_qty,
            "max_risk":       (
                atm_put_data["ltp"] * hedge_qty * config.LOT_SIZE
            ),
            "stop_loss":      (
                atm_put_data["ltp"]
                * hedge_qty
                * (1 - config.EVENT_STRANGLE_STOP_PCT)
            ),
            "profit_target":  None,
            "exit_dte":       None,
            "max_hold_date":  max_hold_date,
            "strategy_type":  "LONG",
            "reduction_legs": reduction_legs,
            "hedge_qty":      hedge_qty,
        }

        return (legs, meta)

    async def _pre_trade_checks(
        self, strategy_name: str, legs: List[Leg]
    ) -> bool:
        """Run all pre-trade validation checks."""
        for leg in legs:
            if leg.action == "SELL":
                try:
                    expiry_date = datetime.strptime(
                        leg.expiry, "%Y-%m-%d"
                    ).date()
                    dte = (expiry_date - date.today()).days
                    if dte < 5:
                        logger.warning(
                            f"Pre-trade: DTE={dte} < 5 "
                            f"for SELL leg strike={leg.strike}"
                        )
                        return False
                except ValueError:
                    return False

        chain = self.dm.option_chain
        for leg in legs:
            strike_data = chain.get(leg.strike, {})
            opt_data = strike_data.get(leg.option_type, {})
            spread = (
                opt_data.get("ask", 0) - opt_data.get("bid", 0)
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
                    f"max={max_spread} for "
                    f"{leg.option_type} strike={leg.strike}"
                )
                return False

        estimated_risk = self._estimate_max_loss(
            strategy_name, legs
        )
        current_risk = sum(
            p.max_risk for p in self.open_positions
        )
        if current_risk + estimated_risk > config.MAX_COMBINED_RISK:
            logger.warning(
                f"Pre-trade: portfolio risk limit breach "
                f"current={current_risk:.0f} "
                f"new={estimated_risk:.0f}"
            )
            return False

        new_greeks = self._estimate_greeks_impact(legs)
        regime = self.re.confirmed_regime
        limits = config.GREEKS_LIMITS.get(regime, {})
        portfolio_greeks = self._get_portfolio_greeks()
        post_delta = (
            portfolio_greeks["delta"] + new_greeks["delta"]
        )
        delta_max = limits.get("delta_max", 99)
        if abs(post_delta) > delta_max:
            logger.warning(
                f"Pre-trade: delta limit breach "
                f"post_delta={post_delta:.3f} max={delta_max}"
            )
            return False

        if not config.PAPER_TRADING_MODE:
            margin_legs = [
                {
                    "instrument_key":   leg.instrument_key,
                    "quantity":         leg.qty * config.LOT_SIZE,
                    "transaction_type": leg.action,
                    "product":          "D",
                    "price":            leg.entry_price,
                }
                for leg in legs
            ]
            margin_ok, required = await self.dm.check_margin(
                margin_legs
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
        legs: List[Leg],
        meta: Dict,
        trade_id: str = "",
    ) -> bool:
        """
        Execute all legs with idempotent order placement.
        CRITICAL FIX 1+3: Each leg gets a deterministic tag.
        """
        if not trade_id:
            trade_id = str(uuid.uuid4())

        if strategy_name == config.STRAT_DEFENSIVE:
            reduction_success = await self._execute_reductions(
                meta.get("reduction_legs", [])
            )
            if not reduction_success:
                return False
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        long_legs = [l for l in legs if l.action == "BUY"]
        short_legs = [l for l in legs if l.action == "SELL"]
        filled_legs: List[Leg] = []

        # Execute long legs first (RULE O1)
        for idx, leg in enumerate(long_legs):
            success, order_id = await self._place_single_leg(
                leg,
                use_market=False,
                trade_id=trade_id,
                leg_index=idx,
            )
            if not success:
                logger.warning(
                    f"Long leg failed: {leg.option_type} "
                    f"strike={leg.strike} — aborting"
                )
                await self._cancel_and_reverse(filled_legs)
                return False
            leg.order_id = order_id
            leg.fill_status = "COMPLETE"
            filled_legs.append(leg)
            self._log_order_detail(
                trade_id, order_id, leg, "FILLED",
                leg.entry_price,
            )
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        # Execute short legs second (RULE O1)
        for idx, leg in enumerate(short_legs):
            success, order_id = await self._place_single_leg(
                leg,
                use_market=False,
                trade_id=trade_id,
                leg_index=len(long_legs) + idx,
            )
            if not success:
                logger.warning(
                    f"Short leg failed: {leg.option_type} "
                    f"strike={leg.strike} — aborting"
                )
                await self._cancel_and_reverse(filled_legs)
                return False
            leg.order_id = order_id
            leg.fill_status = "COMPLETE"
            filled_legs.append(leg)
            self._log_order_detail(
                trade_id, order_id, leg, "FILLED",
                leg.entry_price,
            )
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        logger.info(
            f"All {len(filled_legs)} legs filled for "
            f"{strategy_name}"
        )
        return True

    def _log_order_detail(
        self,
        trade_id: str,
        order_id: str,
        leg: Leg,
        status: str,
        fill_price: float = 0.0,
    ) -> None:
        """Log order details to SQLite order_log table."""
        try:
            IST = pytz.timezone(config.TZ)
            conn = sqlite3.connect(self.db_path)
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
                datetime.now(IST).isoformat(),
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
        position: Position,
        exit_reason: str,
        use_market: bool = False,
    ) -> None:
        """Close all legs of a position."""
        if position.status != "OPEN":
            return

        logger.info(
            f"Closing position: {position.trade_id[:8]} "
            f"strategy={position.strategy_name} "
            f"reason={exit_reason}"
        )

        use_market_order = use_market or exit_reason in [
            config.EXIT_REASONS["STOP_LOSS"],
            config.EXIT_REASONS["CIRCUIT_BREAK"],
            config.EXIT_REASONS["REGIME_CHANGE"],
        ]

        short_legs = [
            l for l in position.legs if l.action == "SELL"
        ]
        long_legs = [
            l for l in position.legs if l.action == "BUY"
        ]

        for idx, leg in enumerate(short_legs + long_legs):
            if leg.qty <= 0:
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

            # Generate exit tag (different from entry tag)
            exit_tag = self._generate_order_tag(
                f"exit-{position.trade_id[:12]}",
                leg.instrument_key,
                close_action,
                idx,
            )
            close_leg.order_tag = exit_tag

            success, order_id = await self._place_single_leg(
                close_leg,
                use_market=use_market_order,
                trade_id=f"exit-{position.trade_id}",
                leg_index=idx,
            )

            if not success:
                logger.warning(
                    f"Close leg failed — retrying at market: "
                    f"{leg.option_type} strike={leg.strike}"
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
                    trade_id=f"exit-retry-{position.trade_id}",
                    leg_index=idx,
                )
                leg.exit_price = retry_leg.entry_price
            else:
                leg.exit_price = close_leg.entry_price

            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )

        IST = pytz.timezone(config.TZ)
        position.exit_reason = exit_reason
        position.exit_timestamp = datetime.now(IST).isoformat()
        position.exit_spot = self.dm.spot or 0.0
        position.exit_vix = self.dm.vix or 0.0
        position.status = "CLOSED"

        # HIGH FIX 4: Calculate gross, costs, and net PnL
        gross_pnl, tx_costs, net_pnl = (
            self._calculate_final_pnl(position)
        )
        position.realized_pnl = gross_pnl
        position.transaction_costs = tx_costs
        position.net_pnl = net_pnl
        position.realized_pnl_percent = (
            (net_pnl / config.TOTAL_CAPITAL) * 100
            if config.TOTAL_CAPITAL > 0 else 0.0
        )

        if position in self.open_positions:
            self.open_positions.remove(position)
        self.closed_positions.append(position)

        # Update capital using NET PnL (after costs)
        self.daily_pnl += net_pnl
        self.weekly_pnl += net_pnl
        self.current_capital += net_pnl
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital

        self.dm.close_position(
            position.trade_id,
            self._position_to_dict(position),
        )

        logger.info(
            f"Position closed: {position.trade_id[:8]} "
            f"gross=₹{gross_pnl:,.2f} "
            f"costs=₹{tx_costs:,.2f} "
            f"net=₹{net_pnl:,.2f} "
            f"reason={exit_reason}"
        )

    def _calculate_lot_size(
        self, strategy_name: str, meta: Dict
    ) -> int:
        """Calculate appropriate lot size."""
        max_loss_per_lot = meta.get("max_risk", 0)
        if max_loss_per_lot <= 0:
            return 1

        risk_per_trade = config.MAX_RISK_PER_TRADE
        lots = math.floor(risk_per_trade / max_loss_per_lot)

        regime = self.re.confirmed_regime
        max_lots = config.REGIME_MAX_LOTS.get(regime, 1)
        lots = min(lots, max_lots)
        lots = max(lots, 1)

        regime_capital = (
            config.REGIME_CAPITAL_PCT.get(regime, 0)
            * config.TOTAL_CAPITAL
        )
        deployed_capital = sum(
            p.max_risk for p in self.open_positions
        )
        available_capital = regime_capital - deployed_capital

        if available_capital <= 0:
            return 0

        if strategy_name == config.STRAT_RATIO_SPREAD:
            ratio_cap = (
                config.RATIO_MAX_CAPITAL_PCT * config.TOTAL_CAPITAL
            )
            available_capital = min(available_capital, ratio_cap)

        lots_by_capital = math.floor(
            available_capital / max_loss_per_lot
        )
        lots = min(lots, lots_by_capital)
        lots = max(lots, 0)

        logger.info(
            f"Lot size: {lots} for {strategy_name} "
            f"risk={risk_per_trade} "
            f"max_loss={max_loss_per_lot:.0f}"
        )
        return lots

    def _get_position_value(
        self, position: Position
    ) -> float:
        """Get current total value from live LTP."""
        total_value = 0.0
        chain = self.dm.option_chain
        for leg in position.legs:
            ltp = (
                chain.get(leg.strike, {})
                .get(leg.option_type, {})
                .get("ltp", 0)
            )
            if ltp == 0:
                ltp = leg.entry_price
            total_value += ltp * leg.qty
        return total_value

    def _get_position_current_premium(
        self, position: Position
    ) -> float:
        """Get current total premium for credit strategies."""
        total_premium = 0.0
        chain = self.dm.option_chain
        for leg in position.legs:
            if leg.action == "SELL":
                ltp = (
                    chain.get(leg.strike, {})
                    .get(leg.option_type, {})
                    .get("ltp", 0)
                )
                if ltp == 0:
                    ltp = leg.entry_price
                total_premium += ltp * leg.qty
        return total_premium

    def _get_portfolio_greeks(self) -> Dict[str, float]:
        """Compute aggregate portfolio Greeks."""
        total_delta = 0.0
        total_gamma = 0.0
        total_vega = 0.0
        total_theta = 0.0

        for position in self.open_positions:
            for leg in position.legs:
                sign = +1 if leg.action == "BUY" else -1
                total_delta += (
                    sign * leg.delta * leg.qty * config.LOT_SIZE
                )
                total_gamma += (
                    sign * leg.gamma * leg.qty * config.LOT_SIZE
                )
                total_vega += (
                    sign * leg.vega * leg.qty * config.LOT_SIZE
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
        """Estimate Greeks impact of new legs on portfolio."""
        delta = 0.0
        gamma = 0.0
        vega = 0.0
        theta = 0.0
        for leg in legs:
            sign = +1 if leg.action == "BUY" else -1
            delta += sign * leg.delta * leg.qty * config.LOT_SIZE
            gamma += sign * leg.gamma * leg.qty * config.LOT_SIZE
            vega  += sign * leg.vega  * leg.qty * config.LOT_SIZE
            theta += sign * leg.theta * leg.qty * config.LOT_SIZE
        return {
            "delta": delta, "gamma": gamma,
            "vega": vega, "theta": theta,
        }

    def _estimate_max_loss(
        self, strategy_name: str, legs: List[Leg]
    ) -> float:
        """Estimate maximum possible loss for strategy."""
        if strategy_name == config.STRAT_SHORT_STRADDLE:
            total_prem = sum(
                l.entry_price for l in legs
                if l.action == "SELL"
            )
            return (
                total_prem * config.STRADDLE_STOP_PCT
                * config.LOT_SIZE
            )
        elif strategy_name == config.STRAT_IRON_CONDOR:
            net_credit = (
                sum(l.entry_price for l in legs if l.action == "SELL")
                - sum(l.entry_price for l in legs if l.action == "BUY")
            )
            return (
                config.CONDOR_WING_WIDTH - net_credit
            ) * config.LOT_SIZE
        elif strategy_name == config.STRAT_CREDIT_SPREADS:
            net_credit = (
                sum(l.entry_price for l in legs if l.action == "SELL")
                - sum(l.entry_price for l in legs if l.action == "BUY")
            )
            spread_width = config.CONDOR_WING_WIDTH / 2
            return max(
                0, (spread_width - net_credit) * config.LOT_SIZE
            )
        elif strategy_name in [
            config.STRAT_LONG_STRADDLE,
            config.STRAT_STRANGLE,
            config.STRAT_BUTTERFLY,
            config.STRAT_BACKSPREAD,
            config.STRAT_DEFENSIVE,
        ]:
            total_debit = (
                sum(l.entry_price for l in legs if l.action == "BUY")
                - sum(l.entry_price for l in legs if l.action == "SELL")
            )
            return max(0, total_debit * config.LOT_SIZE)
        elif strategy_name == config.STRAT_RATIO_SPREAD:
            total_debit = (
                sum(l.entry_price for l in legs if l.action == "BUY")
                - sum(l.entry_price for l in legs if l.action == "SELL")
            )
            return max(0, total_debit * 2 * config.LOT_SIZE)
        return float(config.MAX_RISK_PER_TRADE)

    def _check_greeks_limits(self) -> None:
        """Check portfolio Greeks against regime limits."""
        regime = self.re.confirmed_regime
        limits = config.GREEKS_LIMITS.get(regime, {})
        greeks = self._get_portfolio_greeks()

        delta_max = limits.get("delta_max", 99)
        if abs(greeks["delta"]) > delta_max:
            logger.warning(
                f"Delta breach: {greeks['delta']:.3f} "
                f"> limit={delta_max} — scheduling hedge"
            )
            asyncio.create_task(
                self._hedge_delta(
                    greeks["delta"], delta_max
                )
            )

        gamma_max = limits.get("gamma_max", 99)
        gamma_min = limits.get("gamma_min", -99)
        if gamma_min is not None and greeks["gamma"] < gamma_min:
            logger.warning(
                f"Gamma below minimum: "
                f"{greeks['gamma']:.5f} < {gamma_min}"
            )
        if gamma_max is not None and greeks["gamma"] > gamma_max:
            logger.warning(
                f"Gamma above maximum: "
                f"{greeks['gamma']:.5f} > {gamma_max}"
            )

        vega_max = limits.get("vega_max", 99999)
        vega_min = limits.get("vega_min", -99999)
        if vega_min is not None and greeks["vega"] < vega_min:
            logger.warning(
                f"Vega below minimum: "
                f"{greeks['vega']:.1f} < {vega_min}"
            )

        theta_min = limits.get("theta_min")
        if theta_min is not None:
            if greeks["theta"] < theta_min:
                logger.warning(
                    f"Theta below minimum: "
                    f"{greeks['theta']:.1f} < {theta_min}"
                )

    async def _hedge_delta(
        self, current_delta: float, delta_limit: float
    ) -> None:
        """Hedge excess delta using Nifty futures."""
        excess = abs(current_delta) - delta_limit
        if excess <= 0:
            return

        futures_lots = math.ceil(excess / 1.0)
        action = "SELL" if current_delta > delta_limit else "BUY"

        if config.PAPER_TRADING_MODE:
            logger.info(
                f"Paper delta hedge: {action} "
                f"{futures_lots} Nifty futures "
                f"current_delta={current_delta:.3f}"
            )
            return

        payload = {
            "quantity":           futures_lots * config.LOT_SIZE,
            "product":            "D",
            "validity":           "DAY",
            "price":              0,
            "instrument_token":   config.INSTRUMENT_NIFTY_FUT,
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
                f"Delta hedge placed: {action} "
                f"{futures_lots} lots"
            )
        except Exception as e:
            logger.error(f"Delta hedge failed: {e}")

    async def _reduce_position_50pct(
        self, position: Position
    ) -> None:
        """Reduce all short legs by 50%."""
        for idx, leg in enumerate(position.legs):
            if leg.action == "SELL":
                reduce_qty = math.floor(leg.qty * 0.50)
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
                    use_market=False,
                    trade_id=f"reduce-{position.trade_id}",
                    leg_index=idx,
                )
                if success:
                    leg.qty -= reduce_qty
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )
        self._move_stop_to_breakeven(position)

    async def _reduce_position_pct(
        self, position: Position, pct: float
    ) -> None:
        """Reduce all short legs by given percentage."""
        for idx, leg in enumerate(position.legs):
            if leg.action == "SELL":
                reduce_qty = math.floor(leg.qty * pct)
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
                    use_market=False,
                    trade_id=f"reduce-pct-{position.trade_id}",
                    leg_index=idx,
                )
                if success:
                    leg.qty -= reduce_qty
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )

    async def _convert_shorts_to_spreads(
        self, position: Position
    ) -> None:
        """Add hedge legs to convert naked shorts into spreads."""
        chain = self.dm.option_chain
        for idx, leg in enumerate(position.legs):
            if leg.action == "SELL":
                if leg.option_type == "put":
                    hedge_strike = (
                        leg.strike - config.CONDOR_WING_WIDTH // 3
                    )
                else:
                    hedge_strike = (
                        leg.strike + config.CONDOR_WING_WIDTH // 3
                    )

                hedge_strike = (
                    round(
                        hedge_strike / config.NIFTY_STRIKE_STEP
                    ) * config.NIFTY_STRIKE_STEP
                )

                if hedge_strike not in chain:
                    continue

                hedge_leg = Leg(
                    instrument_key=chain[hedge_strike][
                        leg.option_type
                    ]["instrument_key"],
                    option_type=leg.option_type,
                    action="BUY",
                    strike=hedge_strike,
                    expiry=leg.expiry,
                    qty=leg.qty,
                    delta=chain[hedge_strike][leg.option_type]["delta"],
                    gamma=chain[hedge_strike][leg.option_type]["gamma"],
                    vega=chain[hedge_strike][leg.option_type]["vega"],
                    theta=chain[hedge_strike][leg.option_type]["theta"],
                )
                success, order_id = await self._place_single_leg(
                    hedge_leg,
                    use_market=False,
                    trade_id=f"convert-{position.trade_id}",
                    leg_index=idx,
                )
                if success:
                    position.legs.append(hedge_leg)
                    logger.info(
                        f"Converted short {leg.strike} to spread "
                        f"with hedge at {hedge_strike}"
                    )
                await asyncio.sleep(
                    config.ORDER_BETWEEN_LEGS_DELAY_SEC
                )

    def _move_stop_to_breakeven(
        self, position: Position
    ) -> None:
        """Move stop loss to breakeven."""
        if position.strategy_name == config.STRAT_SHORT_STRADDLE:
            position.stop_loss = position.entry_spot
            logger.info(
                f"Stop moved to breakeven: "
                f"{position.entry_spot:.2f} "
                f"for {position.trade_id[:8]}"
            )
        elif position.strategy_name in [
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
        ]:
            position.stop_loss = 0.0
            logger.info(
                f"Stop moved to breakeven (credit recovered) "
                f"for {position.trade_id[:8]}"
            )

    async def _emergency_flatten_all(self) -> None:
        """Emergency close all positions at market."""
        logger.critical(
            "EMERGENCY: Flattening all positions at market"
        )
        # CRITICAL FIX 2: Cancel all open orders first
        await self.cancel_all_open_orders(
            context="EMERGENCY_FLATTEN"
        )
        for position in list(self.open_positions):
            await self._close_position(
                position,
                config.EXIT_REASONS["CIRCUIT_BREAK"],
                use_market=True,
            )
        logger.critical("All positions flattened")

    async def _reduce_all_positions_50pct(self) -> None:
        """Reduce all open positions by 50% (CB Level 3)."""
        logger.warning("CB L3: Reducing all positions by 50%")
        for position in list(self.open_positions):
            await self._reduce_position_50pct(position)

    async def _execute_reductions(
        self, reduction_legs: List[Dict]
    ) -> bool:
        """Execute short leg reductions for defensive hedge."""
        for idx, item in enumerate(reduction_legs):
            leg = item["leg"]
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
                trade_id=f"defensive-reduce-{idx}",
                leg_index=idx,
            )
            if not success:
                logger.warning(
                    f"Reduction failed for strike={leg.strike}"
                )
                return False
            leg.qty -= reduce_qty
            await asyncio.sleep(
                config.ORDER_BETWEEN_LEGS_DELAY_SEC
            )
        return True

    async def _close_one_side(
        self,
        position: Position,
        option_type: str,
        exit_reason: str,
    ) -> None:
        """Close only one side of a multi-leg position."""
        side_legs = [
            l for l in position.legs
            if l.option_type == option_type
        ]
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
            asyncio.create_task(
                self._place_single_leg(
                    close_leg,
                    use_market=True,
                    trade_id=f"oneside-{position.trade_id}",
                    leg_index=idx,
                )
            )
        logger.info(
            f"Closed {option_type} side of "
            f"{position.trade_id[:8]} reason={exit_reason}"
        )

    async def _reconcile_with_broker(self) -> None:
        """Reconcile local positions with broker on startup."""
        if config.PAPER_TRADING_MODE:
            return

        try:
            broker_positions = await self.dm._api_get(
                config.EP_POSITIONS, {}
            )
            if not broker_positions:
                return

            broker_map: Dict[str, int] = {}
            pos_list = (
                broker_positions
                if isinstance(broker_positions, list)
                else broker_positions.get("data", [])
            )
            for pos in pos_list:
                key = pos.get("instrument_token", "")
                qty = int(pos.get("quantity", 0))
                if key and qty != 0:
                    broker_map[key] = qty

            for position in self.open_positions:
                for leg in position.legs:
                    broker_qty = broker_map.get(
                        leg.instrument_key, 0
                    )
                    local_qty = leg.qty * config.LOT_SIZE

                    if broker_qty == 0 and local_qty != 0:
                        logger.warning(
                            f"Position mismatch: local has "
                            f"{local_qty} units but broker has 0 "
                            f"for {leg.instrument_key}"
                        )
                        leg.qty = 0
                        leg.fill_status = "CLOSED_EXTERNALLY"
                    elif broker_qty != 0 and abs(broker_qty) != local_qty:
                        logger.warning(
                            f"Qty mismatch: local={local_qty} "
                            f"broker={abs(broker_qty)} — "
                            f"broker wins"
                        )
                        leg.qty = (
                            abs(broker_qty) // config.LOT_SIZE
                        )

            logger.info("Broker reconciliation complete")

        except Exception as e:
            logger.error(f"Reconciliation error: {e}")

    def _create_position_record(
        self,
        strategy_name: str,
        legs: List[Leg],
        meta: Dict,
        trade_id: str = "",
    ) -> Position:
        """Create a Position dataclass from strategy legs and meta."""
        if not trade_id:
            trade_id = str(uuid.uuid4())
        IST = pytz.timezone(config.TZ)
        now = datetime.now(IST).isoformat()

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
        dte = 0
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
            entry_vix=self.dm.vix or 0.0,
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
            trend_direction=meta.get("trend_direction", 0.0),
            meta=meta,
        )

    def _position_to_dict(self, position: Position) -> Dict:
        """Convert Position to dictionary for SQLite/CSV storage."""
        IST = pytz.timezone(config.TZ)

        holding_days = 0
        if position.exit_timestamp and position.entry_timestamp:
            try:
                entry_dt = datetime.fromisoformat(
                    position.entry_timestamp
                )
                exit_dt = datetime.fromisoformat(
                    position.exit_timestamp
                )
                holding_days = (exit_dt - entry_dt).days
            except (ValueError, TypeError):
                holding_days = 0

        slippage_total = sum(
            l.slippage_pts for l in position.legs
        )

        # HIGH FIX 4: Include transaction costs in dict
        tx_costs = self._calculate_transaction_costs(position)
        gross_pnl = position.realized_pnl
        net_pnl = gross_pnl - tx_costs

        return {
            "trade_id":                   position.trade_id,
            "strategy_name":              position.strategy_name,
            "regime_at_entry":            position.regime_at_entry,
            "regime_at_exit":             self.re.confirmed_regime,
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
        }

    def _get_short_strike(
        self, position: Position, option_type: str
    ) -> Optional[float]:
        """Get strike of short leg for given option type."""
        for leg in position.legs:
            if (
                leg.action == "SELL"
                and leg.option_type == option_type
            ):
                return leg.strike
        return None

    def _get_upper_wing_strike(
        self, position: Position
    ) -> Optional[float]:
        """Get highest BUY put strike."""
        put_strikes = [
            l.strike for l in position.legs
            if l.option_type == "put" and l.action == "BUY"
        ]
        return max(put_strikes) if put_strikes else None

    def _get_lower_wing_strike(
        self, position: Position
    ) -> Optional[float]:
        """Get lowest BUY put strike."""
        put_strikes = [
            l.strike for l in position.legs
            if l.option_type == "put" and l.action == "BUY"
        ]
        return min(put_strikes) if put_strikes else None

    def _has_short_positions(self) -> bool:
        """Check if any open position has short legs."""
        return any(
            leg.action == "SELL"
            for pos in self.open_positions
            for leg in pos.legs
        )

    def _gamma_above_50pct_limit(self) -> bool:
        """Check if portfolio gamma exceeds 50% of regime minimum."""
        regime = self.re.confirmed_regime
        limits = config.GREEKS_LIMITS.get(regime, {})
        gamma_min = limits.get("gamma_min", -99)
        if gamma_min is None:
            return False
        greeks = self._get_portfolio_greeks()
        threshold = gamma_min * 0.50
        return greeks["gamma"] < threshold

    def _get_25d_put_iv(self) -> float:
        """Get implied volatility of 25-delta put."""
        strike = self.dm.get_strike_by_delta("put", 0.25)
        if strike is None:
            return 0.0
        return float(
            self.dm.option_chain.get(strike, {})
            .get("put", {}).get("iv", 0.0)
        )

    def _get_25d_call_iv(self) -> float:
        """Get implied volatility of 25-delta call."""
        strike = self.dm.get_strike_by_delta("call", 0.25)
        if strike is None:
            return 0.0
        return float(
            self.dm.option_chain.get(strike, {})
            .get("call", {}).get("iv", 0.0)
        )

    def _get_otm_bid_ask(self, option_type: str) -> float:
        """Get bid-ask spread for event strangle delta strike."""
        strike = self.dm.get_strike_by_delta(
            option_type, config.EVENT_STRANGLE_DELTA
        )
        if strike is None:
            return 99.0
        opt = (
            self.dm.option_chain.get(strike, {})
            .get(option_type, {})
        )
        return float(opt.get("ask", 99) - opt.get("bid", 0))

    def _get_ema_200(self) -> float:
        """Compute EMA(200) from candle closes."""
        if len(self.dm.candles_15m) < 200:
            return self.dm.spot or 0.0
        closes = [c["close"] for c in self.dm.candles_15m]
        ema = pd.Series(closes).ewm(
            span=200, adjust=False
        ).mean()
        return float(ema.iloc[-1])

    def _compute_ema_n(self, period: int) -> float:
        """Compute EMA of given period from candle closes."""
        if len(self.dm.candles_15m) < period:
            return self.dm.spot or 0.0
        closes = [c["close"] for c in self.dm.candles_15m]
        ema = pd.Series(closes).ewm(
            span=period, adjust=False
        ).mean()
        return float(ema.iloc[-1])

    def _get_dte_for_target(
        self, min_dte: int, max_dte: int
    ) -> Optional[int]:
        """Get DTE for target expiry range."""
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
        """Persist all open positions to SQLite."""
        for position in self.open_positions:
            self.dm.save_position(
                self._position_to_dict(position)
            )

    def _log_portfolio_summary(self) -> None:
        """Log formatted portfolio summary."""
        greeks = self._get_portfolio_greeks()
        logger.info(
            f"\n{'=' * 60}\n"
            f"PORTFOLIO SUMMARY\n"
            f"Open Positions : {len(self.open_positions)}\n"
            f"Daily P&L (net): ₹{self.daily_pnl:,.2f}\n"
            f"Weekly P&L (net): ₹{self.weekly_pnl:,.2f}\n"
            f"Capital        : ₹{self.current_capital:,.2f}\n"
            f"Peak Capital   : ₹{self.peak_capital:,.2f}\n"
            f"Delta          : {greeks['delta']:.3f}\n"
            f"Gamma          : {greeks['gamma']:.5f}\n"
            f"Vega           : ₹{greeks['vega']:,.0f}\n"
            f"Theta          : ₹{greeks['theta']:,.0f}/day\n"
            f"CB L2 Active   : {self.cb_level_2_active}\n"
            f"Kill Switch    : {self.kill_switch_active}\n"
            f"{'=' * 60}"
        )

    def _log_circuit_breaker(
        self, level: int, trigger: str, action: str
    ) -> None:
        """Log circuit breaker event to SQLite."""
        try:
            IST = pytz.timezone(config.TZ)
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO circuit_breaker_log (
                    timestamp, level, trigger,
                    action, daily_pnl, drawdown, regime
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(IST).isoformat(),
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
                f"_log_circuit_breaker SQLite error: {e}"
            )

    def _load_positions_from_sqlite(self) -> None:
        """Restore open positions from SQLite on startup."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM open_positions "
                "WHERE status = 'OPEN'"
            )
            rows = cursor.fetchall()
            col_names = [
                desc[0] for desc in cursor.description
            ]
            conn.close()

            if not rows:
                logger.info(
                    "No open positions to restore from SQLite"
                )
                return

            for row in rows:
                row_dict = dict(zip(col_names, row))
                legs_json = row_dict.get("legs_json", "[]")
                try:
                    legs_data = json.loads(legs_json)
                except Exception:
                    legs_data = []

                legs = []
                for l in legs_data:
                    leg = Leg(
                        instrument_key=l.get(
                            "instrument_key", ""
                        ),
                        option_type=l.get(
                            "option_type", "call"
                        ),
                        action=l.get("side", "BUY"),
                        strike=float(l.get("strike", 0)),
                        expiry=row_dict.get("expiry_date", ""),
                        qty=int(l.get("qty", 1)),
                        entry_price=float(
                            l.get("entry_price", 0)
                        ),
                        exit_price=float(
                            l.get("exit_price", 0)
                        ),
                        order_tag=l.get("order_tag", ""),
                    )
                    legs.append(leg)

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
                        row_dict.get("composite_at_entry", 0)
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
                )
                self.open_positions.append(position)
                logger.info(
                    f"Restored: {position.strategy_name} "
                    f"id={position.trade_id[:8]}"
                )

            logger.info(
                f"Restored {len(rows)} positions from SQLite"
            )

        except sqlite3.OperationalError:
            logger.info("No state.db found — fresh start")
        except Exception as e:
            logger.warning(
                f"_load_positions_from_sqlite error: {e}"
            )

    def reset_daily_state(self) -> None:
        """Reset daily P&L and circuit breaker state."""
        self.daily_pnl = 0.0
        self.daily_trading_halted = False
        self.cb_level_2_active = False
        self.cb_level_1_count = 0
        logger.info("Daily state reset complete")

    def reset_weekly_state(self) -> None:
        """Reset weekly P&L and circuit breaker state."""
        self.weekly_pnl = 0.0
        self.cb_level_3_active = False
        logger.info("Weekly state reset complete")