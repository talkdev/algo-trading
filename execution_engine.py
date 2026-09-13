# execution_engine.py
# NIFTY Intraday Options Engine v3.0
# Order execution, position monitoring, exit logic (7-priority system),
# transaction cost computation, profit lock, paper and live trading.
# Complete rewrite — clean exit priority order, no regime-based exits,
# ABORT only blocks new entries (never closes positions).

from __future__ import annotations

import json
import time as time_module
import uuid
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional, List, Tuple, Dict

from core import (
    Config, Database,
    ExpiryCalendar, now_ist, today_ist,
    print_section, print_kv_table,
    load_config, setup_logging,
    RateLimiter, UpstoxClient,
    UpstoxAPIError, AlertNotifier,
    # v7: the entry credit the FILLS booked, as against the one the strategy
    # engine planned. Used by execute_close() to settle a trade on its own
    # prices; the same function feeds the per-trade console report.
    realised_entry_credit,
    TradeConsoleReporter,
)
from data_engine import MarketDataEngine
from calibration_engine import CalibrationEngine



class OrderNotPlaced(RuntimeError):
    """The broker confirmed that the request never became an order.

    Distinct from a plain failure because it is safe to try again with a new
    tag: nothing is resting at the exchange. Anything else - a timeout whose
    fate is unknown - must never be re-sent.
    """

# ─────────────────────────────────────────────────────────────────────────────
# EXIT PRIORITY CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

EXIT_PRIORITY_DELTA_BREACH   = 1   # Short leg delta > 0.40
EXIT_PRIORITY_SPOT_PROXIMITY = 2   # Spot within 40pts of short strike
EXIT_PRIORITY_PRICE_STOP     = 3   # Price stop = 0.30 × opening straddle
EXIT_PRIORITY_PROFIT_LOCK    = 4   # Profit lock at 40% (DTE0) or 25% (DTE1+)
EXIT_PRIORITY_CHEAP_BUYBACK  = 5   # Short leg ≤ 2pts after 13:00
EXIT_PRIORITY_TIME_TARGET    = 6   # Time-based profit target
EXIT_PRIORITY_HARD_EXIT      = 7   # Hard exit time (15:00)

EXIT_PRIORITY_NAMES = {
    EXIT_PRIORITY_DELTA_BREACH:   "DELTA_BREACH",
    EXIT_PRIORITY_SPOT_PROXIMITY: "SPOT_PROXIMITY",
    EXIT_PRIORITY_PRICE_STOP:     "PRICE_STOP",
    EXIT_PRIORITY_PROFIT_LOCK:    "PROFIT_LOCK",
    EXIT_PRIORITY_CHEAP_BUYBACK:  "CHEAP_BUYBACK",
    EXIT_PRIORITY_TIME_TARGET:    "TIME_TARGET",
    EXIT_PRIORITY_HARD_EXIT:      "HARD_EXIT",
}

# Exit reason strings stored in database
EXIT_REASON_MAP = {
    EXIT_PRIORITY_DELTA_BREACH:   "CLOSE_STOP",
    EXIT_PRIORITY_SPOT_PROXIMITY: "CLOSE_STOP",
    EXIT_PRIORITY_PRICE_STOP:     "CLOSE_STOP",
    EXIT_PRIORITY_PROFIT_LOCK:    "CLOSE_TARGET",
    EXIT_PRIORITY_CHEAP_BUYBACK:  "CLOSE_TARGET",
    EXIT_PRIORITY_TIME_TARGET:    "CLOSE_TARGET",
    EXIT_PRIORITY_HARD_EXIT:      "HARD_EXIT_15:00",
}


# ─────────────────────────────────────────────────────────────────────────────
# PAPER ORDER EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

class PaperOrderExecutor:
    """
    Simulates order execution for paper trading.
    Uses mid-price for fills (no real slippage simulation — slippage
    is modelled separately in transaction cost computation).
    """

    def __init__(self, config: Config, logger):
        self.config  = config
        self.logger  = logger
        self._counter = 0

    def _next_order_id(self) -> str:
        self._counter += 1
        return f"PAPER-{today_ist().isoformat()}-{self._counter:04d}"

    def execute_leg_entry(
        self, leg: dict, lots: int, chain: dict
    ) -> dict:
        """Simulate entry fill. Uses exec_price from strategy params."""
        order_id   = self._next_order_id()
        fill_price = float(leg.get("exec_price", 0) or 0)

        self.logger.info(
            f"[PAPER] ENTRY: {leg['action']} {leg['option_type'].upper()} "
            f"{leg['strike']:.0f} ×{lots} @ {fill_price:.2f} "
            f"(order={order_id})"
        )
        return {
            "order_id":   order_id,
            "fill_price": fill_price,
            "status":     "FILLED",
        }

    def execute_leg_exit(
        self, leg: dict, chain: dict, lots: int
    ) -> dict:
        """
        Simulate exit fill.
        For SELL legs (buying back): use ask price.
        For BUY legs (selling): use bid price.
        Falls back to entry price if chain unavailable.
        """
        order_id   = self._next_order_id()
        strike     = float(leg.get("strike", 0))
        opt_type   = str(leg.get("option_type", ""))
        action     = str(leg.get("action", "SELL"))
        entry_price = float(leg.get("entry_price", 0) or 0)

        opt = chain.get(strike, {}).get(opt_type, {}) if chain else {}
        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        ltp = float(opt.get("ltp", 0) or 0)

        # Exit is the reverse of entry action
        if action == "SELL":
            # Buying back: pay ask
            fill_price = ask if ask > 0 else (ltp if ltp > 0 else entry_price)
        else:
            # Selling: receive bid
            fill_price = bid if bid > 0 else (ltp if ltp > 0 else entry_price)

        self.logger.info(
            f"[PAPER] EXIT: close {action} {opt_type.upper()} "
            f"{strike:.0f} ×{lots} @ {fill_price:.2f} "
            f"(order={order_id})"
        )
        return {
            "order_id":   order_id,
            "fill_price": fill_price,
            "status":     "FILLED",
        }


# ─────────────────────────────────────────────────────────────────────────────
# LIVE ORDER EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

