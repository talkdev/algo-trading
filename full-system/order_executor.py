# -*- coding: utf-8 -*-
"""
order_executor.py — order placement, position management, exits and the
regime-transition rules (spec §6, §7).

* Entries: LONG legs first, SHORT legs second (never a naked short if a hedge
  leg fails). LIMIT at Ask+1 / Bid-1. Ratio legs are separate orders (qty=1).
  Global timeout ORDER_FILL_TIMEOUT; any failed/partial leg cancels ALL
  pending orders and aborts the whole set.
* Paper mode: slippage 20 bps core shorts / 100 bps hedges (+1 tick).
* Exits: MARKET for stops/emergencies, LIMIT for profit taking.
* Transitions A-E on confirmed regime changes.
* Every fill and every regime change is checkpointed to SQLite.
"""
import json
import time
import uuid
from datetime import datetime

import config as C
from common_utilsimport fx

EXIT_PROFIT = "PROFIT_TARGET"
EXIT_STOP = "STOP_LOSS"
EXIT_TIME = "TIME_EXIT"
EXIT_REGIME = "REGIME_CHANGE"
EXIT_MANUAL = "MANUAL"
EXIT_EXPIRY = "EXPIRY_SQUAREOFF"
EXIT_DAILY_LOSS = "DAILY_LOSS"
EXIT_KILL = "KILL_SWITCH"


def est_transaction_costs(legs):
    """Estimated ₹ costs: brokerage + STT + exchange + SEBI + stamp + GST."""
    brokerage = C.COST_BROKERAGE_OPTION * max(len(legs), 1)
    prem = sum(l.get("premium_value", 0.0) for l in legs)
    stt = prem * C.COST_STT_OPTION_SELL_PCT
    exch = prem * C.COST_EXCHANGE_PCT
    sebi = prem * C.COST_SEBI_PCT
    stamp = prem * C.COST_STAMP_PCT
    gst = (brokerage + exch + sebi) * C.COST_GST_PCT
    return brokerage + stt + exch + sebi + stamp + gst