class LiveOrderExecutor:
    """
    Places real orders via Upstox API.
    Uses aggressive limit pricing to ensure fills.
    Fetches actual fill price after order placement.
    """

    def __init__(self, config: Config, client: UpstoxClient, logger,
                 db=None, notifier=None):
        if config.paper_trade_mode:
            raise RuntimeError(
                "LiveOrderExecutor instantiated while PAPER_TRADE_MODE=True. Refusing."
            )
        self.config   = config
        self.client   = client
        self.logger   = logger
        self.db       = db
        self.notifier = notifier

    def _resolve_instrument_key(self, leg: dict, chain: dict) -> Optional[str]:
        """Get instrument key from leg or chain."""
        if leg.get("instrument_key"):
            return leg["instrument_key"]
        strike   = float(leg.get("strike", 0))
        opt_type = str(leg.get("option_type", ""))
        opt = chain.get(strike, {}).get(opt_type, {}) if chain else {}
        return opt.get("instrument_key")

    def _aggressive_limit_price(
        self,
        chain:            dict,
        strike:           float,
        opt_type:         str,
        transaction_type: str,
        fallback:         float,
    ) -> float:
        """
        Compute aggressive limit price to ensure fill.
        BUY: ask + 2 ticks (willing to pay slightly more)
        SELL: bid - 2 ticks (willing to accept slightly less)
        """
        opt = chain.get(strike, {}).get(opt_type, {}) if chain else {}
        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        tick = 0.10

        if transaction_type == "BUY":
            price = (
                ask + 2 * tick if ask > 0
                else bid + 4 * tick if bid > 0
                else fallback + 2 * tick
            )
        else:
            price = (
                max(bid - 2 * tick, tick) if bid > 0
                else max(ask - 4 * tick, tick) if ask > 0
                else max(fallback - 2 * tick, tick)
            )

        return round(round(price / tick) * tick, 2)

    # Broker statuses meaning "this order is done and fully executed".
    _TERMINAL_OK = {"complete", "completed", "filled", "traded", "executed"}
    _TERMINAL_BAD = {"cancelled", "canceled", "rejected", "expired", "lapsed"}

    def _get_fill_price(
        self, order_id: str, fallback: float, retries: int = 8
    ) -> float:
        """
        Fetch the ACTUAL fill price and verify the order really completed.

        v3.1: this previously polled three times and, on failure, returned the
        price the strategy *expected*, while execute_leg_entry/exit
        unconditionally reported status "FILLED". An unfilled or partially
        filled leg was therefore written into position_legs as a clean fill.
        From that moment the engine's book no longer matched the broker's, so
        every downstream number — position premium, unrealised P&L, the daily
        loss halt, every exit decision — was computed on a fiction. On a
        four-legged structure this is precisely how a "defined risk" position
        quietly becomes a naked one, and nothing in the logs would say so.

        A non-completed order now raises, which routes into the existing
        _emergency_unwind path instead of silently corrupting the book.
        """
        last_status = ""
        for attempt in range(retries):
            try:
                details = self.client.get_order_details(order_id) or {}
                status  = str(
                    details.get("status") or details.get("order_status") or ""
                ).strip().lower()
                last_status = status or last_status

                filled  = details.get("filled_quantity")
                pending = details.get("pending_quantity")
                price   = details.get("average_price") or details.get("price")

                if status in self._TERMINAL_BAD:
                    raise RuntimeError(
                        f"order {order_id} terminated as '{status}' — not filled"
                    )

                if status in self._TERMINAL_OK:
                    try:
                        if pending is not None and float(pending) > 0:
                            raise RuntimeError(
                                f"order {order_id} reports '{status}' but "
                                f"pending_quantity={pending}"
                            )
                    except (TypeError, ValueError):
                        pass
                    if price and float(price) > 0:
                        return float(price)
                    raise RuntimeError(
                        f"order {order_id} complete but broker returned no "
                        f"average_price"
                    )

                # Not terminal yet: accept only if the broker explicitly says
                # everything is filled and nothing is pending.
                if filled is not None and pending is not None and price:
                    try:
                        if (float(filled) > 0 and float(pending) == 0
                                and float(price) > 0):
                            return float(price)
                    except (TypeError, ValueError):
                        pass

            except RuntimeError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Could not fetch order details for {order_id} "
                    f"(attempt {attempt + 1}/{retries}): {e}"
                )
            time_module.sleep(1)

        raise RuntimeError(
            f"order {order_id} did not reach a confirmed filled state "
            f"(last status='{last_status or 'unknown'}'); refusing to book a "
            f"phantom fill at {fallback:.2f}"
        )

    # ── v6: dispatch ledger, reconcile-on-lost-response, exit escalation ───

    def _alert(self, level: str, text: str) -> None:
        notifier = getattr(self, "notifier", None)
        try:
            if notifier is not None:
                notifier.send(text, level)
        except Exception:
            pass

    def _new_tag(self, leg: dict, transaction_type: str, phase: str) -> str:
        """Unique, pre-assigned order tag: the only client-side handle that
        survives a lost response, and short enough for UDAPI1119 (40 chars)."""
        prefix = str(getattr(self.config, "order_tag_prefix", "nav6") or "nav6")[:12]
        try:
            strike = int(round(float(leg.get("strike", 0) or 0)))
        except (TypeError, ValueError):
            strike = 0
        try:
            idx = int(leg.get("leg_idx", leg.get("leg_id", 0)) or 0) % 99
        except (TypeError, ValueError):
            idx = 0
        side = "B" if transaction_type == "BUY" else "S"
        ph   = "E" if str(phase).upper().startswith("ENT") else "X"
        return f"{prefix}-{ph}{idx}-{side}{strike}-{uuid.uuid4().hex[:8]}"[:40]

    def _dispatch_write(self, tag, leg, transaction_type, qty, price, phase,
                        order_id=None, state="DISPATCHED", error=None):
        db = getattr(self, "db", None)
        if db is None or not tag:
            return
        try:
            existing = db.query_one("SELECT id FROM order_dispatch WHERE tag=?", (tag,))
            fields = {
                "position_id":      leg.get("position_id"),
                "leg_idx":          leg.get("leg_idx", leg.get("leg_id")),
                "phase":            phase,
                "action":           leg.get("action"),
                "transaction_type": transaction_type,
                "instrument_token": leg.get("instrument_key"),
                "quantity":         qty,
                "limit_price":      price,
                "state":            state,
                "updated_at":       now_ist().isoformat(),
            }
            if order_id is not None:
                fields["order_id"] = order_id
            if error is not None:
                fields["error"] = str(error)[:400]
            if existing:
                db.update("order_dispatch", fields, {"tag": tag})
                db.execute(
                    "UPDATE order_dispatch SET attempts = attempts + 1 WHERE tag=?",
                    (tag,),
                )
            else:
                fields["tag"] = tag
                fields["created_at"] = now_ist().isoformat()
                db.insert("order_dispatch", fields)
        except Exception as e:
            self.logger.warning(f"order dispatch ledger write failed ({tag}): {e}")

    def _reconcile_by_tag(self, tag: str, leg: dict):
        """Answer 'did that order reach the exchange?' from the broker's side.

        Returns a dict describing the found order, or None when nothing
        carries the tag - in which case the request genuinely did not land.
        Raises only when the broker says the order is dead.
        """
        try:
            rows = self.client.get_order_history_by_tag(tag)
        except Exception as e:
            self.logger.warning(
                f"cannot reconcile order tag {tag}: {e} — treating as unresolved"
            )
            return {"state": "UNKNOWN", "order_id": None, "tag": tag}
        if not rows:
            return None
        by_id: dict = {}
        for row in rows:
            oid = str(row.get("order_id") or "")
            if oid:
                by_id[oid] = row          # history is chronological: last wins
        if not by_id:
            return None
        if len(by_id) > 1:
            self._alert(
                "CRITICAL",
                f"{len(by_id)} orders share tag {tag} on "
                f"{leg.get('strike')}{str(leg.get('option_type', '')).upper()}; "
                f"cancelling every extra before anything is booked",
            )
            newest = max(by_id)
            for oid, row in by_id.items():
                if oid == newest:
                    continue
                state = str(row.get("status") or "").strip().lower()
                if state in self._TERMINAL_OK or state in self._TERMINAL_BAD:
                    continue
                try:
                    self.client.cancel_order(oid)
                except Exception as e:
                    self.logger.critical(
                        f"orphan order {oid} (tag {tag}) could not be cancelled: {e}"
                    )
        order_id, row = max(by_id.items(), key=lambda kv: str(kv[1].get("order_timestamp") or ""))
        status = str(row.get("status") or "").strip().lower()
        price  = float(row.get("average_price") or 0.0)
        filled = float(row.get("filled_quantity") or 0.0)
        self._dispatch_write(tag, leg, str(row.get("transaction_type") or ""),
                             int(row.get("quantity") or 0), price,
                             "RECONCILED", order_id=order_id, state="PLACED")
        if status in self._TERMINAL_OK and price > 0:
            return {"state": "FILLED", "order_id": order_id, "tag": tag,
                    "fill_price": price, "reconciled": True}
        if status in self._TERMINAL_BAD:
            raise RuntimeError(
                f"order {order_id} (tag {tag}) terminated as '{status}' after an "
                f"unconfirmed request"
            )
        return {"state": "OPEN", "order_id": order_id, "tag": tag, "status": status}

    def _place_order_reconciled(
        self, *, instrument_key: str, leg: dict, transaction_type: str,
        qty: int, limit_price: float, phase: str,
    ) -> dict:
        """Place one order exactly once, and resolve an unanswered response.

        The v2 place-order API has no client order id, so a request whose
        response is lost is neither failed nor successful. The session that
        carries order traffic therefore never re-sends; instead the tag that
        went with the request is looked up in /order/history. That turns a
        duplicate-position risk into a query.
        """
        tag = self._new_tag(leg, transaction_type, phase)
        self._dispatch_write(tag, leg, transaction_type, qty, limit_price, phase)
        settled = False
        try:
            try:
                result = self.client.place_order(
                    instrument_token=instrument_key,
                    quantity=qty,
                    transaction_type=transaction_type,
                    order_type="LIMIT",
                    price=limit_price,
                    product="I",
                    tag=tag,
                )
            except UpstoxAPIError as e:
                if getattr(e, "maybe_delivered", False) and bool(
                    getattr(self.config, "reconcile_after_timeout", True)
                ):
                    found = self._reconcile_by_tag(tag, leg)
                    if found is not None:
                        settled = True
                        return found
                    settled = True
                    self._dispatch_write(tag, leg, transaction_type, qty,
                                        limit_price, phase, state="NOT_PLACED",
                                        error=e)
                    raise OrderNotPlaced(
                        f"{e} — reconciled by tag {tag}: no order at broker, "
                        f"nothing was placed"
                    ) from e
                settled = True
                self._dispatch_write(tag, leg, transaction_type, qty,
                                    limit_price, phase, state="FAILED", error=e)
                raise
            settled = True
            order_id = str((result or {}).get("order_id") or "")
            self._dispatch_write(tag, leg, transaction_type, qty, limit_price,
                                phase, order_id=order_id, state="PLACED")
            placed = (result or {}).get("state")
            out = {"order_id": order_id, "tag": tag}
            if isinstance(placed, str):
                out["state"] = placed
            return out
        except BaseException as e:
            # Ctrl-C or a hard failure in the gap between sending the request
            # and recording the answer is the one way the engine can lose a
            # live order completely. The row stays UNRESOLVED so the next
            # start looks it up by tag instead of guessing.
            if not settled:
                self._dispatch_write(tag, leg, transaction_type, qty,
                                    limit_price, phase, state="UNRESOLVED",
                                    error=f"{type(e).__name__}: {e}")
            raise

    def _await_fill(
        self, order_id: str, fallback: float, wait_sec: float
    ) -> dict:
        """Short, non-raising version of _get_fill_price for the exit ladder."""
        deadline = time_module.monotonic() + max(1.0, float(wait_sec))
        last_status = ""
        while True:
            try:
                details = self.client.get_order_details(order_id) or {}
                status  = str(
                    details.get("status") or details.get("order_status") or ""
                ).strip().lower()
                last_status = status or last_status
                price  = float(details.get("average_price") or 0.0)
                filled = float(details.get("filled_quantity") or 0.0)
                pending = details.get("pending_quantity")
                if status in self._TERMINAL_OK and price > 0:
                    if pending is None or float(pending or 0) == 0:
                        return {"state": "FILLED", "price": price, "status": status}
                if status in self._TERMINAL_BAD:
                    return {"state": "BAD", "price": 0.0, "status": status}
                if filled > 0 and price > 0 and (pending is None or float(pending or 0) == 0):
                    return {"state": "FILLED", "price": price, "status": status}
            except Exception as e:
                self.logger.debug(f"fill poll failed for {order_id}: {e}")
            if time_module.monotonic() >= deadline:
                return {"state": "OPEN", "price": 0.0,
                        "status": last_status or "unknown"}
            time_module.sleep(0.5)

    def _cancel_and_confirm(self, order_id: str, leg: dict) -> dict:
        """Cancel a resting exit order and prove what happened.

        Returns {"state": "CANCELLED"}, {"state": "FILLED", "price": p} when the
        order filled in the gap, or {"state": "UNKNOWN"}. Only the first may be
        followed by a re-quote: escalating on top of a live order would double
        the exit, and treating an unresolved cancel as a failure while the
        exchange filled the leg would leave a closed leg recorded as open.
        """
        try:
            self.client.cancel_order(order_id)
        except UpstoxAPIError as e:
            status = str((getattr(e, "response_body", "") or ""))[:160]
            details = {}
            try:
                details = self.client.get_order_details(order_id) or {}
            except Exception:
                pass
            st = str(details.get("status") or "").strip().lower()
            price = float(details.get("average_price") or 0.0)
            if st in self._TERMINAL_OK and price > 0:
                return {"state": "FILLED", "price": price}
            if st == "cancelled" or "UDAPI1109" in status:
                return {"state": "CANCELLED"}
            return {"state": "UNKNOWN", "status": st or status or "unknown"}
        for _ in range(4):
            try:
                details = self.client.get_order_details(order_id) or {}
            except Exception:
                details = {}
            st    = str(details.get("status") or "").strip().lower()
            price = float(details.get("average_price") or 0.0)
            if st == "cancelled":
                return {"state": "CANCELLED"}
            if st in self._TERMINAL_OK and price > 0:
                return {"state": "FILLED", "price": price}
            time_module.sleep(0.5)
        return {"state": "UNKNOWN", "status": "cancel not confirmed"}

    def _escalate_exit_price(
        self, chain: dict, leg: dict, transaction_type: str, fallback: float,
        attempt: int,
    ) -> float:
        """Re-quote a stuck exit through the market-protection price.

        MARKET orders are not processed from the API, so the only way to force
        an exit is a LIMIT far enough through the book to cross it, priced
        against LTP rather than against a bid that stopped answering.
        """
        strike   = float(leg.get("strike", 0))
        opt_type = str(leg.get("option_type", ""))
        opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
        ltp = float(opt.get("ltp", 0) or 0) or float(opt.get("bid", 0) or 0) \
            or float(opt.get("ask", 0) or 0) or fallback
        pct = float(getattr(self.config, "market_protection_pct", 2.0))
        pct = pct * (1.0 + 0.5 * max(0, attempt - 1))
        price = self.client.synthetic_market_price(ltp, transaction_type, pct)
        return price if price > 0 else self._aggressive_limit_price(
            chain, strike, opt_type, transaction_type, fallback
        )

    def execute_leg_entry(
        self, leg: dict, lots: int, chain: dict
    ) -> dict:
        """Place a live entry order."""
        instrument_key = self._resolve_instrument_key(leg, chain)
        if not instrument_key:
            raise RuntimeError(
                f"No instrument_key for leg {leg['strike']} {leg['option_type']}"
            )

        qty              = lots * self.config.lot_size
        transaction_type = "SELL" if leg["action"] == "SELL" else "BUY"
        limit_price      = self._aggressive_limit_price(
            chain, leg["strike"], leg["option_type"],
            transaction_type, float(leg.get("exec_price", 0) or 0)
        )

        placed = self._place_order_reconciled(
            instrument_key=instrument_key, leg=leg,
            transaction_type=transaction_type, qty=qty,
            limit_price=limit_price, phase="ENTRY",
        )
        order_id = placed.get("order_id", "")

        self.logger.info(
            f"[LIVE] ENTRY ORDER: {transaction_type} {leg['option_type'].upper()} "
            f"{leg['strike']:.0f} ×{lots} @ limit={limit_price:.2f} "
            f"order_id={order_id} tag={placed.get('tag')}"
            + (" (reconciled after lost response)" if placed.get("reconciled") else "")
        )

        fill_price = float(placed.get("fill_price") or 0.0)
        if fill_price <= 0:
            fill_price = self._get_fill_price(
                order_id, fallback=float(leg.get("exec_price", 0) or 0)
            )
        return {
            "order_id":   order_id,
            "fill_price": fill_price,
            "status":     "FILLED",
        }

    def execute_leg_exit(
        self, leg: dict, chain: dict, lots: int
    ) -> dict:
        """Place a live exit order, escalating if the book stops answering.

        A resting exit order is never simply abandoned: the ladder cancels it,
        confirms the cancellation, and only then re-quotes deeper. Two live
        orders on one leg would exit twice, which at this size is worse than
        exiting a cycle late.
        """
        instrument_key = self._resolve_instrument_key(leg, chain)
        if not instrument_key:
            raise RuntimeError(
                f"No instrument_key for leg {leg['strike']} {leg['option_type']}"
            )

        qty              = lots * self.config.lot_size
        # Exit is reverse of entry action
        transaction_type = "BUY" if leg["action"] == "SELL" else "SELL"
        fallback         = float(leg.get("entry_price", 0) or 0)
        limit_price      = self._aggressive_limit_price(
            chain, float(leg.get("strike", 0)), str(leg.get("option_type", "")),
            transaction_type, fallback
        )

        try:
            escalate = int(getattr(self.config, "exit_escalation_attempts", 1))
        except (TypeError, ValueError):
            escalate = 1
        attempts   = 1 + max(0, min(escalate, 3))
        try:
            wait_sec = float(getattr(self.config, "exit_escalate_after_sec", 6.0))
        except (TypeError, ValueError):
            wait_sec = 6.0
        if not bool(getattr(self.config, "exit_escalation_enabled", True)):
            attempts = 1
        describe = (
            f"{transaction_type} {str(leg.get('option_type', '')).upper()} "
            f"{float(leg.get('strike', 0) or 0):.0f} ×{lots}"
        )

        last_state = ""
        for attempt in range(attempts):
            try:
                placed = self._place_order_reconciled(
                    instrument_key=instrument_key, leg=leg,
                    transaction_type=transaction_type, qty=qty,
                    limit_price=limit_price, phase="EXIT",
                )
            except (UpstoxAPIError, OrderNotPlaced) as e:
                # A rejected price or an order the broker confirms does not
                # exist can both be re-tried safely at a deeper price; an
                # unknown-fate error cannot, so it is left to the caller.
                fatal = isinstance(e, UpstoxAPIError) and e.status_code != 400
                if fatal or attempt + 1 >= attempts:
                    raise
                limit_price = self._escalate_exit_price(
                    chain, leg, transaction_type, fallback, attempt + 2
                )
                self.logger.warning(
                    f"[LIVE] EXIT ESCALATION: {describe} rejected "
                    f"({str(e)[:90]}); re-quoting {limit_price:.2f}"
                )
                self._alert(
                    "WARNING",
                    f"exit {describe} rejected ({str(e)[:90]}); re-quoting "
                    f"{limit_price:.2f}",
                )
                continue
            order_id = placed.get("order_id", "")
            self.logger.info(
                f"[LIVE] EXIT ORDER: {describe} @ limit={limit_price:.2f} "
                f"order_id={order_id} tag={placed.get('tag')}"
                + (" (reconciled after lost response)" if placed.get("reconciled") else "")
            )

            fill_price = float(placed.get("fill_price") or 0.0)
            if fill_price <= 0:
                outcome    = self._await_fill(order_id, fallback, wait_sec)
                fill_price = float(outcome.get("price") or 0.0)
                last_state = str(outcome.get("status") or outcome.get("state") or "")
                if outcome.get("state") == "FILLED":
                    fill_price = float(outcome.get("price") or 0.0)

            if fill_price > 0:
                return {
                    "order_id":   order_id,
                    "fill_price": fill_price,
                    "status":     "FILLED",
                }

            # Nothing may be left resting at the broker: an order that fills
            # after this method returns would make the next cycle's exit a
            # double exit. So every unfilled attempt is cancelled and the
            # cancellation confirmed, including the last one.
            final_try = attempt + 1 >= attempts
            if str(placed.get("state") or "") == "BAD":
                confirmed = {"state": "CANCELLED"}
            else:
                confirmed = self._cancel_and_confirm(order_id, leg)
            if confirmed.get("state") == "FILLED":
                price = float(confirmed.get("price") or 0.0)
                if price > 0:
                    self.logger.warning(
                        f"exit order {order_id} filled while being cancelled at "
                        f"{price:.2f} — booking that fill rather than re-quoting"
                    )
                    return {
                        "order_id":   order_id,
                        "fill_price": price,
                        "status":     "FILLED",
                    }
            if confirmed.get("state") != "CANCELLED":
                raise RuntimeError(
                    f"exit order {order_id} for {describe} is unaccounted for "
                    f"(status={last_state or 'unknown'}, "
                    f"cancel={confirmed.get('state')}); leg left OPEN so no "
                    f"second exit is sent - resolve at the broker before the "
                    f"next cycle"
                )
            if final_try:
                raise RuntimeError(
                    f"exit for {describe} not filled after {attempts} attempt(s) "
                    f"and cancelled (last status={last_state or 'unknown'}); "
                    f"leg left OPEN for the next cycle"
                )


            attempt_no   = attempt + 2
            new_price    = self._escalate_exit_price(
                chain, leg, transaction_type, fallback, attempt_no
            )
            self._alert(
                "WARNING",
                f"exit {describe} unfilled after {wait_sec:.0f}s "
                f"(status={last_state or 'unknown'}); cancelled and re-quoting "
                f"{new_price:.2f} (was {limit_price:.2f})",
            )
            self.logger.warning(
                f"[LIVE] EXIT ESCALATION: {describe} cancelled at "
                f"{limit_price:.2f}, re-quoting {new_price:.2f}"
            )
            limit_price = new_price

        raise RuntimeError(
            f"exit for {describe} not filled after {attempts} attempt(s) "
            f"(last status={last_state or 'unknown'})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Order execution and position lifecycle management.

    Responsibilities:
    1. Pre-trade validation (delta/vega limits, chain freshness, price drift)
    2. Entry execution (paper or live)
    3. Position monitoring with 7-priority exit system
    4. Exit execution with P&L computation
    5. Transaction cost computation (STT, exchange, SEBI, stamp, brokerage, GST)
    6. State management (daily P&L, capital, consecutive stops)
    7. Exit quality logging for calibration feedback

    Exit Priority System (NEVER close on regime label change):
    Priority 1: Short leg delta > 0.40 → CLOSE_STOP
    Priority 2: Spot within 40pts of short strike → CLOSE_STOP
    Priority 3: Price stop = 0.30 × opening straddle → CLOSE_STOP
    Priority 4: Profit lock at 40% (DTE0) or 25% (DTE1+) → move stop to breakeven
    Priority 5: Cheap buyback — short leg ≤ 2pts after 13:00 → CLOSE_TARGET
    Priority 6: Time-based target → CLOSE_TARGET
    Priority 7: Hard exit at 15:00 → HARD_EXIT_15:00

    NEVER close on:
    - Regime label change
    - VIX noise
    - Parkinson RV anomaly
    - ABORT signal (ABORT only blocks NEW entries)
    """

    def __init__(
        self,
        config:         Config,
        db:             Database,
        market_engine:  MarketDataEngine,
        cal_engine:     CalibrationEngine,
        client:         UpstoxClient,
        logger,
    ):
        self.config        = config
        self.db            = db
        self.market_engine = market_engine
        self.cal_engine    = cal_engine
        self.logger        = logger
        # Held here as well as in the executor: the safety paths on this class
        # (margin pre-flight, flatten verification, cancel-all) must be able to
        # ask the broker directly instead of trusting local bookkeeping.
        self.client        = client
        self.notifier      = (
            AlertNotifier(config) if not config.paper_trade_mode else None
        )

        self._ensure_extra_columns()

        if config.paper_trade_mode:
            self.executor = PaperOrderExecutor(config, logger)
            logger.info("ExecutionEngine: PAPER TRADE mode.")
        else:
            self.executor = LiveOrderExecutor(
                config, client, logger, db=db, notifier=self.notifier
            )
            logger.warning(
                "ExecutionEngine: LIVE TRADING mode — REAL ORDERS WILL BE PLACED."
            )

    # ─────────────────────────────────────────────────────────────────────
    # ALERTING
    # ─────────────────────────────────────────────────────────────────────

    def _alert(self, level: str, text: str) -> None:
        """Page a human when the engine can no longer help itself.

        Never allowed to raise: an alerting failure must not become a trading
        failure, and must not abort a flatten that is already in progress.
        """
        try:
            getattr(self.logger, {"CRITICAL": "critical",
                                 "WARNING": "warning"}.get(level, "info"))(text)
        except Exception:
            pass
        try:
            if self.notifier is not None:
                self.notifier.send(text, level)
        except Exception:
            pass


    # ─────────────────────────────────────────────────────────────────────
    # SCHEMA
    # ─────────────────────────────────────────────────────────────────────

    def _ensure_extra_columns(self) -> None:
        """Add any columns that may be missing from older database versions."""
        extra = [
            # v7: monitor_position() writes the liquidation mark on every
            # cycle and MainEngine.compute_unrealized_pnl() reads it, but the
            # column exists in neither SCHEMA_SQL nor MIGRATION_SQL - it was
            # only ever created by MarketDataEngine._ensure_extra_columns().
            # That made the exit ladder depend on another engine having been
            # constructed first: build an ExecutionEngine against a fresh book
            # on its own (a tool, a test, a refactor) and the first
            # monitor_position() raises sqlite3.OperationalError, which the
            # main loop catches as "UNHANDLED ERROR in run_one_cycle" - so
            # every cycle fails and no position is ever monitored or exited.
            # Ensure it here too; ensure_column is a no-op when it exists.
            ("positions", "last_liquidation_premium", "REAL"),
            # v7: the credit the FILLS booked, written at close next to the
            # planned credit the strategy engine priced the trade with, so
            # the two can always be compared after the fact.
            ("positions", "entry_credit_realised",   "REAL"),
            ("positions", "profit_lock_activated",   "INTEGER DEFAULT 0"),
            ("positions", "profit_lock_stop_level",  "REAL"),
            ("positions", "exit_priority",           "INTEGER"),
            ("positions", "price_stop_level_call",   "REAL"),
            ("positions", "price_stop_level_put",    "REAL"),
            ("positions", "is_borderline_sell",      "INTEGER DEFAULT 0"),
            ("positions", "opening_straddle_at_entry","REAL"),
            ("positions", "entry_vrp_smoothed",      "REAL"),
            ("positions", "vol_regime_at_entry",     "TEXT"),
            ("positions", "price_regime_at_entry",   "TEXT"),
            ("positions", "positioning_at_entry",    "TEXT"),
            ("positions", "confidence_score_at_entry","REAL"),
            ("trade_exits", "exit_priority",         "INTEGER"),
            ("trade_exits", "exit_priority_name",    "TEXT"),
            ("trade_exits", "pnl_15min_after_exit",  "REAL"),
            # v7: execute_close() has written these two into trade_exits since
            # it was first drafted, but neither SCHEMA_SQL nor MIGRATION_SQL
            # declared them, so the INSERT raised "no column named exit_adx"
            # on every single close and the row was dropped (caught and logged
            # as a warning). The exit audit table was therefore always empty -
            # the 2026-09-11 paper book holds 2 CLOSED positions and 0
            # trade_exits rows - and every query that joins trade_exits
            # silently returned nothing. Declared in core.py too; ensured here
            # so an existing book is repaired on the next engine start.
            ("trade_exits", "exit_adx",              "REAL"),
            ("trade_exits", "exit_vwap_dist",        "REAL"),
        ]
        for table, col, coltype in extra:
            self.db.ensure_column(table, col, coltype)

    # ─────────────────────────────────────────────────────────────────────
    # POSITION QUERIES
    # ─────────────────────────────────────────────────────────────────────

    def _get_open_positions(self) -> List[dict]:
        """Return all open positions for today."""
        return self.db.query(
            "SELECT * FROM positions WHERE trading_date=? AND status='OPEN'",
            (today_ist().isoformat(),),
        )

    def _get_position_legs(self, position_id: str) -> List[dict]:
        """Return all legs for a position."""
        return self.db.query(
            "SELECT * FROM position_legs WHERE position_id=?",
            (position_id,),
        )

    # ─────────────────────────────────────────────────────────────────────
    # TRANSACTION COST COMPUTATION
    # ─────────────────────────────────────────────────────────────────────

    def _compute_transaction_costs(
        self,
        legs:   List[dict],
        lots:   int,
        action: str,  # "ENTRY" or "EXIT"
    ) -> dict:
        """
        Compute all transaction costs for a set of legs.

        For ENTRY: STT on sell side, stamp on buy side
        For EXIT:  the sides are the MIRROR IMAGE - a leg that was sold at
                   entry is bought back, and a leg that was bought is sold -
                   and STT is levied on the SALE of an option, so on exit it
                   belongs to the legs that were originally BOUGHT.

        Returns dict with total_rupees and detailed breakdown.
        """
        C02        = self.config.lot_size
        sell_value = buy_value = 0.0
        num_orders = len(legs)
        # v7: `action` used to be accepted and ignored, and the classification
        # below read leg["action"] - the ENTRY side - for both phases. On an
        # exit that charges STT to the buy-backs (which pay no STT) and stamp
        # duty to the sales (which pay no stamp), while the turnover-based
        # charges stay right, so the error is invisible in the total's
        # magnitude and only shows up when the live book is reconciled
        # against the replay: StrategyEngine._compute_costs() and
        # BacktestRunner._close() both pass the CLOSING side, so the same
        # trade cost Rs 2.27 more live than in replay on a 4-lot bear call
        # spread (STT Rs 3.12 charged where Rs 0.78 was due).
        closing = str(action or "").strip().upper() == "EXIT"

        for leg in legs:
            # Use fill price for cost computation
            price = float(
                leg.get("fill_price") or
                leg.get("exit_price") or
                leg.get("entry_price") or
                leg.get("exec_price") or 0
            )
            if price <= 0:
                continue

            qty           = lots * C02
            premium_value = price * qty

            side = str(leg.get("action") or "").strip().upper()
            if closing:
                side = "BUY" if side.startswith("S") else "SELL"

            if side == "SELL":
                sell_value += premium_value
            else:
                buy_value += premium_value

        turnover = sell_value + buy_value
        if turnover <= 0:
            return {"total_rupees": 0.0, "breakdown": {}}

        # STT: on sell side only (options)
        stt = sell_value * self.config.stt_options_sell

        # Exchange transaction charge
        exchange = turnover * self.config.exchange_txn_rate

        # SEBI turnover fee
        sebi = turnover * self.config.sebi_rate

        # Stamp duty: on buy side only
        stamp = buy_value * self.config.stamp_duty_buy_options

        # Brokerage: fixed per order
        brokerage = self.config.brokerage_per_order * num_orders

        # GST: 18% on brokerage + exchange + SEBI
        gst = (brokerage + exchange + sebi) * 0.18

        total = stt + exchange + sebi + stamp + brokerage + gst

        return {
            "total_rupees": round(total, 2),
            "breakdown": {
                "stt":       round(stt, 2),
                "exchange":  round(exchange, 2),
                "sebi":      round(sebi, 4),
                "stamp":     round(stamp, 4),
                "brokerage": round(brokerage, 2),
                "gst":       round(gst, 2),
                "total":     round(total, 2),
            },
        }

    # ─────────────────────────────────────────────────────────────────────
    # MARK PRICE
    # ─────────────────────────────────────────────────────────────────────

    def _get_mark_price(self, leg: dict, chain: dict) -> float:
        """
        Get current mark price for a leg from the live chain.
        Uses mid-price if bid/ask available, falls back to LTP, then entry price.
        """
        if not chain:
            return float(leg.get("entry_price", 0) or 0)

        strike   = float(leg.get("strike", 0))
        opt_type = str(leg.get("option_type", ""))
        opt      = chain.get(strike, {}).get(opt_type, {})

        if not opt:
            return float(leg.get("entry_price", 0) or 0)

        bid = float(opt.get("bid", 0) or 0)
        ask = float(opt.get("ask", 0) or 0)
        ltp = float(opt.get("ltp", 0) or 0)

        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        if ltp > 0:
            return ltp
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        return float(leg.get("entry_price", 0) or 0)

    def _liquidation_premium(
        self, legs: List[dict], chain: dict
    ) -> float:
        """
        v3.1: the premium the position can ACTUALLY be closed at right now.

        _compute_current_premium marks at the mid. That is the correct number
        to report, but it is the wrong number to make a profit-taking decision
        on: closing a credit structure means BUYING BACK the shorts at the ask
        and SELLING the longs at the bid. Deciding targets on the mid meant
        the engine repeatedly declared a target reached, sent the exit, and
        filled worse — systematically converting the modelled edge into
        slippage, trade after trade, in a way that never shows up as a losing
        decision anywhere in the logs.

        Shorts are therefore marked at the ask and longs at the bid.
        """
        premium = 0.0
        for leg in legs:
            if leg.get("leg_status") != "OPEN":
                continue
            strike   = float(leg.get("strike", 0))
            opt_type = str(leg.get("option_type", ""))
            opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
            bid = float(opt.get("bid", 0) or 0)
            ask = float(opt.get("ask", 0) or 0)
            if leg["action"] == "SELL":
                mark = ask if ask > 0 else self._get_mark_price(leg, chain)
                premium += mark
            else:
                mark = bid if bid > 0 else self._get_mark_price(leg, chain)
                premium -= mark
        return premium

    def _round_trip_cost_pts(self, legs: List[dict], chain: dict) -> float:
        """
        v3.1: approximate cost, in premium points, of closing this position
        (statutory charges, brokerage and crossing the spread). Used to floor
        the profit lock and the 0DTE de-risk ladder so that "taking a small
        profit" is a profit AFTER costs rather than a rounding error that pays
        the broker and the exchange.
        """
        C02 = float(self.config.lot_size or 1)
        live = [l for l in legs if l.get("leg_status") != "CLOSED"]
        n_legs = max(len(live), 1)
        # v7: brokerage carries 18% GST like every other charge in this
        # repository (_compute_transaction_costs and
        # StrategyEngine._compute_costs both compute GST on
        # brokerage + exchange + sebi). It was the only cost line here that
        # was added net of GST.
        brokerage_pts = (self.config.brokerage_per_order * 1.18 * n_legs) / C02
        pct_pts = 0.0
        spread_pts = 0.0
        for leg in live:
            strike   = float(leg.get("strike", 0))
            opt_type = str(leg.get("option_type", ""))
            opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
            bid = float(opt.get("bid", 0) or 0)
            ask = float(opt.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                spread_pts += (ask - bid) / 2.0
            else:
                mid = float(leg.get("entry_price", 0) or 0)
                spread_pts += 0.35
            # On exit, STT applies to the legs being SOLD, i.e. the ones that
            # were originally bought.
            _stt = self.config.stt_options_sell if leg.get("action") == "BUY" else 0.0
            # v7: GST is levied on the exchange and SEBI charges (and on
            # brokerage, added separately below), never on STT - STT is a
            # tax, not a service. The 1.18 used to be applied to the whole
            # bracket, so the estimate charged 18% GST on a statutory tax,
            # and it disagreed with _compute_transaction_costs() and
            # StrategyEngine._compute_costs() in the same repository, both of
            # which compute GST as (brokerage + exchange + sebi) * 0.18.
            pct_pts += mid * (
                (self.config.exchange_txn_rate + self.config.sebi_rate) * 1.18
                + _stt
            )
        return round(brokerage_pts + pct_pts + spread_pts, 3)

    def _compute_current_premium(
        self, legs: List[dict], chain: dict
    ) -> float:
        """
        Compute current total premium for a position.
        For SELL legs: mark price is what we'd pay to close
        For BUY legs: mark price is what we'd receive to close
        Premium = sum(sell_leg_marks) - sum(buy_leg_marks)
        """
        premium = 0.0
        for leg in legs:
            if leg.get("leg_status") != "OPEN":
                continue
            mark = self._get_mark_price(leg, chain)
            if leg["action"] == "SELL":
                premium += mark
            else:
                premium -= mark
        return premium

    # ─────────────────────────────────────────────────────────────────────
    # PRE-TRADE VALIDATION
    # ─────────────────────────────────────────────────────────────────────

    def validate_pre_trade(
        self, params: dict, signals: dict
    ) -> Tuple[str, dict]:
        """
        Final validation before order placement.
        Checks that market conditions haven't changed since strategy decision.

        Checks:
        1. Daily loss limit (including projected loss from this trade)
        2. Delta limit (portfolio delta after trade)
        3. Vega limit (portfolio vega after trade)
        4. Chain availability and freshness
        5. Leg-level validation (bid/ask, OI, spread, price drift)
        6. Credit decay (credit has not decayed > 20% since computed)
        7. Timing (still within entry window)
        8. Hard exit buffer (enough time before hard exit)

        Returns ("GO", updated_params) or ("NO_GO", {"reason": ...})
        """
        state       = self.market_engine.state
        current_cap = float(state.get("current_capital", self.config.starting_capital) or 0)

        if current_cap <= 0:
            return "NO_GO", {"reason": "current_capital_zero_or_negative"}

        # ── Check 1: Daily loss limit ─────────────────────────────────────
        daily_pnl  = float(state.get("daily_pnl", 0.0) or 0.0)
        daily_loss = max(0.0, -daily_pnl)
        daily_loss_pct = daily_loss / current_cap

        if daily_loss_pct >= self.config.max_daily_loss_pct:
            return "NO_GO", {
                "reason": f"daily_loss_{daily_loss_pct*100:.2f}pct_exceeds_limit"
            }

        # Soft limit: reduce size at 80% of daily loss limit
        size_adj = 1.0
        if daily_loss_pct >= self.config.max_daily_loss_pct * 0.80:
            size_adj = 0.50
            self.logger.info(
                f"Soft daily loss limit ({daily_loss_pct*100:.2f}%): reducing size 50%"
            )

        # Check projected daily loss
        # v3.2: total_max_risk is the stop-efficacy BLEND the strategy
        # engine sizes with - it already assumes the stop works most of
        # the time, so it is roughly half the real maximum loss. Asking
        # "could this trade breach the daily cap" with a number that
        # presumes the stop holds defeats the purpose of the question;
        # the daily loss limit exists for the days when it does not.
        trade_max_loss  = float(
            params.get("total_structural_risk")
            or params.get("total_max_risk", 0)
            or 0
        )
        projected_pct   = (daily_loss + trade_max_loss) / current_cap
        max_projected   = self.config.max_daily_loss_pct * 1.25

        final_lots = int(params.get("final_lots", 1) or 1)

        if projected_pct > max_projected:
            max_additional = (current_cap * max_projected) - daily_loss
            if max_additional <= 0:
                return "NO_GO", {"reason": "projected_daily_loss_would_exceed_limit"}
            max_loss_per_lot = float(params.get("max_loss_per_lot", 1) or 1)
            max_lots_by_daily = max(1, int(max_additional / max_loss_per_lot))
            if max_lots_by_daily < final_lots:
                self.logger.info(
                    f"Lots reduced {final_lots} → {max_lots_by_daily} for daily loss limit"
                )
                final_lots = max_lots_by_daily

        final_lots = max(1, int(final_lots * size_adj))

        # ── Check 2: Chain availability ───────────────────────────────────
        chain = self.market_engine.last_chain
        if not chain:
            return "NO_GO", {"reason": "chain_unavailable_at_execution"}

        # ── Check 3: Leg-level validation ─────────────────────────────────
        for leg in params.get("legs", []):
            strike   = float(leg.get("strike", 0))
            opt_type = str(leg.get("option_type", ""))
            action   = str(leg.get("action", "SELL"))

            opt = chain.get(strike, {}).get(opt_type, {})
            if not opt:
                return "NO_GO", {"reason": f"leg_{strike:.0f}_{opt_type}_not_in_chain"}

            bid = float(opt.get("bid", 0) or 0)
            ask = float(opt.get("ask", 0) or 0)
            oi  = int(opt.get("oi",  0) or 0)

            if bid <= 0 and ask <= 0:
                return "NO_GO", {"reason": f"leg_{strike:.0f}_{opt_type}_no_bid_ask"}

            min_oi = 500 if action == "SELL" else 100
            if oi < min_oi:
                return "NO_GO", {"reason": f"leg_{strike:.0f}_{opt_type}_oi_{oi}_below_{min_oi}"}

            # v3.1: an 8% relative gate here was TIGHTER than the 15%/30%
            # gate the strategy engine used to build the trade, so sound
            # structures were computed and then discarded at the door — and a
            # Rs 1.50 protective wing quoted one tick wide reads as 6.7% and
            # could never pass reliably. Rupee-aware and aligned with
            # StrategyEngine._validate_leg.
            if bid > 0 and ask > 0:
                _mid_pt  = (bid + ask) / 2.0
                _rel_cap = 0.15 if action == "SELL" else 0.30
                _abs_cap = float(
                    getattr(self.config, "spread_abs_tolerance", 0.85)
                )
                if (ask - bid) > max(_mid_pt * _rel_cap, _abs_cap):
                    return "NO_GO", {
                        "reason": f"leg_{strike:.0f}_{opt_type}_spread_too_wide"
                    }

            # Price drift check: has price moved > 25% since strategy computed it?
            original_price = float(leg.get("exec_price", 0) or 0)
            current_price  = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
            if original_price > 0 and current_price > 0:
                drift = abs(current_price - original_price) / original_price
                if drift > 0.25:
                    return "NO_GO", {
                        "reason": f"leg_{strike:.0f}_{opt_type}_price_drifted_{drift*100:.0f}pct"
                    }

        # ── Check 4: Credit decay ─────────────────────────────────────────
        original_credit = float(params.get("entry_credit", 0) or 0)
        if original_credit > 0:
            current_gross = 0.0
            for leg in params.get("legs", []):
                strike   = float(leg.get("strike", 0))
                opt_type = str(leg.get("option_type", ""))
                action   = str(leg.get("action", "SELL"))
                opt      = chain.get(strike, {}).get(opt_type, {})
                if opt:
                    bid = float(opt.get("bid", 0) or 0)
                    ask = float(opt.get("ask", 0) or 0)
                    ltp = float(opt.get("ltp", 0) or 0)
                    if action == "SELL":
                        current_gross += bid if bid > 0 else ltp
                    else:
                        current_gross -= ask if ask > 0 else ltp

            if original_credit > 0 and current_gross < original_credit * 0.80:
                decay = (original_credit - current_gross) / original_credit
                return "NO_GO", {
                    "reason": f"credit_decayed_{decay*100:.0f}pct_since_computed"
                }

        # ── Check 5: Timing ───────────────────────────────────────────────
        current_time = now_ist().time()
        try:
            entry_start = datetime.strptime(
                state.get("entry_start", "09:45"), "%H:%M"
            ).time()
            entry_end = datetime.strptime(
                state.get("entry_end", "14:00"), "%H:%M"
            ).time()
        except Exception:
            entry_start = self.config.trading_window_start
            entry_end   = self.config.trading_window_last_entry

        if current_time > entry_end:
            return "NO_GO", {"reason": f"past_entry_window_{entry_end}"}
        if current_time < entry_start:
            return "NO_GO", {"reason": f"before_entry_window_{entry_start}"}

        # ── Check 6: Hard exit buffer ─────────────────────────────────────
        try:
            hard_exit = datetime.strptime(
                state.get("hard_exit_time", "15:00"), "%H:%M"
            ).time()
        except Exception:
            hard_exit = self.config.hard_exit_time

        dte        = signals.get("actual_dte")
        min_buffer = 90 if dte == 0 else 90
        dt1        = datetime.combine(today_ist(), current_time)
        dt2        = datetime.combine(today_ist(), hard_exit)
        mins_to_exit = (dt2 - dt1).total_seconds() / 60.0

        if mins_to_exit < min_buffer:
            return "NO_GO", {
                "reason": f"only_{mins_to_exit:.0f}min_before_hard_exit_need_{min_buffer}"
            }

        # ── Update params with validated lot count ────────────────────────
        params["final_lots"]    = final_lots
        params["total_max_risk"] = float(params.get("max_loss_per_lot", 0) or 0) * final_lots

        # ── Check 7: broker margin (live only, advisory by default) ───────
        # The engine's own estimate is a wing-based approximation, so this is
        # a warning lane, not a gate: blocking entries on a guess would strand
        # positions that have no other way out. gate mode refuses the entry.
        mode = str(getattr(self.config, "margin_preflight_mode", "warn") or "warn")
        if (
            mode != "off"
            and not self.config.paper_trade_mode
            and float(params.get("estimated_margin", 0) or 0) > 0
        ):
            available = self._broker_available_margin()
            if available is not None:
                need = float(params["estimated_margin"]) * 1.15
                if available < need:
                    msg = (
                        f"broker available margin Rs{available:,.0f} below the "
                        f"Rs{need:,.0f} this {final_lots}-lot entry needs "
                        f"({'gating' if mode == 'gate' else 'advisory'})"
                    )
                    if mode == "gate":
                        return "NO_GO", {"reason": f"margin_{msg}"}
                    self._alert("WARNING", msg)
                    params["margin_preflight_warning"] = round(need - available, 2)
                else:
                    params["margin_headroom_rupees"] = round(available - need, 2)

        return "GO", params

    def _broker_available_margin(self) -> Optional[float]:
        """Live available margin, or None when it cannot be determined.

        v2 reports F&O margin under data.equity.available_margin; v3 renamed
        the field to available_to_trade.cash_available_to_trade.total. Both
        shapes are read so the check survives an endpoint upgrade in either
        direction. An API failure returns None: unknown headroom must never
        become a reason to stop unwinding a position.
        """
        try:
            data = self.client.get_funds_and_margin() or {}
        except Exception as e:
            self.logger.debug(f"margin pre-flight unavailable: {e}")
            return None
        candidates: List[float] = []
        equity = data.get("equity") if isinstance(data.get("equity"), dict) else {}
        for src in (equity, data):
            try:
                val = float(src.get("available_margin") or 0.0)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                candidates.append(val)
        try:
            at = (
                data.get("available_to_trade", {})
                .get("cash_available_to_trade", {})
                .get("total")
            )
            val = float(at or 0.0)
            if val > 0:
                candidates.append(val)
        except (TypeError, ValueError, AttributeError):
            pass
        return min(candidates) if candidates else None


    # ─────────────────────────────────────────────────────────────────────
    # ENTRY EXECUTION
    # ─────────────────────────────────────────────────────────────────────

    def _emergency_unwind(
        self, filled_legs: List[dict], lots: int, chain: dict
    ) -> None:
        """
        Emergency unwind of partially filled entry.
        Called when some but not all legs have been filled.
        Logs CRITICAL if any unwind fails (manual intervention required).
        """
        for leg in filled_legs:
            try:
                self.executor.execute_leg_exit(leg, chain, lots)
                self.logger.warning(
                    f"Emergency unwind: closed {leg['action']} "
                    f"{leg['option_type']} {leg['strike']:.0f}"
                )
            except Exception as e:
                self.logger.critical(
                    f"EMERGENCY UNWIND FAILED for {leg['strike']:.0f} "
                    f"{leg['option_type']}: {e}. "
                    f"MANUAL INTERVENTION REQUIRED."
                )

    def execute_entry(self, params: dict, signals: dict) -> Optional[str]:
        """
        Execute all legs of a new position.

        Flow:
        1. Execute BUY legs first (defined risk), then SELL legs
        2. On partial fill failure: emergency unwind all filled legs
        3. Compute actual entry costs
        4. Persist position, legs, and trade_entry to database
        5. Update session state
        6. Return position_id or None on failure
        """
        position_id = str(uuid.uuid4())
        lots        = int(params.get("final_lots", 1) or 1)
        chain       = self.market_engine.last_chain
        filled_legs: List[dict] = []

        try:
            # Execute BUY legs first (reduces risk on partial fill)
            buy_legs  = [l for l in params["legs"] if l["action"] == "BUY"]
            sell_legs = [l for l in params["legs"] if l["action"] == "SELL"]

            for leg in buy_legs + sell_legs:
                fill = self.executor.execute_leg_entry(leg, lots, chain)
                filled_legs.append({**leg, "fill": fill})

        except Exception as e:
            self.logger.error(f"Entry execution failed: {e}")
            if filled_legs:
                self.logger.critical(
                    f"PARTIAL FILL on entry — {len(filled_legs)}/{len(params['legs'])} "
                    f"legs filled. Attempting emergency unwind."
                )
                self._emergency_unwind(filled_legs, lots, chain)
            return None

        # Build actual fill legs
        actual_fill_legs = [
            {**fl, "entry_price": fl["fill"]["fill_price"],
             "exec_price": fl["fill"]["fill_price"]}
            for fl in filled_legs
        ]

        # Compute actual entry costs
        entry_costs = self._compute_transaction_costs(actual_fill_legs, lots, "ENTRY")
        actual_entry_costs_rs = entry_costs["total_rupees"]

        now = now_ist()

        # ── Persist position ──────────────────────────────────────────────
        self.db.insert("positions", {
            "position_id":              position_id,
            "trading_date":             today_ist().isoformat(),
            "strategy_name":            params["strategy_name"],
            "strategy_type":            params["strategy_type"],
            "selection_reason":         params["selection_reason"],
            "target_expiry":            params["target_expiry"],
            "actual_dte":               params["actual_dte"],
            "entry_time":               now.isoformat(),
            "entry_spot":               params.get("entry_spot"),
            "entry_vix":                params.get("entry_vix"),
            "entry_vrp":                params.get("entry_vrp"),
            "entry_vrp_smoothed":       params.get("entry_vrp_smoothed"),
            "entry_credit":             params.get("entry_credit"),
            "gross_credit":             params.get("gross_credit"),
            "opening_straddle_at_entry":params.get("opening_straddle_at_entry"),
            "total_slippage":           params.get("total_slippage"),
            "entry_costs_rupees":       actual_entry_costs_rs,
            "stop_premium":             params.get("stop_premium"),
            "target_premium":           params.get("target_premium"),
            "price_stop_pts":           params.get("price_stop_pts"),
            "price_stop_level_call":    params.get("price_stop_level_call"),
            "price_stop_level_put":     params.get("price_stop_level_put"),
            "hard_exit_time":           params.get("hard_exit_time"),
            "final_lots":               lots,
            "max_loss_per_lot":         params.get("max_loss_per_lot"),
            "total_max_risk":           params.get("total_max_risk"),
            "estimated_margin":         params.get("estimated_margin"),
            "status":                   "OPEN",
            "last_known_premium":       params.get("last_known_premium"),
            "profit_lock_activated":    0,
            "profit_lock_stop_level":   None,
            "paper_trade":              1 if self.config.paper_trade_mode else 0,
            "raw_params_json":          json.dumps(params, default=str),
            "vol_regime_at_entry":      params.get("vol_regime_at_entry"),
            "price_regime_at_entry":    params.get("price_regime_at_entry"),
            "positioning_at_entry":     params.get("positioning_at_entry"),
            "confidence_at_entry":      params.get("confidence_level_at_entry"),
            "confidence_score_at_entry":params.get("confidence_score_at_entry"),
            "final_regime_at_entry":    params.get("final_regime_at_entry"),
            "event_day":                int(bool(params.get("event_day", False))),
            "event_name":               params.get("event_name", ""),
            "defined_risk_only":        int(bool(params.get("defined_risk_only", False))),
            "is_borderline_sell":       int(bool(params.get("is_borderline_sell", False))),
            "created_at":               now.isoformat(),
            "updated_at":               now.isoformat(),
        })

        # ── Persist position legs ─────────────────────────────────────────
        for fl in filled_legs:
            fill_price = fl["fill"]["fill_price"]
            bid        = float(fl.get("bid", 0) or 0)
            ask        = float(fl.get("ask", 0) or 0)
            quoted_mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else float(fl.get("exec_price", 0) or 0)

            self.db.insert("position_legs", {
                "position_id":          position_id,
                "strike":               fl["strike"],
                "option_type":          fl["option_type"],
                "action":               fl["action"],
                "qty":                  lots * self.config.lot_size,
                "entry_price":          fill_price,
                "exit_price":           None,
                "entry_bid":            bid,
                "entry_ask":            ask,
                "entry_delta":          float(fl.get("delta", 0) or 0),
                "entry_gamma":          float(fl.get("gamma", 0) or 0),
                "entry_vega":           float(fl.get("vega",  0) or 0),
                "entry_theta":          float(fl.get("theta", 0) or 0),
                "entry_iv":             float(fl.get("iv",    0) or 0),
                "entry_oi":             int(fl.get("oi",      0) or 0),
                "exit_delta":           None,
                "broker_order_id_entry":fl["fill"]["order_id"],
                "broker_order_id_exit": None,
                "quoted_mid_at_entry":  quoted_mid,
                "quoted_mid_at_exit":   None,
                "leg_status":           "OPEN",
            })

        # ── Persist trade entry ───────────────────────────────────────────
        self._persist_trade_entry(position_id, params, signals, actual_entry_costs_rs)

        # ── Update session state ──────────────────────────────────────────
        state = self.market_engine.state
        state["entry_count"]       = state.get("entry_count", 0) + 1
        state["consecutive_stops"] = 0
        state["last_entry_time"]   = now.isoformat()
        self.market_engine._save_session_state()

        print_section(f"POSITION OPENED: {params['strategy_name']}")
        print_kv_table({
            "Position ID":       position_id[:16] + "...",
            "Lots":              lots,
            "Net Credit (pts)":  params.get("entry_credit"),
            "Stop (pts)":        params.get("stop_premium"),
            "Target (pts)":      params.get("target_premium"),
            "Price Stop (pts)":  params.get("price_stop_pts"),
            "Max Risk (Rs)":     params.get("total_max_risk"),
            "DTE":               params.get("actual_dte"),
            "Vol Regime":        params.get("vol_regime_at_entry"),
            "Price Regime":      params.get("price_regime_at_entry"),
            "Confidence":        params.get("confidence_level_at_entry"),
            "Borderline Sell":   params.get("borderline_sell"),
            "Paper Trade":       self.config.paper_trade_mode,
        })
        self.logger.info(
            f"ENTRY EXECUTED: {params['strategy_name']} "
            f"position_id={position_id} "
            f"lots={lots} "
            f"credit={params.get('entry_credit'):.2f}pts "
            f"paper={self.config.paper_trade_mode}"
        )
        return position_id

    def _persist_trade_entry(
        self,
        position_id:        str,
        params:             dict,
        signals:            dict,
        actual_costs_rs:    float,
    ) -> None:
        """Persist trade entry record to trade_entries table."""
        try:
            self.db.insert("trade_entries", {
                "trade_id":                 position_id,
                "position_id":              position_id,
                "strategy_name":            params["strategy_name"],
                "entry_time":               now_ist().isoformat(),
                "trading_date":             today_ist().isoformat(),
                "day_label":                self.market_engine.state.get("day_label"),
                "entry_spot":               params.get("entry_spot"),
                "entry_vix":                params.get("entry_vix"),
                "entry_vrp_raw":            signals.get("vrp_raw"),
                "entry_vrp_smoothed":       params.get("entry_vrp_smoothed"),
                "entry_atm_iv":             signals.get("atm_iv"),
                "entry_parkinson_rv":       signals.get("parkinson_rv"),
                "entry_adx_15":             signals.get("adx_15"),
                "entry_vwap":               signals.get("vwap"),
                "entry_vwap_dist_pct":      signals.get("vwap_dist_pct"),
                "entry_pcr":                signals.get("pcr"),
                "entry_skew_ratio":         signals.get("skew_ratio"),
                "or_width":                 signals.get("or_width"),
                "or_condition":             signals.get("or_condition"),
                "iv_behavior":              signals.get("iv_behavior"),
                "iv_change_pct_from_open":  signals.get("iv_change_pct_from_open"),
                "day_move_used_at_entry":   signals.get("day_move_used_pct"),
                "opening_straddle_at_entry":params.get("opening_straddle_at_entry"),
                "vol_regime_at_entry":      params.get("vol_regime_at_entry"),
                "price_regime_at_entry":    params.get("price_regime_at_entry"),
                "positioning_at_entry":     params.get("positioning_at_entry"),
                "confidence_level_at_entry":params.get("confidence_level_at_entry"),
                "confidence_score_at_entry":params.get("confidence_score_at_entry"),
                "final_regime_at_entry":    params.get("final_regime_at_entry"),
                "target_expiry":            params.get("target_expiry"),
                "actual_dte":               params.get("actual_dte"),
                "legs_json":                json.dumps(params.get("legs", []), default=str),
                "entry_credit":             params.get("entry_credit"),
                "gross_credit":             params.get("gross_credit"),
                "total_slippage":           params.get("total_slippage"),
                "entry_costs_pts":          params.get("total_costs_pts"),
                "entry_costs_rupees":       actual_costs_rs,
                "stop_premium":             params.get("stop_premium"),
                "target_premium":           params.get("target_premium"),
                "price_stop_pts":           params.get("price_stop_pts"),
                "hard_exit_time":           params.get("hard_exit_time"),
                "final_lots":               params.get("final_lots"),
                "max_loss_per_lot":         params.get("max_loss_per_lot"),
                "total_max_risk":           params.get("total_max_risk"),
                "capital_at_entry":         self.market_engine.state.get("current_capital"),
                "daily_pnl_at_entry":       self.market_engine.state.get("daily_pnl"),
                "paper_trade":              1 if self.config.paper_trade_mode else 0,
                "selection_reason":         params.get("selection_reason"),
                "is_borderline_sell":       int(bool(params.get("is_borderline_sell", False))),
                "event_day":                int(bool(params.get("event_day", False))),
                "event_name":               params.get("event_name", ""),
                "calibration_tier_at_entry":params.get("calibration_tier_at_entry", 0),
                "created_at":               now_ist().isoformat(),
            })
        except Exception as e:
            self.logger.warning(f"trade_entries insert error: {e}")

    def process_entry_decision(
        self, decision: dict, signals: dict
    ) -> Optional[str]:
        """
        Process an ENTER decision from strategy_engine.
        1. Run pre-trade validation
        2. Execute entry if validation passes
        Returns position_id or None.
        """
        if decision.get("action") != "ENTER":
            return None

        go, result = self.validate_pre_trade(decision["params"], signals)

        if go != "GO":
            reason = result.get("reason", "unknown")
            print_section("PRE-TRADE VALIDATION: NO_GO")
            print(f"  Reason: {reason}")
            self.logger.warning(f"PRE_TRADE_NO_GO: {reason}")
            return None

        print_section("PRE-TRADE VALIDATION: GO")
        return self.execute_entry(result, signals)

    # ─────────────────────────────────────────────────────────────────────
    # POSITION MONITORING — 7-PRIORITY EXIT SYSTEM
    # ─────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────
    # v5 — NET-DEBIT (LONG-PREMIUM) EXIT LADDER
    # ─────────────────────────────────────────────────────────────────────

    def _monitor_debit_position(
        self,
        position:         dict,
        open_legs:        List[dict],
        chain:            dict,
        current_time:     dtime,
        spot:             float,
        current_premium:  float,
        liq_premium:      float,
    ) -> Tuple[str, int, dict]:
        """Exit ladder for a net-debit structure (the engine's own premium).

        The 7-priority ladder in monitor_position is written for a SOLD
        structure: it stops when the premium EXPANDS, targets when it DECAYS,
        locks profit against a credit, and derives its spot backstop from a
        short strike. A bought option inverts every one of those, and
        leaving a debit position to fall through that ladder means it has no
        stop at all — which is how a Rs 6,000 breakout ticket becomes a
        lottery ticket. So a long position is managed on the value of what is
        owned: liquidation value for decisions the engine can act on, mid
        value for the ratchet, so one wide print cannot lock in a fake gain.

        Priorities: premium stop (loss), ratcheted profit lock (free trade),
        target (planned capture), late-window flatten (never carry a long
        option into the close), hard exit.
        """
        cfg = self.config

        # The credit ladder's marks are "cash needed to close". For a net
        # debit position the position's own value is therefore their
        # negative, and entry_credit is stored negative (the premium paid).
        entry_value = abs(float(position.get("entry_credit") or 0.0))
        value       = -float(liq_premium)
        value_mid   = -float(current_premium)
        if entry_value <= 0:
            return "HOLD", 0, {"reason_detail": "debit_position_no_entry_value"}

        raw = {}
        try:
            raw = json.loads(position.get("raw_params_json") or "{}")
        except Exception:
            raw = {}

        stop   = float(position.get("stop_premium") or raw.get("stop_premium") or 0.0)
        target = float(position.get("target_premium") or raw.get("target_premium") or 0.0)
        lock   = float(raw.get("profit_lock_trigger") or
                       (entry_value * (1.0 + float(getattr(cfg, "momentum_lock_trigger", 0.25)))))
        rt_cost = self._round_trip_cost_pts(open_legs, chain)

        # ── D1: premium stop ─────────────────────────────────────────────
        if stop > 0 and value <= stop:
            self.logger.warning(
                f"DEBIT PREMIUM STOP: {position['strategy_name']} "
                f"value={value:.2f} <= stop={stop:.2f} (entry {entry_value:.2f})"
            )
            return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                "reason_detail": f"momentum_premium_stop_{value:.2f}<={stop:.2f}",
                "current_premium": current_premium,
                "liquidation_premium": liq_premium,
            }

        # ── D2: profit lock, ratcheted upward on the mid ─────────────────
        activated = bool(position.get("profit_lock_activated"))
        locked    = position.get("profit_lock_stop_level")
        if value_mid >= lock:
            keep = float(getattr(cfg, "momentum_lock_keep_frac", 0.50))
            new_level = max(
                value_mid - max(value_mid - entry_value, 0.0) * keep,
                entry_value + rt_cost,
            )
            cur_level = float(locked or 0.0)
            if not activated or new_level > cur_level:
                self.db.update(
                    "positions",
                    {
                        "profit_lock_activated":  1,
                        "profit_lock_stop_level": new_level,
                        "stop_premium":           max(stop, new_level),
                        "updated_at":             now_ist().isoformat(),
                    },
                    {"position_id": position["position_id"]},
                )
                return "TIGHTEN_STOP", EXIT_PRIORITY_PROFIT_LOCK, {
                    "reason_detail": "momentum_profit_lock_ratchet",
                    "new_level": new_level,
                }
            if value <= new_level:
                return "CLOSE_TARGET", EXIT_PRIORITY_PROFIT_LOCK, {
                    "reason_detail": "momentum_lock_given_back",
                    "locked_level": new_level,
                }

        # ── D3: planned target ───────────────────────────────────────────
        if target > 0 and value >= target:
            self.logger.info(
                f"DEBIT TARGET: {position['strategy_name']} value={value:.2f} "
                f">= target={target:.2f}"
            )
            return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                "reason_detail": "momentum_target_reached",
                "value": value,
            }

        # ── D4: never carry a long option into the closing bell ──────────
        try:
            hard_exit = datetime.strptime(
                position.get("hard_exit_time") or "15:00", "%H:%M").time()
        except Exception:
            hard_exit = cfg.hard_exit_time
        mins_left = (
            datetime.combine(now_ist().date(), hard_exit)
            - datetime.combine(now_ist().date(), current_time)
        ).total_seconds() / 60.0
        window = float(getattr(cfg, "momentum_final_window_min", 45))
        if mins_left <= window:
            if value >= entry_value + rt_cost:
                return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                    "reason_detail": "momentum_flat_before_close",
                    "minutes_left": mins_left,
                }
        if current_time >= hard_exit:
            return "HARD_EXIT_15:00", EXIT_PRIORITY_HARD_EXIT, {
                "hard_exit_time": str(hard_exit),
                "current_time": str(current_time),
            }

        return "HOLD", 0, {
            "current_premium":     current_premium,
            "liquidation_premium": liq_premium,
            "debit_value":         value,
            "entry_value":         entry_value,
        }

    def monitor_position(
        self, position: dict, signals: dict
    ) -> Tuple[str, int, dict]:
        """
        Monitor one open position and determine exit action.

        Returns (action, priority, context) where:
        action: "HOLD", "TIGHTEN_STOP", "CLOSE_STOP", "CLOSE_TARGET",
                "HARD_EXIT_15:00"
        priority: exit priority number (1-7) or 0 for HOLD
        context: dict with details

        Exit priorities checked in strict order:
        1. Delta breach (immediate risk)
        2. Spot proximity (structural risk)
        3. Price stop (predefined loss limit)
        4. Profit lock (convert winning trade to free trade)
        5. Cheap buyback (eliminate tail risk)
        6. Time-based target (take profit at scheduled times)
        7. Hard exit (time-based forced close)
        """
        legs         = self._get_position_legs(position["position_id"])
        open_legs    = [l for l in legs if l.get("leg_status") == "OPEN"]
        chain        = self.market_engine.last_chain
        current_time = now_ist().time()
        spot         = float(signals.get("spot") or 0)

        # Get chain for the correct expiry
        chain_expiry = self.market_engine.last_chain_expiry
        if chain_expiry and chain_expiry.isoformat() != position.get("target_expiry"):
            chain = {}  # Wrong expiry chain — use empty dict (falls back to entry price)

        # v3.1: two marks are now maintained. current_premium is the MID —
        # the right number to report and to trigger a stop on (it is not
        # jumpy). liq_premium is the LIQUIDATION value — what it actually
        # costs to get out — and it is the only honest basis for a
        # profit-taking decision.
        current_premium = self._compute_current_premium(open_legs, chain)
        liq_premium     = self._liquidation_premium(open_legs, chain)
        if not chain:
            liq_premium = current_premium

        self.db.update(
            "positions",
            {
                "last_known_premium":       current_premium,
                "last_liquidation_premium": liq_premium,
                "updated_at":               now_ist().isoformat(),
            },
            {"position_id": position["position_id"]},
        )

        # v3.2: actual_dte is read by the priority-1 delta gate below, so
        # it is resolved before the exit ladder rather than half way down.
        actual_dte = int(position.get("actual_dte") or 0)
        entry_credit = float(position.get("entry_credit") or 0)
        gross_credit = float(position.get("gross_credit") or entry_credit)
        stop_premium = float(position.get("stop_premium") or 0)
        target_premium = float(position.get("target_premium") or 0)
        opening_straddle = float(position.get("opening_straddle_at_entry") or 0)
        profit_lock_activated = bool(position.get("profit_lock_activated"))
        profit_lock_stop_level = position.get("profit_lock_stop_level")

        # ── v5: a net-debit position runs its own ladder ─────────────────
        # Everything below is written for a structure that was SOLD: premium
        # stops on expansion, targets on decay, a spot backstop derived from a
        # short strike, a cheap-buyback on the short legs. A bought option
        # inverts all of it, and falling through would leave a long position
        # with no stop at all until the hard exit.
        if entry_credit < 0 or str(position.get("strategy_type") or "").upper() == "BUY":
            return self._monitor_debit_position(
                position, open_legs, chain, current_time, spot,
                current_premium, liq_premium,
            )

        # ── Priority 1: Delta breach ──────────────────────────────────────
        # Short leg delta > 0.40 → close immediately
        for leg in open_legs:
            if leg["action"] != "SELL":
                continue
            strike   = float(leg.get("strike", 0))
            opt_type = str(leg.get("option_type", ""))
            opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
            cur_delta = abs(float(opt.get("delta", leg.get("entry_delta", 0)) or 0))

            strategy_name_p1 = position.get("strategy_name", "")
            # v3.2: a single flat 0.28 delta exit for every DTE is barely
            # above the 0.15-0.22 delta the engine sells at, so ordinary
            # drift - not a threat to the structure - closes winners at a
            # scratch and pays the round trip for nothing. Expiry day
            # needs the widest band precisely because delta moves fastest
            # there and mean-reverts just as fast; the premium stop and
            # the spot backstop remain the real risk controls.
            #
            # Entry-relative breach: the absolute thresholds below are
            # FLOORS, and the live threshold is the short's own entry
            # delta plus a buffer. A flat 0.30 exit against a 0.43 entry
            # is a stop-loss placed through the entry price — measured
            # 2026-09-09, three bear-call spreads stopped out 15 seconds
            # after entry with no adverse move at all. EM-clamped DTE>=1
            # shorts legitimately carry higher delta (delta prices days
            # of risk; the hold is hours), so the exit must adapt to what
            # was sold: entry + 0.15 is roughly the same adverse spot
            # move the backstop defends, which is exactly when this
            # ladder rung should fire. 0DTE behaviour is unchanged in
            # practice (0.22 + 0.15 = 0.37 against the old 0.35).
            if "BUTTERFLY" in strategy_name_p1:
                delta_thresh_p1 = 0.72
            else:
                if actual_dte == 0:
                    _abs_p1 = float(
                        getattr(self.config, "delta_close_dte0", 0.35)
                    )
                else:
                    _abs_p1 = float(
                        getattr(self.config, "delta_close_dte1p", 0.30)
                    )
                try:
                    _entry_d_p1 = abs(float(leg.get("entry_delta", 0) or 0))
                except (TypeError, ValueError):
                    _entry_d_p1 = 0.0
                if _entry_d_p1 > 0:
                    _buf_p1 = float(getattr(
                        self.config, "delta_breach_buffer", 0.15
                    ))
                    _cap_p1 = float(getattr(
                        self.config, "delta_breach_cap", 0.65
                    ))
                    delta_thresh_p1 = min(
                        max(_entry_d_p1 + _buf_p1, _abs_p1), _cap_p1
                    )
                else:
                    delta_thresh_p1 = _abs_p1
            if cur_delta > delta_thresh_p1:
                self.logger.warning(
                    f"PRIORITY 1 DELTA BREACH: {leg['action']} {opt_type} "
                    f"{strike:.0f} delta={cur_delta:.3f} > {delta_thresh_p1}"
                )
                return "CLOSE_STOP", EXIT_PRIORITY_DELTA_BREACH, {
                    "reason_detail": f"delta_breach_{cur_delta:.3f}",
                    "strike": strike,
                    "opt_type": opt_type,
                    "cur_delta": cur_delta,
                }

        # ── Priority 2: Spot proximity ────────────────────────────────────
        # Spot within 40pts of any short strike
        strategy_name_p2 = position.get("strategy_name", "")
        raw_params_p2 = json.loads(position.get("raw_params_json") or "{}")
        wing_width_p2 = float(raw_params_p2.get("wing_width") or 150)
        # v3.1: a flat 40pt proximity is 0.22% of an 18,000 index and only
        # 0.15% of a 26,000 one — the structural protection silently weakened
        # as NIFTY rose, so by 2026 the engine was sitting closer to its short
        # strikes than it was designed to. Scaled to spot, with the configured
        # absolute value kept as a floor.
        _prox_base = float(self.config.spot_proximity_pts or 40)
        _prox_pct  = float(getattr(self.config, "spot_proximity_pct", 0.0016))
        _prox_scaled = max(_prox_base, spot * _prox_pct) if spot > 0 else _prox_base
        proximity_pts = (
            max(int(wing_width_p2 * 0.55), int(_prox_scaled))
            if "BUTTERFLY" in strategy_name_p2
            else _prox_scaled
        )
        # ── v3.2: proximity to a short strike is the wrong test for an ─
        # at-the-money structure. An iron butterfly is SOLD at the money:
        # spot sits on the short strike from the first tick, so
        # abs(spot - strike) was ~0 against a proximity band of
        # 0.55 x wing and the position was closed for "structural risk"
        # on its own entry cycle - every time, before it could earn a
        # rupee of the theta it was opened to collect. What actually
        # threatens a fly is spot reaching the LONG wings, where the
        # structure is at max loss, so that is what is measured.
        if spot > 0:
            _prox_action = "SELL"
            if "BUTTERFLY" in strategy_name_p2:
                _prox_action = "BUY"
            # v3.9: on expiry-day verticals the absolute proximity band
            # can exceed the whole gap to the short strike (a delta-0.3+
            # short ~45 points away against a 40pt band), which would
            # flatten the trade at entry+5pts regardless of structure.
            # The band is then bounded by (1 - prox_gap_frac) of the
            # entry gap for each short leg - the same line the EV gate
            # now prices - so the defense is where the risk model said
            # it would be when the trade was approved.
            _prox_dte0 = (actual_dte == 0) and \
                ("BUTTERFLY" not in strategy_name_p2)
            _prox_gap_frac = float(getattr(self.config, "prox_gap_frac_dte0", 0.70))
            _entry_spot_p2 = float(position.get("entry_spot") or 0)
            for leg in open_legs:
                if leg["action"] != _prox_action:
                    continue
                strike = float(leg.get("strike", 0))
                _band = proximity_pts
                if _prox_dte0 and _entry_spot_p2 > 0:
                    _gap = abs(strike - _entry_spot_p2)
                    if _gap > 0:
                        _band = min(
                            proximity_pts,
                            max((1.0 - _prox_gap_frac) * _gap, 10.0),
                        )
                if abs(spot - strike) <= _band:
                    self.logger.warning(
                        f"PRIORITY 2 SPOT PROXIMITY: spot={spot:.0f} "
                        f"within {_band:.0f}pts of {_prox_action} "
                        f"{leg['option_type']} {strike:.0f}"
                    )
                    return "CLOSE_STOP", EXIT_PRIORITY_SPOT_PROXIMITY, {
                        "reason_detail": f"spot_proximity_{abs(spot - strike):.0f}pts",
                        "strike": strike,
                        "spot": spot,
                    }

        # ── Priority 3: Price stop ────────────────────────────────────────
        # Price stop = 0.30 × opening straddle from short strike level
        # Also check the stored price_stop_level_call and price_stop_level_put
        if spot > 0 and opening_straddle > 20:
            price_stop_pts = int(opening_straddle * self.config.price_stop_straddle_mult)

            price_stop_call = position.get("price_stop_level_call")
            price_stop_put  = position.get("price_stop_level_put")

            if price_stop_call and spot >= price_stop_call:
                self.logger.warning(
                    f"PRIORITY 3 PRICE STOP (call): spot={spot:.0f} >= "
                    f"price_stop_call={price_stop_call:.0f}"
                )
                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                    "reason_detail": f"price_stop_call_breached_{spot:.0f}>={price_stop_call:.0f}",
                    "spot": spot,
                    "price_stop_level": price_stop_call,
                }

            if price_stop_put and spot <= price_stop_put:
                self.logger.warning(
                    f"PRIORITY 3 PRICE STOP (put): spot={spot:.0f} <= "
                    f"price_stop_put={price_stop_put:.0f}"
                )
                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                    "reason_detail": f"price_stop_put_breached_{spot:.0f}<={price_stop_put:.0f}",
                    "spot": spot,
                    "price_stop_level": price_stop_put,
                }

        # Also check premium-based stop (for cases without price stop levels).
        # v3.1: the trigger deliberately stays on the MID so that a single
        # wide print cannot stop the position out on quote noise — but a hard
        # guard now fires if the price we could genuinely get out at has run
        # well past the stop. That is a real loss, not a quoting artefact, and
        # previously the engine would sit through it.
        if entry_credit > 0 and stop_premium > 0:
            if current_premium >= stop_premium:
                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                    "reason_detail": f"premium_stop_{current_premium:.2f}>={stop_premium:.2f}",
                    "current_premium": current_premium,
                    "stop_premium": stop_premium,
                }
            if liq_premium >= stop_premium * 1.20:
                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                    "reason_detail": (
                        f"liquidation_stop_{liq_premium:.2f}>="
                        f"{stop_premium * 1.20:.2f}"
                    ),
                    "current_premium": current_premium,
                    "liquidation_premium": liq_premium,
                    "stop_premium": stop_premium,
                }

        # ── Priority 4: Profit lock ───────────────────────────────────────
        # Move stop to breakeven when profit reaches threshold
        if entry_credit > 0 and gross_credit > 0:
            # v3.1: measured on the liquidation mark — profit you cannot
            # actually take is not profit, and locking against a mid you
            # cannot trade at is how a "free trade" becomes a loser.
            profit_pct = (gross_credit - liq_premium) / gross_credit

            # Profit lock threshold: 40% for DTE0, 25% for DTE1+
            lock_thresh = (
                self.config.profit_lock_pct_dte0
                if actual_dte == 0
                else self.config.profit_lock_pct_dte1plus
            )

            if profit_pct >= lock_thresh and not profit_lock_activated:
                # v3.1: the old lock gave back HALF of everything achieved and
                # then floored the stop at 0.80 x entry_credit — i.e. it was
                # willing to hand back 20% of the credit from a position that
                # was already comfortably in profit, and max() made that floor
                # the binding constraint. Together with the inverted target
                # ladder (F1) this is exactly why winners round-tripped to
                # scratch. The give-back is cut to a quarter and the lock is
                # floored so a locked trade cannot finish worse than covering
                # its own round trip.
                # v1: a quarter give-back chokes weekly winners on normal
                # afternoon retracements - the stop sits inside routine
                # premium noise and converts it into stop-outs (measured
                # 2026-09-10: locked win stopped at +873, held-to-close
                # worth +1,500+). Widen to one HALF of achieved profit -
                # the professional trail for intraday premium - while the
                # v3.1 entry-minus-round-trip floor below still guarantees
                # a locked trade cannot finish red. The time-target ladder
                # (P6) and the 15:20 hard exit bound the ride.
                _achieved = gross_credit - liq_premium
                _keep = _achieved * 0.50
                new_stop = liq_premium + _keep
                _rt_cost = self._round_trip_cost_pts(open_legs, chain)
                new_stop = min(new_stop, max(entry_credit - _rt_cost, 0.05))
                self.db.update(
                    "positions",
                    {
                        "stop_premium":          new_stop,
                        "profit_lock_activated": 1,
                        "profit_lock_stop_level": new_stop,
                        "updated_at":            now_ist().isoformat(),
                    },
                    {"position_id": position["position_id"]},
                )
                self.logger.info(
                    f"PRIORITY 4 PROFIT LOCK: {position['strategy_name']} "
                    f"profit={profit_pct*100:.0f}% >= {lock_thresh*100:.0f}% — "
                    f"stop moved to breakeven {new_stop:.2f}pts"
                )
                return "TIGHTEN_STOP", EXIT_PRIORITY_PROFIT_LOCK, {
                    "profit_pct": profit_pct,
                    "new_stop": new_stop,
                }

            # If profit lock is active, check if we've given back too much
            if profit_lock_activated and profit_lock_stop_level:
                if liq_premium >= float(profit_lock_stop_level):
                    return "CLOSE_TARGET", EXIT_PRIORITY_PROFIT_LOCK, {
                        "reason_detail": "profit_lock_stop_hit",
                        "current_premium": current_premium,
                        "profit_lock_stop": profit_lock_stop_level,
                    }

        # ── Priority 5: Cheap buyback ─────────────────────────────────────
        # Buy back any short leg ≤ 2pts after 13:00
        cheap_thresh = self.config.cheap_buyback_pts  # default 2.0
        cheap_after  = self.config.cheap_buyback_after_time  # default 13:00

        # ── v3.2: cheap buyback must not liquidate the TESTED side ────
        # v3.1 closed the ENTIRE position the moment any one short leg
        # printed below the threshold. On a condor that leg is cheap for
        # exactly one reason - the other side is being tested - so the
        # rule fired precisely when the remaining short was at its most
        # expensive, and crystallised the worst available price on the
        # side that mattered. It converted a manageable one-sided test
        # into a realised loss, repeatedly, and logged it as a target.
        #
        # A full close is now taken only when it is genuinely protective:
        # a single-short structure (vertical), or a multi-leg structure
        # where EVERY short is cheap, or where the whole position can be
        # liquidated at a small fraction of the credit taken in - i.e.
        # the trade has already made essentially all of its money.
        if current_time >= cheap_after:
            short_legs_cb = [l for l in open_legs if l["action"] == "SELL"]
            cheap_marks = []
            for leg in short_legs_cb:
                strike   = float(leg.get("strike", 0))
                opt_type = str(leg.get("option_type", ""))
                opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
                bid      = float(opt.get("bid", 0) or 0)
                ask      = float(opt.get("ask", 0) or 0)
                mark     = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else bid
                cheap_marks.append((strike, opt_type, mark))

            _priced = [m for m in cheap_marks if m[2] > 0]
            _all_cheap = bool(_priced) and all(
                m[2] <= cheap_thresh for m in _priced
            ) and len(_priced) == len(short_legs_cb)
            _single_short = len(short_legs_cb) <= 1 and bool(_priced) and (
                _priced[0][2] <= cheap_thresh
            )
            _near_max_profit = (
                gross_credit > 0 and 0 <= liq_premium <= gross_credit * 0.20
            )

            if _all_cheap or _single_short or _near_max_profit:
                _detail = ",".join(
                    f"{t}{s:.0f}={m:.2f}" for s, t, m in cheap_marks
                )
                self.logger.info(
                    f"PRIORITY 5 CHEAP BUYBACK: shorts [{_detail}] "
                    f"liq={liq_premium:.2f} credit={gross_credit:.2f}"
                )
                return "CLOSE_TARGET", EXIT_PRIORITY_CHEAP_BUYBACK, {
                    "reason_detail": f"cheap_buyback[{_detail}]",
                    "liquidation_premium": liq_premium,
                }

        # ── Priority 6: Time-based target ─────────────────────────────────
        if entry_credit > 0:
            if actual_dte == 0:
                time_targets = [
                    (dtime(11, 30), 0.50),
                    (dtime(12, 30), 0.42),
                    (dtime(13, 30), 0.35),
                    (dtime(14, 15), 0.25),
                ]
            else:
                time_targets = [
                    (dtime(12, 0),  0.48),
                    (dtime(13, 0),  0.40),
                    (dtime(14, 0),  0.32),
                ]

            # ── v3.1 [F1]: the ladder was inverted by a min() ──────────
            # These are PREMIUM LEVELS the position must fall BELOW to take
            # profit, so a LOWER number is a HARDER target. Taking
            # min(target_premium, time_target) therefore always selected the
            # harder of the two — and since the stored entry target
            # (credit x (1 - target_pct)) is almost always the lower one, it
            # always won and the entire "accept less profit as the clock runs
            # down" ladder was dead code. Winners were never harvested late:
            # they were carried into the 15:00 hard exit or handed back to a
            # stop. max() restores the intended behaviour — the target LOOSENS
            # with time. This is the highest-impact single change to realised
            # P&L in this patch.
            #
            # [F2] The comparison is made on the LIQUIDATION mark, because a
            # target you can only reach at the mid is not a target.
            _best_target = None
            for time_threshold, target_pct in time_targets:
                if current_time >= time_threshold:
                    time_target = entry_credit * (1.0 - target_pct)
                    _best_target = (
                        time_target if _best_target is None
                        else max(_best_target, time_target)
                    )

            if _best_target is not None:
                effective_target = (
                    max(target_premium, _best_target)
                    if target_premium > 0
                    else _best_target
                )
                if liq_premium <= effective_target:
                    self.logger.info(
                        f"PRIORITY 6 TIME TARGET: {position['strategy_name']} "
                        f"liq={liq_premium:.2f} <= target={effective_target:.2f}"
                    )
                    return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                        "current_premium": current_premium,
                        "liquidation_premium": liq_premium,
                        "time_target": effective_target,
                        "reason_detail": "time_decayed_target_reached",
                    }

            # Also check stored target_premium (set at entry)
            if target_premium > 0 and liq_premium <= target_premium:
                return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                    "current_premium": current_premium,
                    "liquidation_premium": liq_premium,
                    "target_premium": target_premium,
                    "reason_detail": "entry_target_reached",
                }

            # ── v3.1 [F4]: 0DTE gamma-time de-risk ladder ─────────────────
            # After roughly 13:30 on NIFTY expiry day the remaining theta on a
            # short structure is small while gamma is vertical: the position
            # is risking the full width of the wing to earn a handful of
            # residual points. There was no management of that at all — the
            # engine simply held to the 15:00 bell. Professionals flatten into
            # that window. From 13:30 any meaningful profit is taken; from
            # 14:15 anything better than covering the round trip is taken.
            # Losing positions remain governed by the stop logic above.
            if actual_dte == 0 and entry_credit > 0:
                _rt = self._round_trip_cost_pts(open_legs, chain)
                if current_time >= dtime(14, 15):
                    if liq_premium <= entry_credit - _rt:
                        return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                            "current_premium": current_premium,
                            "liquidation_premium": liq_premium,
                            "reason_detail": "gamma_window_scratch_or_better_1415",
                        }
                elif current_time >= dtime(13, 30):
                    if liq_premium <= entry_credit * 0.88 - _rt:
                        return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                            "current_premium": current_premium,
                            "liquidation_premium": liq_premium,
                            "reason_detail": "gamma_window_derisk_1330",
                        }

        # ── Priority 7: Hard exit ─────────────────────────────────────────
        try:
            hard_exit = datetime.strptime(
                position.get("hard_exit_time", "15:00"), "%H:%M"
            ).time()
        except Exception:
            hard_exit = self.config.hard_exit_time

        if current_time >= hard_exit:
            self.logger.info(
                f"PRIORITY 7 HARD EXIT: {position['strategy_name']} "
                f"time={current_time} >= hard_exit={hard_exit}"
            )
            return "HARD_EXIT_15:00", EXIT_PRIORITY_HARD_EXIT, {
                "hard_exit_time": str(hard_exit),
                "current_time": str(current_time),
            }

        return "HOLD", 0, {"current_premium": current_premium}

    def monitor_all_positions(self, signals: dict) -> None:
        """
        Monitor all open positions and execute exits as needed.

        IMPORTANT: ABORT signal only blocks new entries.
        It does NOT close existing positions.
        Existing positions are always managed by their own exit rules.
        """
        # ABORT only blocks new entries — positions continue to be managed
        if signals.get("block_new_entries"):
            self.logger.info(
                "ABORT active — blocking new entries only. "
                "Existing positions managed by own exit rules."
            )


        _open = self._get_open_positions()
        if _open:
            _cur_straddle = float(signals.get("atm_straddle_price") or 0)
            _open_straddle = float(self.market_engine.state.get("_straddle_open_for_regime") or 0)
            if (_cur_straddle > 0 and _open_straddle > 0 and
                    _cur_straddle > _open_straddle * 1.18):
                self.logger.warning(
                    f"STRADDLE EXPLOSION EXIT: straddle {_cur_straddle:.0f} > "
                    f"1.18x opening {_open_straddle:.0f} — closing all positions"
                )
                self.close_all_positions("STRADDLE_EXPLOSION_EXIT")
                return
        for position in self._get_open_positions():
            action, priority, context = self.monitor_position(position, signals)

            if action == "HOLD":
                continue

            if action == "TIGHTEN_STOP":
                self.logger.info(
                    f"Stop tightened for {position['strategy_name']} "
                    f"({position['position_id'][:16]})"
                )
                continue

            if action in (
                "CLOSE_STOP", "CLOSE_TARGET",
                "HARD_EXIT_15:00", "EOD_CLOSE",
                "SHUTDOWN_CLOSE", "STALE_PRIOR_DAY_CLOSE",
            ):
                exit_reason = EXIT_REASON_MAP.get(priority, action)
                if action == "HARD_EXIT_15:00":
                    exit_reason = "HARD_EXIT_15:00"
                elif action in ("EOD_CLOSE", "SHUTDOWN_CLOSE", "STALE_PRIOR_DAY_CLOSE"):
                    exit_reason = action

                self.execute_close(position, exit_reason, priority, context)

    # ─────────────────────────────────────────────────────────────────────
    # EXIT EXECUTION
    # ─────────────────────────────────────────────────────────────────────

    def execute_close(
        self,
        position:    dict,
        reason:      str,
        priority:    int = 0,
        context:     Optional[dict] = None,
    ) -> None:
        """
        Execute the close of all open legs in a position.

        Flow:
        1. Execute SELL legs first (buy back shorts), then BUY legs (sell longs)
        2. Compute exit costs
        3. Compute gross and net P&L
        4. Persist to positions, trade_exits tables
        5. Update session state (daily P&L, capital, consecutive stops)
        6. Log exit quality for calibration feedback
        """
        legs      = self._get_position_legs(position["position_id"])
        open_legs = sorted(
            [l for l in legs if l.get("leg_status") == "OPEN"],
            # Close SELL legs first (buy back shorts to reduce risk)
            key=lambda l: 0 if l["action"] == "SELL" else 1,
        )
        lots  = int(position.get("final_lots", 1) or 1)
        chain = self.market_engine.last_chain

        exit_legs_info: List[dict] = []
        exit_premium = 0.0

        try:
            for leg in open_legs:
                fill = self.executor.execute_leg_exit(leg, chain, lots)
                exit_price = float(fill["fill_price"])

                # Get quoted mid at exit for slippage analysis
                strike   = float(leg.get("strike", 0))
                opt_type = str(leg.get("option_type", ""))
                opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
                bid      = float(opt.get("bid", 0) or 0)
                ask      = float(opt.get("ask", 0) or 0)
                quoted_mid_exit = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else exit_price

                self.db.update(
                    "position_legs",
                    {
                        "exit_price":           exit_price,
                        "leg_status":           "CLOSED",
                        "broker_order_id_exit": fill["order_id"],
                        "quoted_mid_at_exit":   quoted_mid_exit,
                        "exit_delta":           float(opt.get("delta", 0) or 0),
                    },
                    {"leg_id": leg["leg_id"]},
                )

                # v7: the fill alone is not an audit. quoted_mid_exit is
                # already computed above and written to position_legs, but it
                # never made it into this dict, so the exit_slippage sum below
                # filtered every leg out and trade_exits booked 0.0 slippage
                # for every close the system ever made - the one number that
                # says whether the exits are being taken at fair value was
                # permanently zero.
                exit_legs_info.append({
                    **leg,
                    "exit_price":         exit_price,
                    "quoted_mid_at_exit": quoted_mid_exit,
                    "exit_delta":         float(opt.get("delta", 0) or 0),
                    "fill":               fill,
                })

                # Accumulate exit premium
                # For SELL legs: we pay to close (cost)
                # For BUY legs: we receive to close (income)
                if leg["action"] == "SELL":
                    exit_premium += exit_price
                else:
                    exit_premium -= exit_price

        except Exception as e:
            self.logger.critical(
                f"EXIT EXECUTION FAILED for {position['position_id']}: {e}. "
                f"MANUAL INTERVENTION MAY BE REQUIRED."
            )
            return

        # ── Compute P&L ───────────────────────────────────────────────────
        C02          = self.config.lot_size
        entry_credit = float(position.get("entry_credit") or 0)

        # v7: settle on the FILLS, not on the plan.
        #
        # positions.entry_credit is the strategy engine's planned NET credit:
        # gross credit minus an ESTIMATED slippage and the entry charges
        # converted to points (StrategyEngine: net_credit = gross_credit -
        # total_slippage - entry_costs_pts). Using it here did two wrong
        # things at once - it booked a modelled slippage as though it had
        # happened, and it removed the entry charges in points before the
        # lines below removed them again in rupees, because total_costs_rs
        # includes entry_costs_rupees.
        #
        # Measured on the 2026-09-11 paper book: BULL_PUT_SPREAD 2 lots,
        # planned credit 18.58 pts, fills 47.65 / 28.60 = 19.05 pts, exit
        # 15.50 pts, entry charges Rs 57.67, exit charges Rs 56.05.
        #   booked : (18.58 - 15.50) x 130 - 113.72 = Rs 286.68
        #   correct: (19.05 - 15.50) x 130 - 113.72 = Rs 347.78
        # Rs 61.10 of a real profit never existed - Rs 57.67 charged twice
        # and Rs 3.38 of estimated slippage charged as real. The day was
        # reported Rs 125 (10%) worse than the fills say, and since the
        # replay harness settles on filled prices, the live book and the
        # backtest could never be reconciled on the same trade.
        #
        # The exit ladder is untouched: stop_premium, target_premium and the
        # profit lock keep comparing against the stored entry_credit exactly
        # as before, so no exit decision changes. Only the money that is
        # booked, reported and fed to calibration becomes the money that was
        # actually made.
        realised_credit, credit_basis = realised_entry_credit(position, legs)
        if abs(realised_credit - entry_credit) > 1e-9:
            self.logger.info(
                f"entry credit settled on {credit_basis}: "
                f"planned {entry_credit:+.3f} pts vs realised "
                f"{realised_credit:+.3f} pts "
                f"({(realised_credit - entry_credit) * C02 * lots:+,.2f} Rs "
                f"on {lots} lot(s))"
            )

        # Gross P&L = realised entry credit - what it cost to close
        gross_pnl_pts = realised_credit - exit_premium
        gross_pnl_rs  = gross_pnl_pts * C02 * lots

        # Costs
        exit_costs_dict    = self._compute_transaction_costs(exit_legs_info, lots, "EXIT")
        exit_costs_rs      = exit_costs_dict["total_rupees"]
        entry_costs_rs     = float(position.get("entry_costs_rupees") or 0)
        total_costs_rs     = entry_costs_rs + exit_costs_rs

        # Net P&L
        net_pnl_rs         = gross_pnl_rs - total_costs_rs
        net_pnl_pts        = net_pnl_rs / (C02 * lots) if (C02 * lots) > 0 else 0.0
        current_capital    = float(self.market_engine.state.get("current_capital",
                                                                  self.config.starting_capital) or 0)
        net_pnl_pct        = (net_pnl_rs / current_capital * 100.0) if current_capital else 0.0
        result             = "WIN" if net_pnl_rs > 0 else ("LOSS" if net_pnl_rs < 0 else "BREAKEVEN")
        credit_or_debit    = entry_credit or 1.0
        profit_pct_credit  = (net_pnl_pts / credit_or_debit * 100.0) if credit_or_debit else 0.0

        # Hold time
        now        = now_ist()
        entry_time = datetime.fromisoformat(position["entry_time"]) if position.get("entry_time") else now
        hold_minutes = (now - entry_time).total_seconds() / 60.0

        # ── Update position ───────────────────────────────────────────────
        _close_update = {
            "status":            "CLOSED",
            "exit_time":         now.isoformat(),
            "exit_reason":       reason,
            "exit_priority":     priority,
            "exit_premium":      exit_premium,
            "gross_pnl_rupees":  gross_pnl_rs,
            "exit_costs_rupees": exit_costs_rs,
            "net_pnl_rupees":    net_pnl_rs,
            # v7: kept next to the planned figure in entry_credit so the two
            # can be compared on any closed trade, forever.
            "entry_credit_realised": realised_credit,
            "updated_at":        now.isoformat(),
        }
        try:
            self.db.update(
                "positions", _close_update,
                {"position_id": position["position_id"]},
            )
        except Exception as _cue:
            # An audit column that could not be added must never cost the
            # close itself: retry without it and say so.
            self.logger.warning(
                f"positions close update failed ({_cue}); retrying without "
                f"entry_credit_realised"
            )
            _close_update.pop("entry_credit_realised", None)
            self.db.update(
                "positions", _close_update,
                {"position_id": position["position_id"]},
            )

        # ── Persist trade exit ────────────────────────────────────────────
        priority_name = EXIT_PRIORITY_NAMES.get(priority, reason)
        try:
            self.db.insert("trade_exits", {
                "trade_id":             position["position_id"],
                "position_id":          position["position_id"],
                "strategy_name":        position["strategy_name"],
                "exit_time":            now.isoformat(),
                "hold_minutes":         hold_minutes,
                "exit_reason":          reason,
                "exit_priority":        priority,
                "exit_priority_name":   priority_name,
                "exit_spot":            self.market_engine.state.get("prev_spot"),
                "exit_vix":             self.market_engine.state.get("prev_vix"),
                "exit_adx":             (context or {}).get("adx"),
                "exit_vwap_dist":       (context or {}).get("vwap_dist"),
                "exit_legs_json":       json.dumps(exit_legs_info, default=str),
                "exit_premium":         exit_premium,
                "gross_pnl_pts":        gross_pnl_pts,
                "gross_pnl_rupees":     gross_pnl_rs,
                "exit_slippage":        round(sum(
                    abs(float(l.get("exit_price") or 0) - float(l.get("quoted_mid_at_exit") or l.get("exit_price") or 0))
                    for l in exit_legs_info
                    if l.get("exit_price") and l.get("quoted_mid_at_exit")
                ), 3),
                "exit_costs_pts":       exit_costs_rs / C02 if C02 else None,
                "exit_costs_rupees":    exit_costs_rs,
                "total_costs_rupees":   total_costs_rs,
                "net_pnl_pts":          net_pnl_pts,
                "net_pnl_rupees":       net_pnl_rs,
                "net_pnl_pct":          net_pnl_pct,
                "result":               result,
                "profit_pct_of_credit": profit_pct_credit,
                "pnl_15min_after_exit": None,  # Filled by calibration engine EOD
                "created_at":           now.isoformat(),
            })
        except Exception as e:
            self.logger.warning(f"trade_exits insert error: {e}")

        # ── Log exit quality for calibration feedback ─────────────────────
        try:
            self.cal_engine.log_exit_quality(
                position_id=position["position_id"],
                trading_date=today_ist().isoformat(),
                exit_priority=priority,
                exit_time=now.isoformat(),
                exit_pnl_rupees=net_pnl_rs,
            )
        except Exception as e:
            self.logger.debug(f"Exit quality log error: {e}")

        # ── Update session state ──────────────────────────────────────────
        self._update_state_after_close(reason, net_pnl_rs, priority)

        print_section(f"POSITION CLOSED: {position['strategy_name']} — {reason}")
        print_kv_table({
            "Position ID":     position["position_id"][:16] + "...",
            "Hold Time (min)": f"{hold_minutes:.1f}",
            "Exit Reason":     reason,
            "Exit Priority":   f"{priority} ({priority_name})",
            "Exit Premium":    f"{exit_premium:.2f}pts",
            "Entry Credit":    f"{realised_credit:.2f}pts realised "
                               f"({credit_basis}) vs {entry_credit:.2f}pts "
                               f"planned",
            "Gross P&L (Rs)":  f"{gross_pnl_rs:,.0f}",
            "Total Costs (Rs)":f"{total_costs_rs:,.0f}",
            "Net P&L (Rs)":    f"{net_pnl_rs:,.0f}",
            "Net P&L (%)":     f"{net_pnl_pct:.3f}%",
            "Result":          result,
        })
        self.logger.info(
            f"POSITION CLOSED: {position['strategy_name']} "
            f"reason={reason} priority={priority} "
            f"net_pnl=Rs{net_pnl_rs:.2f} result={result} "
            f"hold={hold_minutes:.1f}min"
        )

    def _update_state_after_close(
        self, reason: str, net_pnl_rs: float, priority: int
    ) -> None:
        """
        Update session state after a position close.
        Handles: daily P&L, capital, consecutive stops, daily halt check.
        """
        state = self.market_engine.state

        state["daily_pnl"]       = float(state.get("daily_pnl", 0.0) or 0.0) + net_pnl_rs
        state["current_capital"] = float(state.get("current_capital",
                                                     self.config.starting_capital) or 0) + net_pnl_rs

        # Consecutive stops tracking
        if reason == "CLOSE_STOP" or priority in (
            EXIT_PRIORITY_DELTA_BREACH,
            EXIT_PRIORITY_SPOT_PROXIMITY,
            EXIT_PRIORITY_PRICE_STOP,
        ):
            state["last_stop_time"]   = now_ist().isoformat()
            state["last_stop_reason"] = reason
            state["consecutive_stops"] = int(state.get("consecutive_stops", 0) or 0) + 1

            # Record the signal combo that caused the stop
            open_pos = self._get_open_positions()
            if not open_pos:
                sig   = self.market_engine.state
                combo = (
                    f"{sig.get('vol_regime', '')}_"
                    f"{sig.get('price_regime', '')}_"
                    f"{sig.get('direction', '')}"
                )
                state["last_stop_signal_combo"] = combo

            # Halt trading after 2 consecutive stops
            if int(state.get("consecutive_stops", 0) or 0) >= 2:
                state["daily_halted"] = True
                self.logger.warning(
                    "2 consecutive stops — halting trading for the day"
                )

        elif reason in (
            "CLOSE_TARGET", "HARD_EXIT_15:00", "EOD_CLOSE",
            "SHUTDOWN_CLOSE", "STALE_PRIOR_DAY_CLOSE",
        ):
            # Winning exits reset consecutive stops
            if net_pnl_rs > 0:
                state["consecutive_stops"] = 0

        # Daily loss limit check
        # v7: measured against capital AT THE START OF THE DAY, the basis
        # main.check_daily_loss_halt() and the replay harness both use. The
        # denominator here was the post-loss capital, which made one
        # configured limit mean two different things inside a single process:
        # the deeper the loss, the smaller this denominator, so this copy of
        # the check tripped earlier than the one that pages the operator and
        # flattens the book - and it tripped silently, with no alert and no
        # risk_halt row to explain why entries had stopped.
        current_cap = float(state.get("current_capital", self.config.starting_capital) or 0)
        daily_pnl   = float(state.get("daily_pnl", 0.0) or 0.0)
        day_start_cap = current_cap - daily_pnl
        if day_start_cap <= 0:
            day_start_cap = current_cap
        if day_start_cap > 0:
            daily_loss_pct = max(0.0, -daily_pnl) / day_start_cap
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                state["daily_halted"] = True
                self.logger.warning(
                    f"DAILY LOSS LIMIT: {daily_loss_pct*100:.2f}% — halting trading"
                )

        # Persist state
        self.db.update(
            "session_state",
            {
                "daily_pnl":       state["daily_pnl"],
                "current_capital": state["current_capital"],
                "daily_halted":    int(bool(state.get("daily_halted", False))),
                "consecutive_stops": int(state.get("consecutive_stops", 0) or 0),
            },
            {"trading_date": today_ist().isoformat()},
        )
        self.market_engine._save_session_state()

    def close_all_positions(self, reason: str, force: bool = False) -> None:
        """Close all open positions with the given reason.

        force=True is the kill-switch contract: a position that is still open
        after the first pass is retried, because every live path that lands
        here (hard-exit sweep, EOD, watchdog, risk halt) is a path where being
        flat matters more than the price. Without it the behaviour is exactly
        what it always was.
        """
        open_positions = self._get_open_positions()
        if not open_positions:
            return

        ids = [p["position_id"] for p in open_positions]
        self.logger.info(
            f"Closing all {len(open_positions)} open position(s): {reason}"
        )
        for position in open_positions:
            try:
                self.execute_close(position, reason, 0, {})
            except Exception as e:
                # One row whose bookkeeping failed must not strand the rest of
                # the book: the sweep exists to get everything flat.
                self.logger.critical(
                    f"close failed for {position['position_id']} ({reason}): {e}"
                )

        if not force or self.config.paper_trade_mode:
            return

        remaining = self._still_open_positions(ids)
        attempts  = 1
        while remaining and attempts < 3:
            attempts += 1
            self._alert(
                "CRITICAL",
                f"{len(remaining)} position(s) still open after {reason} "
                f"(pass {attempts - 1}): {', '.join(p['position_id'] for p in remaining)}"
                f" — retrying with escalated exit pricing",
            )
            for position in remaining:
                try:
                    self.execute_close(position, f"{reason}_RETRY{attempts - 1}", 7, {})
                except Exception as e:
                    self.logger.critical(
                        f"flatten pass {attempts} failed for "
                        f"{position['position_id']}: {e}"
                    )
            remaining = self._still_open_positions(ids)

        if not remaining:
            self.logger.info(f"flatten confirmed: {len(ids)} position(s) closed")
            return

        self._report_unflattened(remaining, reason)

    def _still_open_positions(self, position_ids: List[str]) -> List[dict]:
        if not position_ids:
            return []
        marks = ",".join("?" * len(position_ids))
        try:
            return self.db.query(
                f"SELECT * FROM positions WHERE status='OPEN' "
                f"AND position_id IN ({marks})",
                tuple(position_ids),
            )
        except Exception as e:
            self.logger.warning(f"flatten verification query failed: {e}")
            return []

    def _report_unflattened(self, positions: List[dict], reason: str) -> None:
        """Tell the operator what the broker still holds, and what it costs.

        The rows are deliberately left OPEN: closing them locally would book a
        fabricated P&L and hide a live exposure. The next cycle retries, and
        Upstox squares the position off itself at 15:20 with the intraday
        penalty, so the alert is the only thing standing between a stuck order
        and an unplanned overnight-ish risk.
        """
        # position_legs carries no instrument key, so the broker side is
        # reported as a count rather than matched leg by leg: enough for the
        # operator to tell "still held" from "already flat, bookkeeping stale".
        broker_open: Optional[int] = None
        try:
            rows = self.client.get_positions() or []
            broker_open = 0
            for row in rows:
                try:
                    if float(row.get("quantity") or 0.0) != 0.0:
                        broker_open += 1
                except (TypeError, ValueError):
                    broker_open += 1
        except Exception as e:
            self.logger.warning(f"broker position reconcile failed: {e}")

        detail = []
        for position in positions:
            legs = [
                l for l in self._get_position_legs(position["position_id"])
                if l.get("leg_status") == "OPEN"
            ]
            leg_txt = ",".join(
                f"{l.get('action')} {l.get('strike')}{str(l.get('option_type', '')).upper()}"
                for l in legs
            )
            detail.append(f"{position['position_id']} [{leg_txt}]")

        broker_txt = (
            f"; broker reports {broker_open} non-zero F&O position(s)"
            if broker_open is not None else "; broker state unavailable"
        )
        msg = (
            f"{len(positions)} position(s) NOT flattened by '{reason}': "
            + "; ".join(detail)
            + broker_txt
            + " — rows left OPEN so P&L is not fabricated; MANUAL FLATTEN"
            " REQUIRED (Upstox auto-squares-off intraday F&O at 15:20 with a"
            " per-order penalty)"
        )
        if broker_open == 0:
            msg = (
                f"{len(positions)} position(s) still OPEN locally after "
                f"'{reason}' while the broker reports none: "
                + "; ".join(detail)
                + " — exits likely filled after their order query failed; "
                "reconcile the book by hand (no automatic close, so P&L stays "
                "truthful)"
            )
        self._alert("CRITICAL", msg)

        if bool(getattr(self.config, "exit_all_positions_fallback", False)):
            self._exit_all_positions_fallback(reason)

    def _exit_all_positions_fallback(self, reason: str) -> None:
        """Last resort: the broker's own flatten.

        Exit-All-Positions sweeps the whole NSE_FO segment, so it is opt-in and
        only ever reached after the engine's own priced exits failed.
        """
        self._alert(
            "CRITICAL",
            f"invoking broker Exit-All-Positions for NSE_FO after '{reason}' — "
            "this flattens every intraday F&O order in the account, not just "
            "this strategy's",
        )
        try:
            self.client.exit_all_positions(segment="NSE_FO")
        except Exception as e:
            self._alert(
                "CRITICAL",
                f"broker Exit-All-Positions failed after '{reason}': {e} — "
                "flatten by hand immediately",
            )
            return
        time_module.sleep(2.0)
        self.logger.critical(
            "broker Exit-All-Positions dispatched; open rows stay OPEN until a "
            "cycle reconciles them"
        )

    def perform_hard_exit_sweep(self) -> None:
        """
        Perform hard exit sweep at 15:00.
        Closes all open positions regardless of P&L.
        Called from main.py every cycle.

        The trigger is the earlier of 15:00 and HARD_EXIT_TIME so tightening
        the configured time cannot silently arrive after the broker's own
        square-off; the sweep forces confirmation because a LIMIT that did not
        fill at 15:00 must not be left sitting there.
        """
        current_time = now_ist().time()
        trigger      = dtime(15, 0)
        try:
            configured = self.config.hard_exit_time
            if isinstance(configured, dtime):
                trigger = min(trigger, configured)
        except Exception:
            pass
        if current_time >= trigger:
            open_positions = self._get_open_positions()
            if open_positions:
                self.logger.info(
                    f"HARD EXIT SWEEP @ {trigger.strftime('%H:%M')} — "
                    f"closing {len(open_positions)} position(s)"
                )
                self.close_all_positions("HARD_EXIT_15:00", force=True)

    def flatten_now(self, reason: str) -> None:
        """Kill-switch entry point: cancel resting orders, then flatten."""
        if not self.config.paper_trade_mode:
            try:
                result = self.client.cancel_all_open_orders()
                state = str((result or {}).get("status") or "cancelled")
                self.logger.warning(f"cancel-all before flatten: {state}")
            except Exception as e:
                self.logger.warning(
                    f"cancel-all before flatten failed (continuing): {e}"
                )
        self.close_all_positions(reason, force=True)



# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    import tempfile as _tf3
    from core import load_env_file, ENV_FILE, BASE_DIR
    _env3 = load_env_file(ENV_FILE)
    _prod3 = str(BASE_DIR / _env3.get("DB_PATH", "data/nifty_algo_v3.db"))
    print_section("NIFTY ALGO v3.0 — EXECUTION ENGINE SELF-TEST", char="#")

    from core import load_config, Database, RateLimiter, UpstoxClient, setup_logging

    config        = load_config()
    # v3.8: isolate this self-test from the live production
    # database. The State Update Tests below drive
    # _update_state_after_close through a win then two stops, and
    # that method PERSISTS daily_pnl, consecutive_stops and
    # daily_halted into session_state; its "reset" block at the
    # end only clears the in-memory dict. Pointing those writes at
    # config.db_path left the live book with a fabricated halt
    # (daily_halted=1, consecutive_stops=2, daily_pnl=-1000,
    # last_stop_reason=CLOSE_STOP) and zero matching trades. A
    # test must never write to production. config is left as it
    # is; only the scratch Database is handed in.
    from pathlib import Path as _scratch_path
    db = Database(_scratch_path(_tf3.mkdtemp(
        prefix="exec_selftest_")) / "exec_selftest.db")

    logger        = setup_logging(db, config.log_dir)
    rate_limiter  = RateLimiter(config.rate_limits)
    client        = UpstoxClient(config, rate_limiter, db, logger)
    market_engine = MarketDataEngine(config, db, client, rate_limiter, logger)
    cal_engine    = CalibrationEngine(db, config, logger)

    engine = ExecutionEngine(config, db, market_engine, cal_engine, client, logger)

    # ── Test 1: Transaction cost computation ─────────────────────────────
    print_section("Transaction Cost Tests")

    # 4-leg Iron Condor, 2 lots
    ic_legs = [
        {"action": "SELL", "option_type": "call", "exec_price": 45.0},
        {"action": "SELL", "option_type": "put",  "exec_price": 42.0},
        {"action": "BUY",  "option_type": "call", "exec_price": 12.0},
        {"action": "BUY",  "option_type": "put",  "exec_price": 11.0},
    ]

    costs_ic = engine._compute_transaction_costs(ic_legs, 2, "ENTRY")
    print(f"  Iron Condor 2 lots: total=Rs{costs_ic['total_rupees']:.2f}")
    print(f"  Breakdown: {costs_ic['breakdown']}")
    assert costs_ic["total_rupees"] > 0, "Costs should be positive"
    assert "stt" in costs_ic["breakdown"], "Should have STT"
    assert "brokerage" in costs_ic["breakdown"], "Should have brokerage"

    # Verify STT is only on sell side
    sell_value = (45.0 + 42.0) * 2 * config.lot_size
    expected_stt = sell_value * config.stt_options_sell
    assert abs(costs_ic["breakdown"]["stt"] - expected_stt) < 0.01, \
        f"STT should be {expected_stt:.2f}, got {costs_ic['breakdown']['stt']:.2f}"

    # 2-leg Bull Put Spread, 1 lot
    bps_legs = [
        {"action": "SELL", "option_type": "put", "exec_price": 35.0},
        {"action": "BUY",  "option_type": "put", "exec_price": 12.0},
    ]
    costs_bps = engine._compute_transaction_costs(bps_legs, 1, "ENTRY")
    print(f"  Bull Put Spread 1 lot: total=Rs{costs_bps['total_rupees']:.2f}")
    assert costs_bps["total_rupees"] > 0, "BPS costs should be positive"
    assert costs_bps["total_rupees"] < costs_ic["total_rupees"], \
        "2-leg should cost less than 4-leg"

    # Cost per point check
    cost_pts_ic  = costs_ic["total_rupees"] / config.lot_size
    cost_pts_bps = costs_bps["total_rupees"] / config.lot_size
    print(f"  IC cost per point: {cost_pts_ic:.4f}pts")
    print(f"  BPS cost per point: {cost_pts_bps:.4f}pts")
    assert 0.5 < cost_pts_ic < 15.0, f"IC cost/pt should be reasonable, got {cost_pts_ic:.4f}"

    print("  [OK] Transaction cost tests passed")

    # ── Test 2: Mark price computation ───────────────────────────────────
    print_section("Mark Price Tests")

    mock_chain = {
        24000.0: {
            "call": {"bid": 44.0, "ask": 46.0, "ltp": 45.0, "delta": 0.50},
            "put":  {"bid": 41.0, "ask": 43.0, "ltp": 42.0, "delta": -0.50},
        },
        24100.0: {
            "call": {"bid": 0, "ask": 0, "ltp": 20.0, "delta": 0.30},
            "put":  {"bid": 0, "ask": 0, "ltp": 0, "delta": -0.30},
        },
    }

    # With bid/ask: should use mid
    leg1 = {"strike": 24000.0, "option_type": "call", "entry_price": 45.0}
    mark1 = engine._get_mark_price(leg1, mock_chain)
    print(f"  ATM call with bid/ask: mark={mark1:.2f} (expect 45.00)")
    assert abs(mark1 - 45.0) < 0.01, f"Expected 45.00, got {mark1}"

    # With LTP only: should use LTP
    leg2 = {"strike": 24100.0, "option_type": "call", "entry_price": 20.0}
    mark2 = engine._get_mark_price(leg2, mock_chain)
    print(f"  OTM call with LTP only: mark={mark2:.2f} (expect 20.00)")
    assert abs(mark2 - 20.0) < 0.01, f"Expected 20.00, got {mark2}"

    # No chain data: should use entry price
    leg3 = {"strike": 24000.0, "option_type": "call", "entry_price": 45.0}
    mark3 = engine._get_mark_price(leg3, {})
    print(f"  No chain data: mark={mark3:.2f} (expect 45.00 from entry_price)")
    assert abs(mark3 - 45.0) < 0.01, f"Expected 45.00, got {mark3}"

    print("  [OK] Mark price tests passed")

    # ── Test 3: Current premium computation ──────────────────────────────
    print_section("Current Premium Tests")

    # Iron Condor: short call + short put - long call - long put
    ic_open_legs = [
        {"leg_status": "OPEN", "action": "SELL", "option_type": "call",
         "strike": 24000.0, "entry_price": 45.0},
        {"leg_status": "OPEN", "action": "SELL", "option_type": "put",
         "strike": 24000.0, "entry_price": 42.0},
        {"leg_status": "OPEN", "action": "BUY",  "option_type": "call",
         "strike": 24100.0, "entry_price": 12.0},
        {"leg_status": "OPEN", "action": "BUY",  "option_type": "put",
         "strike": 24100.0, "entry_price": 0.0},
    ]

    # Add 24100 put to chain
    mock_chain[24100.0]["put"] = {"bid": 9.0, "ask": 11.0, "ltp": 10.0}

    premium = engine._compute_current_premium(ic_open_legs, mock_chain)
    # Expected: sell_call(45) + sell_put(42) - buy_call(20) - buy_put(10) = 57
    print(f"  IC current premium: {premium:.2f} (expect ~57.00)")
    assert 50 < premium < 65, f"IC premium should be ~57, got {premium:.2f}"

    # Closed leg should not contribute
    ic_legs_with_closed = ic_open_legs + [
        {"leg_status": "CLOSED", "action": "SELL", "option_type": "call",
         "strike": 24000.0, "entry_price": 45.0}
    ]
    premium2 = engine._compute_current_premium(ic_legs_with_closed, mock_chain)
    assert abs(premium2 - premium) < 0.01, "Closed leg should not affect premium"

    print("  [OK] Current premium tests passed")

    # ── Test 4: Exit priority system ─────────────────────────────────────
    print_section("Exit Priority System Tests")

    # Set up market engine state
    market_engine.state["prev_spot"] = 24000.0
    market_engine.state["prev_vix"]  = 11.5
    market_engine.last_chain         = mock_chain
    market_engine.last_chain_expiry  = today_ist()

    # Build a mock position
    def make_position(**overrides) -> dict:
        base = {
            "position_id":              "test-pos-001",
            "trading_date":             today_ist().isoformat(),
            "strategy_name":            "IRON_CONDOR",
            "strategy_type":            "SELL",
            "target_expiry":            today_ist().isoformat(),
            "actual_dte":               0,
            "entry_time":               now_ist().isoformat(),
            "entry_spot":               24000.0,
            "entry_credit":             35.0,
            "gross_credit":             38.0,
            "opening_straddle_at_entry":175.0,
            "stop_premium":             52.5,  # 1.5× credit
            "target_premium":           19.25,  # 45% of credit
            "price_stop_pts":           52,
            "price_stop_level_call":    24200.0,  # short_call - 52pts
            "price_stop_level_put":     23800.0,  # short_put + 52pts
            "hard_exit_time":           "15:00",
            "final_lots":               2,
            "max_loss_per_lot":         3000.0,
            "entry_costs_rupees":       520.0,
            "last_known_premium":       30.0,
            "profit_lock_activated":    0,
            "profit_lock_stop_level":   None,
            "status":                   "OPEN",
        }
        base.update(overrides)
        return base

    mock_signals = {
        "spot": 24000.0,
        "vix":  11.5,
        "adx_15": 14.0,
        "vol_regime": "SELL_PREMIUM",
        "price_regime": "RANGE",
    }

    # Mock legs for monitoring
    mock_open_legs = [
        {"leg_id": 1, "leg_status": "OPEN", "action": "SELL",
         "option_type": "call", "strike": 24150.0, "entry_price": 22.0,
         "entry_delta": 0.25},
        {"leg_id": 2, "leg_status": "OPEN", "action": "SELL",
         "option_type": "put",  "strike": 23850.0, "entry_price": 20.0,
         "entry_delta": -0.25},
        {"leg_id": 3, "leg_status": "OPEN", "action": "BUY",
         "option_type": "call", "strike": 24300.0, "entry_price": 8.0,
         "entry_delta": 0.10},
        {"leg_id": 4, "leg_status": "OPEN", "action": "BUY",
         "option_type": "put",  "strike": 23700.0, "entry_price": 7.0,
         "entry_delta": -0.10},
    ]

    # Add these strikes to mock chain
    for strike in [24150.0, 23850.0, 24300.0, 23700.0]:
        mock_chain[strike] = {
            "call": {"bid": 10.0, "ask": 12.0, "ltp": 11.0, "delta": 0.20, "oi": 5000},
            "put":  {"bid": 9.0,  "ask": 11.0, "ltp": 10.0, "delta": -0.20, "oi": 5000},
        }

    # Test Priority 1: Delta breach
    # Modify chain to show a short-call delta clearly above the expiry-day
    # close threshold. v3.9 raised delta_close_dte0 to 0.45 (the engine now
    # sells 0.32/0.30/0.28 delta, so the old 0.35 line sat below the entry
    # delta of a 0.30-delta short and self-closed it on entry), which made a
    # hardcoded 0.45 test value a non-breach. Derive the test delta from the
    # live threshold so this assertion stays valid if the knob is retuned.
    _dte0_close_p1 = float(getattr(config, "delta_close_dte0", 0.45))
    mock_chain[24150.0]["call"]["delta"] = round(min(_dte0_close_p1 + 0.15, 0.99), 2)

    # We need to mock _get_position_legs
    original_get_legs = engine._get_position_legs

    def mock_get_legs(position_id):
        return mock_open_legs

    engine._get_position_legs = mock_get_legs

    pos = make_position()
    action1, priority1, ctx1 = engine.monitor_position(pos, mock_signals)
    print(f"  Priority 1 (delta breach): action={action1} priority={priority1}")
    assert action1 == "CLOSE_STOP", f"Expected CLOSE_STOP for delta breach, got {action1}"
    assert priority1 == EXIT_PRIORITY_DELTA_BREACH, \
        f"Expected priority {EXIT_PRIORITY_DELTA_BREACH}, got {priority1}"

    # Reset delta
    mock_chain[24150.0]["call"]["delta"] = 0.25

    # Test Priority 2: Spot proximity
    mock_signals_prox = {**mock_signals, "spot": 24115.0}  # Within 40pts of 24150
    action2, priority2, ctx2 = engine.monitor_position(pos, mock_signals_prox)
    print(f"  Priority 2 (spot proximity): action={action2} priority={priority2}")
    assert action2 == "CLOSE_STOP", f"Expected CLOSE_STOP for spot proximity, got {action2}"
    assert priority2 == EXIT_PRIORITY_SPOT_PROXIMITY, \
        f"Expected priority {EXIT_PRIORITY_SPOT_PROXIMITY}, got {priority2}"

    # Test Priority 3: Price stop
    pos_price_stop = make_position(price_stop_level_call=24050.0)  # Below current spot
    mock_signals_ps = {**mock_signals, "spot": 24060.0}  # Above price_stop_call
    action3, priority3, ctx3 = engine.monitor_position(pos_price_stop, mock_signals_ps)
    print(f"  Priority 3 (price stop): action={action3} priority={priority3}")
    assert action3 == "CLOSE_STOP", f"Expected CLOSE_STOP for price stop, got {action3}"
    assert priority3 == EXIT_PRIORITY_PRICE_STOP, \
        f"Expected priority {EXIT_PRIORITY_PRICE_STOP}, got {priority3}"

    # Test Priority 7: Hard exit (time-based)
    # We can't easily mock time, but we can test the logic
    pos_hard = make_position(
        hard_exit_time="00:01",
        price_stop_level_call=99999.0,
        price_stop_level_put=0.0,
        stop_premium=9999.0,
        target_premium=0.0,
        profit_lock_activated=1,
        profit_lock_stop_level=None,
        entry_credit=0.0,
        gross_credit=0.0,
    )
    action7, priority7, ctx7 = engine.monitor_position(pos_hard, mock_signals)
    print(f"  Priority 7 (hard exit): action={action7} priority={priority7}")
    assert action7 == "HARD_EXIT_15:00", f"Expected HARD_EXIT_15:00, got {action7}"
    assert priority7 == EXIT_PRIORITY_HARD_EXIT, \
        f"Expected priority {EXIT_PRIORITY_HARD_EXIT}, got {priority7}"

    # Test HOLD: all conditions normal
    pos_hold = make_position(
        price_stop_level_call=24500.0,
        price_stop_level_put=23500.0,
        hard_exit_time="23:59",
        profit_lock_activated=1,
        profit_lock_stop_level=None,
        entry_credit=0.0,
        gross_credit=0.0,
        stop_premium=9999.0,
        target_premium=0.0,
    )
    action_hold, priority_hold, ctx_hold = engine.monitor_position(pos_hold, mock_signals)
    print(f"  HOLD (normal conditions): action={action_hold}")
    assert action_hold == "HOLD", f"Expected HOLD for normal conditions, got {action_hold}"

    # Restore original method
    engine._get_position_legs = original_get_legs

    print("  [OK] Exit priority system tests passed")

    # ── Test 5: P&L computation ───────────────────────────────────────────
    print_section("P&L Computation Tests")

    C02 = config.lot_size
    lots = 2

    # Winning trade: collected 35pts, exit at 15pts
    entry_credit_test = 35.0
    exit_premium_test = 15.0
    gross_pnl_pts = entry_credit_test - exit_premium_test  # 20pts
    gross_pnl_rs  = gross_pnl_pts * C02 * lots
    print(f"  Winning trade: gross_pnl={gross_pnl_pts:.1f}pts = Rs{gross_pnl_rs:,.0f}")
    assert gross_pnl_pts == 20.0, f"Expected 20pts, got {gross_pnl_pts}"
    assert gross_pnl_rs == 20.0 * C02 * lots, f"Gross P&L Rs mismatch"

    # Losing trade: collected 35pts, exit at 55pts
    exit_premium_loss = 55.0
    gross_pnl_loss_pts = entry_credit_test - exit_premium_loss  # -20pts
    gross_pnl_loss_rs  = gross_pnl_loss_pts * C02 * lots
    print(f"  Losing trade: gross_pnl={gross_pnl_loss_pts:.1f}pts = Rs{gross_pnl_loss_rs:,.0f}")
    assert gross_pnl_loss_pts == -20.0, f"Expected -20pts, got {gross_pnl_loss_pts}"
    assert gross_pnl_loss_rs < 0, "Losing trade should have negative P&L"

    # Net P&L after costs
    test_costs = 800.0  # Rs800 round trip
    net_pnl_win  = gross_pnl_rs - test_costs
    net_pnl_loss = gross_pnl_loss_rs - test_costs
    print(f"  Net P&L win: Rs{net_pnl_win:,.0f}")
    print(f"  Net P&L loss: Rs{net_pnl_loss:,.0f}")
    assert net_pnl_win > 0, "Net P&L should be positive for winning trade"
    assert net_pnl_loss < 0, "Net P&L should be negative for losing trade"

    print("  [OK] P&L computation tests passed")

    # ── Test 6: State update after close ─────────────────────────────────
    print_section("State Update Tests")

    # Set initial state
    market_engine.state["daily_pnl"]        = 0.0
    market_engine.state["current_capital"]  = config.starting_capital
    market_engine.state["consecutive_stops"] = 0
    market_engine.state["daily_halted"]     = False

    # Simulate a winning close
    engine._update_state_after_close("CLOSE_TARGET", 5000.0, EXIT_PRIORITY_TIME_TARGET)
    assert market_engine.state["daily_pnl"] == 5000.0, \
        f"Daily P&L should be 5000, got {market_engine.state['daily_pnl']}"
    assert market_engine.state["consecutive_stops"] == 0, \
        "Winning trade should not increment consecutive stops"
    print(f"  After win: daily_pnl={market_engine.state['daily_pnl']:,.0f}")

    # Simulate a stop loss
    engine._update_state_after_close("CLOSE_STOP", -3000.0, EXIT_PRIORITY_PRICE_STOP)
    assert market_engine.state["daily_pnl"] == 2000.0, \
        f"Daily P&L should be 2000, got {market_engine.state['daily_pnl']}"
    assert market_engine.state["consecutive_stops"] == 1, \
        f"Should have 1 consecutive stop, got {market_engine.state['consecutive_stops']}"
    print(f"  After stop: daily_pnl={market_engine.state['daily_pnl']:,.0f} "
          f"consecutive_stops={market_engine.state['consecutive_stops']}")

    # Second stop → should halt
    engine._update_state_after_close("CLOSE_STOP", -3000.0, EXIT_PRIORITY_PRICE_STOP)
    assert market_engine.state["consecutive_stops"] == 2, \
        f"Should have 2 consecutive stops, got {market_engine.state['consecutive_stops']}"
    assert market_engine.state["daily_halted"] == True, \
        "Should be halted after 2 consecutive stops"
    print(f"  After 2nd stop: halted={market_engine.state['daily_halted']}")

    # Reset state
    market_engine.state["daily_pnl"]        = 0.0
    market_engine.state["current_capital"]  = config.starting_capital
    market_engine.state["consecutive_stops"] = 0
    market_engine.state["daily_halted"]     = False

    print("  [OK] State update tests passed")

    # ── Test 7: Paper order executor ─────────────────────────────────────
    print_section("Paper Order Executor Tests")

    paper_exec = PaperOrderExecutor(config, logger)

    # Entry fill
    test_leg = {
        "action": "SELL", "option_type": "call", "strike": 24150.0,
        "exec_price": 22.0, "bid": 21.5, "ask": 22.5,
    }
    fill1 = paper_exec.execute_leg_entry(test_leg, 2, mock_chain)
    print(f"  Paper entry fill: {fill1}")
    assert fill1["status"] == "FILLED", "Paper entry should be FILLED"
    assert fill1["fill_price"] == 22.0, f"Fill price should be exec_price 22.0, got {fill1['fill_price']}"
    assert fill1["order_id"].startswith("PAPER-"), "Order ID should start with PAPER-"

    # Exit fill (buying back a short)
    test_exit_leg = {
        "action": "SELL", "option_type": "call", "strike": 24150.0,
        "entry_price": 22.0,
    }
    fill2 = paper_exec.execute_leg_exit(test_exit_leg, mock_chain, 2)
    print(f"  Paper exit fill: {fill2}")
    assert fill2["status"] == "FILLED", "Paper exit should be FILLED"
    # For SELL leg exit: should use ask price from chain
    expected_exit = mock_chain[24150.0]["call"]["ask"]
    assert abs(fill2["fill_price"] - expected_exit) < 0.01, \
        f"Exit fill should be ask {expected_exit}, got {fill2['fill_price']}"

    print("  [OK] Paper order executor tests passed")

    # ── Test 8: Pre-trade validation ─────────────────────────────────────
    print_section("Pre-Trade Validation Tests")

    # Set up valid state
    market_engine.state["daily_pnl"]       = 0.0
    market_engine.state["current_capital"] = config.starting_capital
    market_engine.state["entry_start"]     = "09:45"
    market_engine.state["entry_end"]       = "14:00"
    market_engine.state["hard_exit_time"]  = "15:00"
    market_engine.last_chain               = mock_chain

    # Add required strikes to mock chain with proper data
    for strike in [24150.0, 23850.0, 24300.0, 23700.0]:
        mock_chain[float(strike)] = {
            "call": {
                "bid": 10.0, "ask": 12.0, "ltp": 11.0,
                "delta": 0.20, "oi": 5000,
                "instrument_key": f"NSE_FO|NIFTY{int(strike)}CE",
            },
            "put": {
                "bid": 9.0, "ask": 11.0, "ltp": 10.0,
                "delta": -0.20, "oi": 5000,
                "instrument_key": f"NSE_FO|NIFTY{int(strike)}PE",
            },
        }

    test_params = {
        "final_lots":     2,
        "max_loss_per_lot": 3000.0,
        "total_max_risk": 6000.0,
        "entry_credit":   35.0,
        "legs": [
            {"strike": 24150.0, "option_type": "call", "action": "SELL", "exec_price": 22.0},
            {"strike": 23850.0, "option_type": "put",  "action": "SELL", "exec_price": 20.0},
            {"strike": 24300.0, "option_type": "call", "action": "BUY",  "exec_price": 8.0},
            {"strike": 23700.0, "option_type": "put",  "action": "BUY",  "exec_price": 7.0},
        ],
        "hard_exit_time": "15:00",
    }

    test_signals_ptv = {
        "actual_dte": 0,
        "spot": 24000.0,
        "vix": 11.5,
    }

    go, result = engine.validate_pre_trade(test_params, test_signals_ptv)
    print(f"  Valid params: go={go}")
    if go != "GO":
        print(f"  Reason: {result.get('reason')}")
    # Note: may fail timing check if run outside market hours — that's expected

    # Daily loss limit exceeded
    market_engine.state["daily_pnl"] = -(config.starting_capital * config.max_daily_loss_pct + 1)
    go2, result2 = engine.validate_pre_trade(test_params, test_signals_ptv)
    print(f"  Daily loss exceeded: go={go2} reason={result2.get('reason', '')[:40]}")
    assert go2 == "NO_GO", f"Expected NO_GO for daily loss exceeded, got {go2}"
    assert "daily_loss" in result2.get("reason", ""), \
        f"Expected daily_loss in reason, got {result2.get('reason')}"

    market_engine.state["daily_pnl"] = 0.0  # Reset

    print("  [OK] Pre-trade validation tests passed")

    # ── Test 9: Exit priority names ───────────────────────────────────────
    print_section("Exit Priority Names Tests")

    for priority, name in EXIT_PRIORITY_NAMES.items():
        reason = EXIT_REASON_MAP.get(priority, "UNKNOWN")
        print(f"  Priority {priority}: {name} → {reason}")
        assert name, f"Priority {priority} should have a name"
        assert reason, f"Priority {priority} should have a reason"

    print("  [OK] Exit priority names tests passed")

    # ── Test 10: Hard exit sweep ──────────────────────────────────────────
    print_section("Hard Exit Sweep Test")

    # Should not close anything (no open positions in test DB for today)
    engine.perform_hard_exit_sweep()
    print("  Hard exit sweep ran without error")
    print("  [OK] Hard exit sweep test passed")

    # ── Test 11: v7 close settlement — the fills, not the plan ─────────
    # Reproduces the 2026-09-11 BULL_PUT_SPREAD that exposed the bug: the
    # strategy engine books entry_credit as the PLANNED net credit (gross
    # minus an estimated slippage minus the entry charges in points), and
    # execute_close used to settle against that figure while subtracting the
    # same entry charges again in rupees. The leg rows carry what the fills
    # actually did, so they are the basis now.
    print_section("Close Settlement Tests (v7)")
    import contextlib as _ctxlib11
    import io as _io11

    _pid11 = "selftest-close-v7"
    _lots11 = 2
    _units11 = config.lot_size * _lots11
    db.insert("positions", {
        "position_id": _pid11, "trading_date": today_ist().isoformat(),
        "strategy_name": "BULL_PUT_SPREAD", "strategy_type": "SELL",
        "entry_time": now_ist().isoformat(),
        # planned: 19.05 gross - 0.026 estimated slippage - 0.4436 costs
        "entry_credit": 18.58,
        "gross_credit": 19.05,
        "entry_costs_rupees": 57.67,
        "final_lots": _lots11, "estimated_margin": 15730.0,
        "total_max_risk": 5693.0, "status": "OPEN",
    })
    for _k11, _a11, _px11 in ((23150.0, "SELL", 47.65), (23050.0, "BUY", 28.60)):
        db.insert("position_legs", {
            "position_id": _pid11, "strike": _k11, "option_type": "put",
            "action": _a11, "qty": _units11, "entry_price": _px11,
            "leg_status": "OPEN",
        })

    # The paper executor fills a buy-back at the ask and a sale at the bid,
    # so this chain IS the fill: 40.10 and 24.60, as on 2026-09-11.
    _chain11 = {
        23150.0: {"put": {"bid": 40.00, "ask": 40.10, "ltp": 40.05,
                          "delta": -0.30}},
        23050.0: {"put": {"bid": 24.60, "ask": 24.70, "ltp": 24.65,
                          "delta": -0.22}},
    }
    _saved_chain11 = market_engine.last_chain
    market_engine.last_chain = _chain11

    _pos11 = db.query_one(
        "SELECT * FROM positions WHERE position_id=?", (_pid11,))
    _legs11 = engine._get_position_legs(_pid11)

    # (a) the realised credit is the fills, and it is not the planned figure
    _rc11, _basis11 = realised_entry_credit(_pos11, _legs11)
    print(f"  entry credit: planned {float(_pos11['entry_credit']):.3f} pts, "
          f"realised {_rc11:.3f} pts (basis={_basis11})")
    assert _basis11 == "fills", \
        f"expected the fills to be the basis, got {_basis11}"
    assert abs(_rc11 - (47.65 - 28.60)) < 1e-9, \
        f"realised credit should be 19.05, got {_rc11}"
    assert abs(_rc11 - float(_pos11["entry_credit"])) > 0.01, \
        "this test is pointless if the planned and realised credit agree"

    # (b) exit charges land on the leg SOLD at exit, not on the buy-back
    _exit_costs11 = engine._compute_transaction_costs(
        [{"action": "SELL", "exit_price": 40.10},
         {"action": "BUY",  "exit_price": 24.60}], _lots11, "EXIT")
    _expect_stt11 = 24.60 * _units11 * config.stt_options_sell
    print(f"  exit STT: Rs{_exit_costs11['breakdown']['stt']:.2f} "
          f"(due Rs{_expect_stt11:.2f}, on the leg sold at exit)")
    assert abs(_exit_costs11["breakdown"]["stt"] - _expect_stt11) < 0.01, \
        f"exit STT should be {_expect_stt11:.2f}, got " \
        f"{_exit_costs11['breakdown']['stt']:.2f}"
    # ...and the entry side is untouched by the exit-side fix
    _entry_costs11 = engine._compute_transaction_costs(
        [{"action": "SELL", "exec_price": 47.65},
         {"action": "BUY",  "exec_price": 28.60}], _lots11, "ENTRY")
    assert abs(_entry_costs11["breakdown"]["stt"]
               - 47.65 * _units11 * config.stt_options_sell) < 0.01, \
        "entry STT must still be charged on the leg sold at entry"

    # (c) close through the real path and read the book back
    _state11 = dict(market_engine.state)
    try:
        engine.execute_close(
            _pos11, "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {})
    finally:
        market_engine.last_chain = _saved_chain11

    _row11 = db.query_one(
        "SELECT * FROM positions WHERE position_id=?", (_pid11,))
    _expect_gross11 = ((47.65 - 28.60) - (40.10 - 24.60)) * _units11
    _expect_net11 = _expect_gross11 - 57.67 - float(_row11["exit_costs_rupees"])
    # What the pre-v7 code booked for the very same trade: it settled on the
    # PLANNED credit and charged the exit STT/stamp on the entry-side legs
    # (the old call passed no side, so the entry rules applied at exit too).
    _old_exit_costs11 = engine._compute_transaction_costs(
        [{"action": "SELL", "exit_price": 40.10},
         {"action": "BUY",  "exit_price": 24.60}],
        _lots11, "ENTRY")["total_rupees"]
    _old_net11 = (18.58 - 15.50) * _units11 - 57.67 - _old_exit_costs11
    print(f"  gross P&L: Rs{float(_row11['gross_pnl_rupees']):,.2f} "
          f"(the fills say Rs{_expect_gross11:,.2f}; the planned credit "
          f"would have said Rs{(18.58 - 15.50) * _units11:,.2f})")
    print(f"  net P&L:   Rs{float(_row11['net_pnl_rupees']):,.2f} "
          f"(pre-v7 booked Rs{_old_net11:,.2f} for this trade), charges "
          f"Rs{57.67 + float(_row11['exit_costs_rupees']):,.2f}")
    assert _row11["status"] == "CLOSED", "position not closed"
    assert abs(float(_row11["exit_premium"]) - 15.50) < 1e-9, \
        f"exit premium should be 15.50, got {_row11['exit_premium']}"
    assert abs(float(_row11["gross_pnl_rupees"]) - _expect_gross11) < 0.01, \
        f"gross P&L must settle on the fills: {_expect_gross11:.2f}"
    assert abs(float(_row11["net_pnl_rupees"]) - _expect_net11) < 0.01, \
        "net P&L is not gross minus the charges actually booked"
    assert abs(float(_row11["entry_credit_realised"]) - 19.05) < 1e-9, \
        "entry_credit_realised not persisted"
    assert abs(float(_row11["net_pnl_rupees"]) - _old_net11) > 1.0, \
        "still settling on the planned credit and the wrong exit leg"

    _exit_row11 = db.query_one(
        "SELECT * FROM trade_exits WHERE position_id=?", (_pid11,))
    # v7: this row used never to exist. execute_close() writes exit_adx and
    # exit_vwap_dist, no schema declared them, the INSERT raised, and the
    # warning was swallowed - so the whole exit audit table stayed empty
    # (2026-09-11 book: 2 CLOSED positions, 0 trade_exits rows).
    assert _exit_row11 is not None, (
        "trade_exits row missing - the exit INSERT is failing again; check "
        "that every column execute_close() writes is declared in core.py")
    for _col11 in ("exit_adx", "exit_vwap_dist", "exit_priority",
                   "exit_priority_name", "exit_slippage"):
        assert _col11 in _exit_row11.keys(), \
            f"trade_exits is missing the {_col11} column"
    assert abs(float(_exit_row11["gross_pnl_pts"]) - 3.55) < 1e-9, \
        f"trade_exits.gross_pnl_pts should be 3.55, got " \
        f"{_exit_row11['gross_pnl_pts']}"
    assert abs(float(_exit_row11["net_pnl_rupees"])
               - float(_row11["net_pnl_rupees"])) < 0.01, \
        "trade_exits and positions disagree on what the trade made"
    assert _exit_row11["exit_priority_name"] == "TIME_TARGET", \
        f"exit priority name wrong: {_exit_row11['exit_priority_name']!r}"
    _legs_back11 = json.loads(_exit_row11["exit_legs_json"] or "[]")
    assert len(_legs_back11) == 2, "both exit legs must be in exit_legs_json"
    assert all(_l.get("exit_price") and _l.get("quoted_mid_at_exit")
               for _l in _legs_back11), \
        "exit legs must carry the fill and the quoted mid it was taken against"
    # v7: exit_slippage was 0.0 on every close the system ever made, because
    # the quoted mid never reached the audit dict the sum is built from. Both
    # legs here fill one tick away from the mid, so it must now be measured.
    _expect_slip11 = sum(
        abs(float(_l["exit_price"]) - float(_l["quoted_mid_at_exit"]))
        for _l in _legs_back11)
    assert _expect_slip11 > 0, "the test chain should not fill exactly on the mid"
    assert abs(float(_exit_row11["exit_slippage"]) - _expect_slip11) < 0.01, \
        f"exit_slippage should be {_expect_slip11:.3f}, got " \
        f"{_exit_row11['exit_slippage']}"
    print(f"  exit slippage measured: {_expect_slip11:.3f} pts "
          f"(booked {float(_exit_row11['exit_slippage']):.3f}; pre-v7 always 0.000)")
    print(f"  trade_exits row written: gross {float(_exit_row11['gross_pnl_pts']):.2f}pts "
          f"net Rs{float(_exit_row11['net_pnl_rupees']):,.2f} "
          f"({_exit_row11['result']})")

    # (d) and the console block reports the same money
    _rep11 = TradeConsoleReporter(db, config, logger, source="SELFTEST")
    _buf11 = _io11.StringIO()
    with _ctxlib11.redirect_stdout(_buf11):
        _n11 = _rep11.report_cycle(
            trading_date=today_ist().isoformat(), chain={}, as_of=now_ist())
    _text11 = _buf11.getvalue()
    assert _n11 == 1, f"expected 1 trade block, got {_n11}"
    for _frag11 in (
        "Trade-1", "Strategy: BULL_PUT_SPREAD",
        "Sold: 2 lot of PE with premium: 47.65 at strike: 23150",
        "Bought: 2 lot of PE with premium: 28.60 at strike: 23050",
        "Closed - CLOSE_TARGET", "Position Status: Close",
        f"Rs {float(_row11['net_pnl_rupees']):+,.2f} realised",
    ):
        assert _frag11 in _text11, f"trade block is missing {_frag11!r}"
    assert "the book says" not in _text11, \
        "the block and the book must agree on a freshly closed trade"

    market_engine.state.clear()
    market_engine.state.update(_state11)
    print("  [OK] Close settlement tests passed (v7)")

    db.close()
    print_section("EXECUTION ENGINE SELF-TEST COMPLETE", char="#")
    print("  All tests passed")
    print(f"  Database: {db.db_path}")
    print()


if __name__ == "__main__":
    _self_test()