class OrderExecutor:
    def __init__(self, ctx):
        self.ctx = ctx                      # feed, broker, clock, state, loggers, risk
        self.open_trades = {}               # trade_id -> trade dict (in-memory mirror)
        self._load_open_trades()

    # ------------------------------------------------------------ persistence
    def _load_open_trades(self):
        for pos in self.ctx.state.get_open_positions():
            tid = pos["trade_id"]
            trade = {
                "trade_id": tid,
                "strategy_name": pos["strategy_name"],
                "regime_at_entry": pos["regime_at_entry"],
                "entry_ts": pos["entry_ts"],
                "entry_spot": pos["entry_spot"],
                "entry_vix": pos["entry_vix"],
                "expiry_date": pos["expiry_date"],
                "days_to_expiry": pos["days_to_expiry"],
                "lot_size": pos["lot_size"] or C.NIFTY_LOT_SIZE,
                "legs": json.loads(pos["legs_json"] or "[]"),
                "meta": json.loads(pos["meta_json"] or "{}"),
                "status": pos["status"],
                "scores": {},
            }
            self.open_trades[tid] = trade

    def _save_position(self, trade):
        self.ctx.state.upsert_position({
            "trade_id": trade["trade_id"],
            "strategy_name": trade["strategy_name"],
            "regime_at_entry": trade["regime_at_entry"],
            "status": trade.get("status", "OPEN"),
            "legs_json": json.dumps(trade["legs"]),
            "meta_json": json.dumps(trade.get("meta", {})),
            "entry_ts": trade["entry_ts"],
            "entry_spot": trade.get("entry_spot"),
            "entry_vix": trade.get("entry_vix"),
            "expiry_date": trade.get("expiry_date"),
            "days_to_expiry": trade.get("days_to_expiry"),
            "lot_size": trade.get("lot_size", C.NIFTY_LOT_SIZE),
        })

    def _checkpoint(self, reason="fill"):
        snap = {"spot": self.ctx.last_spot, "vix": self.ctx.last_vix,
                "reason": reason, "open_trades": list(self.open_trades)}
        self.ctx.state.checkpoint(snap, reason=reason)

    # ------------------------------------------------------------- entry
    def execute_entry(self, plan, env, regime, scores=None):
        """Execute a TradePlan. Returns trade_id or None on abort."""
        if not self.ctx.risk.margin_ok(plan):
            self.ctx.loggers.audit("ENTRY_ABORTED", strategy=plan.strategy_name, reason="margin")
            return None

        trade_id = str(uuid.uuid4())
        trade = {
            "trade_id": trade_id,
            "strategy_name": plan.strategy_name,
            "regime_at_entry": regime,
            "entry_ts": self.ctx.clock.now().isoformat(),
            "entry_spot": env.spot,
            "entry_vix": env.vix,
            "expiry_date": plan.expiry_date,
            "days_to_expiry": plan.days_to_expiry,
            "lot_size": plan.lot_size,
            "legs": [{"instrument_key": l.instrument_key, "side": l.side,
                      "qty": l.qty, "kind": l.kind, "role": l.role,
                      "status": "PENDING", "order_id": None, "fill_price": None}
                     for l in plan.legs],
            "meta": {**dict(plan.meta), "max_risk": plan.max_risk_points,
                     "risk_rupees": plan.max_risk_points * plan.lot_size},
            "meta_json": json.dumps({**plan.meta, "max_risk": plan.max_risk_points,
                                     "risk_rupees": plan.max_risk_points * plan.lot_size}),
            "status": "OPEN",
            "scores": scores or {},
            "exit_rules": plan.exit_rules,
        }
        self.open_trades[trade_id] = trade
        self._save_position(trade)
        self.ctx.loggers.audit("ENTRY_STARTED", trade_id=trade_id,
                               strategy=plan.strategy_name, regime=regime,
                               legs=len(plan.legs), spot=env.spot, vix=env.vix)

        # ---- sequence: longs first, shorts second ---------------------------
        long_legs = [l for l in plan.legs if l.kind == "long"]
        short_legs = [l for l in plan.legs if l.kind == "short"]
        ordered = long_legs + short_legs

        deadline = time.monotonic() + C.ORDER_FILL_TIMEOUT
        for leg in ordered:
            if time.monotonic() > deadline:
                self._abort_entry(trade_id, "global timeout")
                return None
            order = self._place_one(leg, trade_id)
            if order is None:
                self._abort_entry(trade_id, "order placement failed")
                return None
            if order["status"] in ("REJECTED",):
                self._abort_entry(trade_id, f"rejected: {order.get('reject_reason')}")
                return None
            if order.get("filled_qty", 0) < leg.qty:
                self._abort_entry(trade_id, f"partial fill {order.get('filled_qty')}/{leg.qty}")
                return None
            self._mark_filled(trade, leg, order)
            self._checkpoint("fill")

        trade["status"] = "OPEN"
        self._save_position(trade)
        gross_credit = sum(l.get("fill_price", 0) * l["qty"] for l in trade["legs"] if l["side"] == "SELL")
        debit = sum(l.get("fill_price", 0) * l["qty"] for l in trade["legs"] if l["side"] == "BUY")
        if trade["strategy_name"] in self.CREDIT_STRATEGIES:
            credit = gross_credit - debit          # net credit received
        else:
            credit = gross_credit
        trade["meta"]["entry_credit"] = round(credit, 2)
        trade["meta"]["entry_debit"] = round(debit, 2)
        self._save_position(trade)
        self.ctx.loggers.audit("ENTRY_COMPLETE", trade_id=trade_id,
                               strategy=plan.strategy_name, credit=round(credit, 2),
                               debit=round(debit, 2))
        return trade_id

    def _place_one(self, leg, trade_id):
        """Place one leg with proper order type/price. -> order dict."""
        q = self.ctx.broker.quote(leg.instrument_key)
        bid, ask = q.get("bid"), q.get("ask")
        tick = 0.05
        order_type = "LIMIT"
        limit_price = None
        if leg.side == "BUY":
            limit_price = (ask + tick) if ask is not None else None
        else:
            limit_price = (bid - tick) if bid is not None else None
        req = {"instrument_key": leg.instrument_key, "side": leg.side,
               "qty": leg.qty, "order_type": order_type,
               "limit_price": limit_price, "slippage_bps": leg.slippage_bps}
        try:
            order = self.ctx.broker.place_order(req, slippage_bps=leg.slippage_bps)
        except Exception as e:
            self.ctx.loggers.audit("ORDER_ERROR", trade_id=trade_id, leg=leg.instrument_key,
                                   error=str(e))
            return None
        # live-mode polling
        if order["status"] == "PENDING":
            order = self._await_fill(order["order_id"], leg, trade_id)
        order["leg_key"] = leg.instrument_key
        return order

    def _await_fill(self, order_id, leg, trade_id):
        deadline = time.monotonic() + C.CORE_FILL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                st = self.ctx.broker.order_history(order_id)
            except Exception as e:
                self.ctx.loggers.audit("ORDER_POLL_ERROR", order_id=order_id, error=str(e))
                continue
            if st["status"] in ("COMPLETE", "TRADED"):
                return {"order_id": order_id, "status": "COMPLETE",
                        "filled_qty": st.get("filled_qty", leg.qty),
                        "avg_price": st.get("avg_price")}
            if st["status"] in ("REJECTED", "CANCELLED"):
                return {"order_id": order_id, "status": "REJECTED",
                        "reject_reason": st.get("reject_reason")}
        return {"order_id": order_id, "status": "REJECTED", "reject_reason": "fill timeout"}

    def _mark_filled(self, trade, leg, order):
        for l in trade["legs"]:
            if l["instrument_key"] == leg.instrument_key:
                l["status"] = "COMPLETE"
                l["order_id"] = order.get("order_id")
                l["fill_price"] = order.get("avg_price")
        self.ctx.state.upsert_order({
            "order_id": order.get("order_id") or str(uuid.uuid4()),
            "trade_id": trade["trade_id"], "leg_key": leg.instrument_key,
            "instrument_key": leg.instrument_key, "side": leg.side,
            "qty": leg.qty, "filled_qty": order.get("filled_qty", leg.qty),
            "avg_price": order.get("avg_price"),
            "status": order.get("status", "COMPLETE"),
            "placed_ts": datetime.now().isoformat(), "updated_ts": datetime.now().isoformat()})

    def _abort_entry(self, trade_id, reason):
        trade = self.open_trades.pop(trade_id, None)
        if trade:
            # cancel any pending legs (best effort)
            for l in trade["legs"]:
                if l["order_id"] and l["status"] in ("PENDING",):
                    try:
                        self.ctx.broker.cancel_order(l["order_id"])
                    except Exception:
                        pass
            self.ctx.state.conn.execute("DELETE FROM positions WHERE trade_id=?", (trade_id,))
            self.ctx.state.conn.commit()
            # restore any fills already taken (paper broker holds them)
            for l in trade["legs"]:
                if l.get("fill_price") is not None and l["status"] == "COMPLETE":
                    self._reverse_fill(l)
        self.ctx.loggers.audit("ENTRY_ABORTED", trade_id=trade_id, reason=reason)

    def _reverse_fill(self, leg):
        """Undo a completed leg (abort path)."""
        q = self.ctx.broker.quote(leg["instrument_key"])
        px = q.get("last_price") or leg.get("fill_price") or 0
        side = "SELL" if leg["side"] == "BUY" else "BUY"
        try:
            self.ctx.broker.place_order({"instrument_key": leg["instrument_key"],
                                         "side": side, "qty": leg["qty"],
                                         "order_type": "MARKET"}, slippage_bps=100.0)
        except Exception:
            pass
        leg["status"] = "REVERSED"

    # ------------------------------------------------------------- monitor
    def manage_positions(self, res, env, now):
        """Run per-cycle checks: stops, profit targets, time exits, expiry
        square-off, daily-loss breaker. Returns list of closed trade ids."""
        closed = []
        for tid in list(self.open_trades):
            trade = self.open_trades.get(tid)
            if not trade or trade.get("status") != "OPEN":
                continue
            reason = self._check_trade(trade, res, env, now)
            if reason:
                self.close_trade(tid, reason)
                closed.append(tid)
        return closed

    def _check_trade(self, trade, res, env, now):
        rules = trade.get("exit_rules", {})
        spot = res.spot
        # ---- static spot stop (short straddle) ----------------------------
        if rules.get("static_stop_pct"):
            entry_spot = trade.get("entry_spot") or spot
            up = entry_spot * (1 + rules["static_stop_pct"])
            dn = entry_spot * (1 - rules["static_stop_pct"])
            if spot >= up or spot <= dn:
                return EXIT_STOP
        # ---- premium-based stop / profit target ---------------------------
        prem = self._current_premium(trade)
        entry_credit = trade["meta"].get("entry_credit", 0.0)
        entry_debit = trade["meta"].get("entry_debit", 0.0)
        is_credit = trade["strategy_name"] in ("ATM_STRADDLE_45D", "WIDE_IRON_CONDOR",
                                               "BULL_PUT_BEAR_CALL", "RATIO_SPREAD_1x2")
        if is_credit and entry_credit > 0:
            if prem <= entry_credit * rules.get("profit_target_pct", C.PROFIT_TARGET_PCT):
                return EXIT_PROFIT
            be = trade.get("meta", {}).get("be_stop")
            if be:
                if prem >= entry_credit:
                    return EXIT_STOP            # breakeven stop (Rule D)
            elif prem >= entry_credit * (1 + self.ctx.risk.stop_pct_for_vix(trade.get("entry_vix") or 15)):
                return EXIT_STOP                # premium rose past stop % of credit
        else:
            if entry_debit > 0:
                mult = rules.get("profit_target_mult")
                if mult and prem >= entry_debit * mult:
                    return EXIT_PROFIT
                mult = rules.get("stop_loss_mult")
                if mult and prem <= entry_debit * mult:
                    return EXIT_STOP
        # ---- time exits -----------------------------------------------------
        dte = (datetime.strptime(trade["expiry_date"], "%Y-%m-%d").date() - now.date()).days \
            if trade.get("expiry_date") else 0
        if rules.get("time_exit_days") and dte <= rules["time_exit_days"]:
            return EXIT_TIME
        if rules.get("hold_days"):
            entry_dt = datetime.fromisoformat(trade["entry_ts"])
            if (now - entry_dt).days >= rules["hold_days"] and now.time() >= C.TIME_EXIT_NORMAL:
                return EXIT_TIME
        # ---- expiry day square-off -------------------------------------------
        if C.is_expiry_day(now.date()) and now.time() >= C.EXPIRY_SQUARE_OFF:
            return EXIT_EXPIRY
        return None

    CREDIT_STRATEGIES = ("ATM_STRADDLE_45D", "WIDE_IRON_CONDOR",
                         "BULL_PUT_BEAR_CALL", "RATIO_SPREAD_1x2")

    def _current_premium(self, trade):
        """Current position value in premium points*lots.

        Credit strategies (sold premium): remaining liability = short value
        minus long value.  Debit strategies (bought premium): position value =
        sum of long (+ short) leg values.  Closed legs are excluded."""
        short_v = long_v = 0.0
        for l in trade["legs"]:
            if l.get("fill_price") is None or l.get("status") in ("CLOSED", "CLOSED_PARTIAL"):
                continue
            q = self.ctx.broker.quote(l["instrument_key"])
            px = q.get("last_price") or l["fill_price"]
            if l["side"] == "SELL":
                short_v += px * l["qty"]
            else:
                long_v += px * l["qty"]
        if trade["strategy_name"] in self.CREDIT_STRATEGIES:
            return short_v - long_v
        return short_v + long_v

    # ------------------------------------------------------------- exits
    def close_trade(self, trade_id, reason, market=True):
        trade = self.open_trades.get(trade_id)
        if not trade:
            return None
        # shorts first (buy back), then longs (sell)
        ordered = [l for l in trade["legs"] if l["side"] == "SELL"] + \
                  [l for l in trade["legs"] if l["side"] == "BUY"]
        realized = 0.0
        ideal = 0.0
        slip = 0.0
        for l in ordered:
            if l.get("fill_price") is None or l.get("status") in ("CLOSED", "CLOSED_PARTIAL"):
                continue
            q = self.ctx.broker.quote(l["instrument_key"])
            bid, ask = q.get("bid"), q.get("ask")
            close_side = "BUY" if l["side"] == "SELL" else "SELL"
            close_px = None
            if market or bid is None or ask is None:
                close_px = q.get("last_price") or l["fill_price"]
            else:
                close_px = (ask + 0.05) if close_side == "BUY" else (bid - 0.05)
            try:
                order = self.ctx.broker.place_order(
                    {"instrument_key": l["instrument_key"], "side": close_side,
                     "qty": l["qty"], "order_type": "MARKET" if market else "LIMIT",
                     "limit_price": None}, slippage_bps=100.0)
                avg = order.get("avg_price") or close_px
            except Exception:
                avg = close_px
            diff = (avg - l["fill_price"]) * (1 if l["side"] == "SELL" else -1)
            realized += diff * l["qty"]
            ideal += ((close_px if close_side == "SELL" else close_px) - l["fill_price"]) \
                * (1 if l["side"] == "SELL" else -1) * l["qty"]
            l["status"] = "CLOSED"
            l["close_price"] = avg
        slip = realized - ideal
        pnl_points = realized / (trade["lot_size"] or C.NIFTY_LOT_SIZE)
        costs = est_transaction_costs([{**l, "premium_value": (l.get("fill_price") or 0) * l["qty"]}
                                       for l in trade["legs"]])
        pnl_rupees = realized * (trade["lot_size"] or C.NIFTY_LOT_SIZE) - costs
        self.ctx.risk.add_day_pnl(pnl_points)
        self._write_trade_row(trade, reason, pnl_rupees, pnl_points, slip, costs)
        self.ctx.state.conn.execute(
            "UPDATE positions SET status=? WHERE trade_id=?", ("CLOSED", trade_id))
        self.ctx.state.conn.commit()
        self._checkpoint("close")
        self.ctx.loggers.audit("TRADE_CLOSED", trade_id=trade_id, reason=reason,
                               pnl_points=round(pnl_points, 2), pnl_rupees=round(pnl_rupees, 2))
        self.open_trades.pop(trade_id, None)
        return pnl_points

    def _write_trade_row(self, trade, reason, pnl_rupees, pnl_points, slip, costs):
        sc = trade.get("scores") or {}
        exit_ts = self.ctx.clock.now()
        try:
            entry_dt = datetime.fromisoformat(trade["entry_ts"])
        except (TypeError, ValueError):
            entry_dt = exit_ts
        self.ctx.loggers.trade_csv.append({
            "trade_id": trade["trade_id"],
            "strategy_name": trade["strategy_name"],
            "regime_at_entry": trade["regime_at_entry"],
            "regime_at_exit": self.ctx.last_regime or "",
            "entry_timestamp": trade["entry_ts"],
            "exit_timestamp": exit_ts.isoformat(),
            "holding_days": max((exit_ts - entry_dt).total_seconds(), 0.0) / 86400.0,
            "entry_spot": trade.get("entry_spot"),
            "exit_spot": self.ctx.last_spot,
            "entry_vix": trade.get("entry_vix"),
            "exit_vix": self.ctx.last_vix,
            "legs_summary": json.dumps([{"instrument": l["instrument_key"],
                                         "side": l["side"], "qty": l["qty"],
                                         "entry_price": l.get("fill_price"),
                                         "exit_price": l.get("close_price")}
                                        for l in trade["legs"]]),
            "total_credit_received": trade["meta"].get("entry_credit", 0.0),
            "total_debit_paid": trade["meta"].get("entry_debit", 0.0),
            "net_premium": (trade["meta"].get("entry_credit", 0.0) -
                            trade["meta"].get("entry_debit", 0.0)),
            "max_risk": trade["meta"].get("max_risk", 0.0),
            "realized_pnl": round(pnl_points, 2),
            "realized_pnl_percent": round(pnl_rupees / self.ctx.risk.capital * 100.0, 4),
            "exit_reason": reason,
            "slippage_total_points": round(slip / (trade["lot_size"] or C.NIFTY_LOT_SIZE), 2),
            "transaction_costs": round(costs, 2),
            "composite_score_at_entry": sc.get("composite"),
            "vol_score": sc.get("vol"), "edge_score": sc.get("edge"),
            "trend_score": sc.get("trend"), "flow_score": sc.get("flow"),
            "days_to_expiry_at_entry": trade.get("days_to_expiry"),
            "expiry_date": trade.get("expiry_date"),
            "paper_trade": C.PAPER_TRADING_MODE,
        })

    # ------------------------------------------------------- transitions A-E
    def apply_transitions(self, old_regime, new_regime, env):
        """Rules A-E (spec §6). Called when the confirmed regime changes."""
        if self.ctx.clock.now().time() >= C.TIME_LAST_IGNORE:
            self.ctx.loggers.audit("TRANSITION_SKIPPED", reason="after 14:45 IST (Rule E)")
            return
        shorts = [l for l in (ll for t in self.open_trades.values()
                              for ll in t["legs"]) if l["side"] == "SELL"]
        if new_regime == C.REGIME_STRONG_BUY:
            # Rule A: flatten ALL shorts immediately (market), keep longs 1 day.
            # For fully-short trades close the whole trade (REGIME_CHANGE exit);
            # for mixed trades flatten the short legs and hold the longs.
            for tid in list(self.open_trades):
                t = self.open_trades.get(tid)
                if not t:
                    continue
                if any(l["side"] == "SELL" for l in t["legs"]):
                    if all(l["side"] == "SELL" for l in t["legs"]):
                        self.close_trade(tid, EXIT_REGIME, market=True)
                    else:
                        self._flatten_shorts(tid)
                    self.ctx.loggers.audit("RULE_A", trade_id=tid)
            return
        if old_regime == C.REGIME_STRONG_SELL and new_regime == C.REGIME_MILD_SELL:
            # Rule B: close 50% of shorts (limit), tighten remaining to BE
            for tid in list(self.open_trades):
                t = self.open_trades.get(tid)
                if not t:
                    continue
                self._close_half_shorts(tid)
                self.ctx.loggers.audit("RULE_B", trade_id=tid)
            return
        if old_regime == C.REGIME_MILD_SELL and new_regime == C.REGIME_NEUTRAL:
            for tid in list(self.open_trades):
                t = self.open_trades.get(tid)
                if not t:
                    continue
                self._close_half_shorts(tid)
                self.ctx.loggers.audit("RULE_B", trade_id=tid)
            return
        if old_regime == C.REGIME_MILD_SELL and new_regime == C.REGIME_BUY:
            # Rule C: don't close shorts — buy 0.20-delta OTM puts = 100% notional
            self._hedge_shorts_with_puts(env)
            self.ctx.loggers.audit("RULE_C", env=env.regime)
            return
        if new_regime in (C.REGIME_NEUTRAL, C.REGIME_BUY) and \
                old_regime in (C.REGIME_STRONG_SELL, C.REGIME_MILD_SELL):
            # Rule D-ish: tighten stops to breakeven on remaining shorts
            self._breakeven_stops()
            return
        if old_regime == new_regime:
            # Rule D: same broad category -> breakeven stops
            self._breakeven_stops()

    def _flatten_shorts(self, tid):
        trade = self.open_trades[tid]
        for l in trade["legs"]:
            if l["side"] == "SELL" and l.get("fill_price") is not None:
                try:
                    self.ctx.broker.place_order(
                        {"instrument_key": l["instrument_key"], "side": "BUY",
                         "qty": l["qty"], "order_type": "MARKET"}, slippage_bps=100.0)
                    l["status"] = "CLOSED_PARTIAL"
                except Exception:
                    pass

    def _close_half_shorts(self, tid):
        trade = self.open_trades[tid]
        for l in trade["legs"]:
            if l["side"] == "SELL" and l.get("fill_price") is not None:
                half = max(1, l["qty"] // 2)
                if l["qty"] - half > 0:
                    try:
                        self.ctx.broker.place_order(
                            {"instrument_key": l["instrument_key"], "side": "BUY",
                             "qty": half, "order_type": "LIMIT",
                             "limit_price": self.ctx.broker.quote(l["instrument_key"]).get("bid")},
                            slippage_bps=100.0)
                        l["qty"] -= half
                    except Exception:
                        pass

    def _hedge_shorts_with_puts(self, env):
        """Rule C: buy 0.20-delta OTM puts covering 100% of short notional."""
        shorts = [l for t in self.open_trades.values() for l in t["legs"]
                  if l["side"] == "SELL"]
        if not shorts:
            return
        put_strike = env.strike_by_delta("PE", 0.20)
        if not put_strike:
            return
        total_qty = sum(l["qty"] for l in shorts)
        key = f"NSE_FO|NIFTY|{env.near_expiry}|{put_strike:.0f}|PE"
        q = self.ctx.broker.quote(key)
        if not q.get("last_price"):
            return
        try:
            self.ctx.broker.place_order({"instrument_key": key, "side": "BUY",
                                         "qty": total_qty, "order_type": "LIMIT",
                                         "limit_price": q.get("ask") + 0.05},
                                        slippage_bps=100.0)
        except Exception:
            pass

    def _breakeven_stops(self):
        # In paper mode stops are checked against entry credit; mark trades
        # so premium-stop threshold moves to the entry value (breakeven).
        for t in self.open_trades.values():
            t["meta"]["be_stop"] = True

    # ------------------------------------------------------------- emergency
    def square_off_all(self, reason=EXIT_MANUAL):
        for tid in list(self.open_trades):
            self.close_trade(tid, reason, market=True)

    def open_short_count(self):
        return sum(1 for t in self.open_trades.values()
                   for l in t["legs"] if l["side"] == "SELL")
