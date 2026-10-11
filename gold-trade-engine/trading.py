"""Risk, execution, and performance reporting."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone  # date used by risk ledger restore
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from config import EngineConfig
from feeds import atr

UTC = timezone.utc
log = logging.getLogger("gold_news.trading")


# --- risk_manager.py ---
from zoneinfo import ZoneInfo



@dataclass
class TradePlan:
    side: str                     # BUY | SELL
    entry_price: float
    stop_price: float
    target_price: float
    stop_distance: float
    target_distance: float
    lots: int
    quantity: int
    atr: float
    risk_budget: float
    estimated_loss_per_unit: float
    rejected: bool = False
    reject_reason: str = ""


@dataclass
class DailyRiskState:
    trading_day: Optional[date] = None
    day_start_equity: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    trading_costs: float = 0.0
    blocked: bool = False
    block_reason: str = ""


def calculate_position_size(
    equity: float,
    risk_pct: float,
    stop_distance: float,
    value_per_price_unit: float,
    estimated_cost_per_unit: float,
) -> int:
    """
    Units (lots) from risk budget. Never rounds UP to the broker minimum.
    """
    risk_budget = equity * risk_pct / 100.0
    loss_per_unit = stop_distance * value_per_price_unit + estimated_cost_per_unit
    if loss_per_unit <= 0:
        return 0
    units = int(risk_budget // loss_per_unit)
    return max(units, 0)


def stop_and_target(
    side: str,
    entry_price: float,
    stop_distance: float,
    reward_risk_ratio: float = 2.0,
) -> tuple[float, float, float]:
    target_distance = reward_risk_ratio * stop_distance
    if side.upper() in ("BUY", "LONG"):
        stop_price = entry_price - stop_distance
        target_price = entry_price + target_distance
    else:
        stop_price = entry_price + stop_distance
        target_price = entry_price - target_distance
    return stop_price, target_price, target_distance


class RiskManager:
    """Account equity, daily loss limit, and trade-plan construction."""

    def __init__(self, cfg: Optional[EngineConfig | dict] = None):
        self.cfg = cfg or EngineConfig.from_mapping()
        if isinstance(self.cfg, dict):
            self.cfg = EngineConfig.from_mapping(self.cfg)
        self.equity = float(self.cfg.initial_equity)
        self.daily = DailyRiskState(
            day_start_equity=self.equity,
        )
        self.open_positions = 0
        self.reject_log: list[dict[str, Any]] = []

    def trading_day_of(self, now_utc: datetime) -> date:
        tz = ZoneInfo(self.cfg.trading_day_tz)
        if now_utc.tzinfo is None:
            from datetime import timezone
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        return now_utc.astimezone(tz).date()

    def roll_day(self, now_utc: datetime) -> None:
        day = self.trading_day_of(now_utc)
        if self.daily.trading_day != day:
            self.daily = DailyRiskState(
                trading_day=day,
                day_start_equity=self.equity,
            )

    def mtm_equity(self) -> float:
        return self.equity + self.daily.unrealized_pnl

    def daily_loss_amount(self) -> float:
        """Positive when the account is down versus day-start equity (MTM)."""
        return self.daily.day_start_equity - self.mtm_equity()

    def daily_loss_reached(self, now_utc: datetime) -> bool:
        self.roll_day(now_utc)
        limit_pct = float(self.cfg.maximum_daily_loss_pct)
        if limit_pct <= 0:
            return False
        limit = self.daily.day_start_equity * limit_pct / 100.0
        # Loss = day_start - current MTM equity (costs already in equity if booked).
        loss = self.daily.day_start_equity - self.mtm_equity()
        if loss >= limit:
            self.daily.blocked = True
            self.daily.block_reason = (
                f"daily loss {loss:.2f} >= limit {limit:.2f}"
            )
            return True
        return self.daily.blocked

    def mark_unrealized(self, unrealized: float) -> None:
        self.daily.unrealized_pnl = float(unrealized)

    def book_cost(self, cost: float) -> None:
        self.daily.trading_costs += float(cost)
        self.equity -= float(cost)

    def book_realized(self, pnl: float) -> None:
        self.equity += float(pnl)
        self.daily.realized_pnl += float(pnl)
        self.daily.unrealized_pnl = 0.0

    def entry_price_for_side(self, side: str, bid: float, ask: float) -> float:
        return float(ask) if side.upper() in ("BUY", "LONG") else float(bid)

    def exit_price_for_side(self, side: str, bid: float, ask: float) -> float:
        # Close long at bid; close short at ask.
        return float(bid) if side.upper() in ("BUY", "LONG") else float(ask)

    def build_trade_plan(
        self,
        side: str,
        bid: float,
        ask: float,
        gold_candles,
        spread_points: Optional[float] = None,
    ) -> TradePlan:
        cfg = self.cfg
        entry = self.entry_price_for_side(side, bid, ask)
        spread = (ask - bid) if spread_points is None else float(spread_points)
        atr_val = atr(gold_candles, cfg.atr_period)

        def reject(reason: str) -> TradePlan:
            plan = TradePlan(
                side=side.upper(), entry_price=entry, stop_price=0.0,
                target_price=0.0, stop_distance=0.0, target_distance=0.0,
                lots=0, quantity=0, atr=float(atr_val or 0.0),
                risk_budget=self.mtm_equity() * cfg.risk_per_trade_pct / 100.0,
                estimated_loss_per_unit=0.0, rejected=True, reject_reason=reason,
            )
            self.reject_log.append({"side": side, "reason": reason, "entry": entry})
            return plan

        if atr_val is None or atr_val <= 0:
            return reject("atr_unavailable")

        stop_distance = cfg.atr_stop_multiplier * atr_val
        if stop_distance < cfg.min_stop_distance:
            return reject("stop_below_minimum")

        if spread > cfg.maximum_spread_points:
            return reject(f"spread_too_wide:{spread:.4f}")

        loss_per_unit = (
            stop_distance * cfg.value_per_price_unit + cfg.estimated_cost_per_unit
        )
        risk_budget = self.mtm_equity() * cfg.risk_per_trade_pct / 100.0
        sizing_mode = str(getattr(cfg, "sizing_mode", "fixed") or "fixed").lower()

        if sizing_mode == "fixed":
            lots = max(1, int(getattr(cfg, "fixed_lots", 1) or 1))
        else:
            lots = calculate_position_size(
                equity=self.mtm_equity(),
                risk_pct=cfg.risk_per_trade_pct,
                stop_distance=stop_distance,
                value_per_price_unit=cfg.value_per_price_unit,
                estimated_cost_per_unit=cfg.estimated_cost_per_unit,
            )
            if lots < cfg.min_position_lots:
                return reject("position_below_minimum")

        # Clamp by margin without ever rounding UP past the risk budget.
        # value_per_price_unit already embeds the contract multiplier (e.g. GOLD=100),
        # so notional per contract is price * value_per_price_unit.
        if cfg.margin_pct_of_notional > 0 and entry > 0:
            notional_per_lot = entry * cfg.value_per_price_unit
            margin_per_lot = notional_per_lot * cfg.margin_pct_of_notional
            if margin_per_lot > 0:
                max_lots_margin = int(self.mtm_equity() // margin_per_lot)
                if sizing_mode == "fixed" and lots > max_lots_margin:
                    return reject("insufficient_margin")
                lots = min(lots, max_lots_margin)
        if lots < cfg.min_position_lots:
            return reject("insufficient_margin")

        # Always enforce risk budget (including fixed-lot mode).
        estimated_risk = lots * loss_per_unit
        enforce_budget = bool(getattr(cfg, "enforce_risk_budget", True))
        if enforce_budget and estimated_risk > risk_budget * 1.0001:
            return reject(
                f"exceeds_risk_budget:{estimated_risk:.2f}>{risk_budget:.2f}"
            )

        # Canonical unit = lots. Upstox MCX commodity quantity = number of lots
        # (not lots × exchange lot_size). lot_size is retained for contract metadata.
        quantity = int(lots)

        stop_price, target_price, target_distance = stop_and_target(
            side, entry, stop_distance, cfg.reward_risk_ratio,
        )
        return TradePlan(
            side=side.upper(),
            entry_price=entry,
            stop_price=stop_price,
            target_price=target_price,
            stop_distance=stop_distance,
            target_distance=target_distance,
            lots=lots,
            quantity=quantity,
            atr=atr_val,
            risk_budget=risk_budget,
            estimated_loss_per_unit=loss_per_unit,
        )

# --- performance.py ---


@dataclass
class PerformanceReport:
    starting_equity: float
    ending_equity: float
    net_profit: float
    max_drawdown: float
    max_drawdown_pct: float
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_trade: float
    profit_factor: float
    news_trade_count: int
    non_news_trade_count: int
    news_net_profit: float
    non_news_net_profit: float
    reject_count: int
    reject_reasons: dict[str, int] = field(default_factory=dict)

    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0
    expectancy: float = 0.0
    max_consecutive_losses: int = 0

    def summary_lines(self) -> list[str]:
        return [
            f"Starting equity : {self.starting_equity:,.2f}",
            f"Ending equity   : {self.ending_equity:,.2f}",
            f"Net profit      : {self.net_profit:,.2f}",
            f"Max drawdown    : {self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%)",
            f"Trades          : {self.trade_count}  (W {self.win_count} / L {self.loss_count})",
            f"Win rate        : {self.win_rate:.1%}",
            f"Avg trade       : {self.avg_trade:,.2f}",
            f"Avg win / loss  : {self.avg_win:,.2f} / {self.avg_loss:,.2f}  payoff={self.payoff_ratio:.2f}",
            f"Expectancy      : {self.expectancy:,.2f}",
            f"Max consec loss : {self.max_consecutive_losses}",
            f"Profit factor   : {self.profit_factor:.3f}",
            f"News trades     : {self.news_trade_count}  pnl {self.news_net_profit:,.2f}",
            f"Non-news trades : {self.non_news_trade_count}  pnl {self.non_news_net_profit:,.2f}",
            f"Rejects         : {self.reject_count}",
        ]

    def __str__(self) -> str:
        return "\n".join(self.summary_lines())


def build_performance(
    trades: Iterable[Any],
    equity_curve: list[tuple[datetime, float]],
    starting_equity: float,
    rejects: Optional[list[dict]] = None,
) -> PerformanceReport:
    trades = list(trades)
    rejects = rejects or []
    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0
    )

    news_trades = [t for t in trades if getattr(t, "news_related", False)]
    non_news = [t for t in trades if not getattr(t, "news_related", False)]

    ending = equity_curve[-1][1] if equity_curve else starting_equity
    max_dd, max_dd_pct = _max_drawdown(equity_curve, starting_equity)

    reason_counts: dict[str, int] = {}
    for r in rejects:
        key = str(r.get("reason") or "unknown")
        reason_counts[key] = reason_counts.get(key, 0) + 1

    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else (float("inf") if avg_win > 0 else 0.0)
    wr = (len(wins) / len(trades)) if trades else 0.0
    expectancy = wr * avg_win + (1.0 - wr) * avg_loss if trades else 0.0
    max_cl = 0
    cur_cl = 0
    for p in pnls:
        if p <= 0:
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0

    return PerformanceReport(
        starting_equity=starting_equity,
        ending_equity=ending,
        net_profit=ending - starting_equity,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        trade_count=len(trades),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=wr,
        avg_trade=(sum(pnls) / len(pnls)) if pnls else 0.0,
        profit_factor=profit_factor,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff if payoff != float("inf") else 999.0,
        expectancy=expectancy,
        max_consecutive_losses=max_cl,
        news_trade_count=len(news_trades),
        non_news_trade_count=len(non_news),
        news_net_profit=sum(float(t.pnl) for t in news_trades),
        non_news_net_profit=sum(float(t.pnl) for t in non_news),
        reject_count=len(rejects),
        reject_reasons=reason_counts,
    )


def _max_drawdown(curve: list[tuple[datetime, float]],
                  starting: float) -> tuple[float, float]:
    if not curve:
        return 0.0, 0.0
    peak = starting
    max_dd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)
    pct = (max_dd / peak * 100.0) if peak > 0 else 0.0
    return max_dd, pct


def write_trades_csv(trades: Iterable[Any], path: str | Path) -> None:
    path = Path(path)
    rows = []
    for t in trades:
        rows.append({
            "side": t.side,
            "quantity": t.quantity,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "entry_time_utc": t.entry_time_utc.isoformat(),
            "exit_time_utc": t.exit_time_utc.isoformat(),
            "pnl": t.pnl,
            "costs": t.costs,
            "exit_reason": t.exit_reason,
            "stop_price": t.stop_price,
            "target_price": t.target_price,
            "breakout_key": t.breakout_key,
            "news_related": t.news_related,
        })
    if not rows:
        path.write_text(
            "side,quantity,entry_price,exit_price,entry_time_utc,exit_time_utc,"
            "pnl,costs,exit_reason,stop_price,target_price,breakout_key,news_related\n",
            encoding="utf-8",
        )
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_rejects_csv(rejects: list[dict], path: str | Path) -> None:
    path = Path(path)
    if not rejects:
        path.write_text("time,reason\n", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rejects:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rejects)

# --- execution ---
from typing import Any, Optional


def _round_to_tick(price: float, tick: float, *, direction: str = "nearest") -> float:
    """
    Tick-align a price.

    direction: nearest | down (more adverse for longs/stops below) |
               up (more adverse for shorts/stops above).
    """
    import math
    tick = float(tick or 1.0)
    if tick <= 0:
        return float(price)
    x = float(price) / tick
    if direction == "down":
        return round(math.floor(x + 1e-12) * tick, 10)
    if direction == "up":
        return round(math.ceil(x - 1e-12) * tick, 10)
    return round(round(x) * tick, 10)


def _round_stop_target(side: str, entry: float, stop: float, target: float,
                       tick: float) -> tuple[float, float, float]:
    """Direction-aware rounding; returns (stop, target, effective_stop_distance)."""
    if side.upper() in ("BUY", "LONG"):
        stop_r = _round_to_tick(stop, tick, direction="down")
        target_r = _round_to_tick(target, tick, direction="down")
        # Stop must remain strictly below entry
        if stop_r >= entry:
            stop_r = _round_to_tick(entry - tick, tick, direction="down")
    else:
        stop_r = _round_to_tick(stop, tick, direction="up")
        target_r = _round_to_tick(target, tick, direction="up")
        if stop_r <= entry:
            stop_r = _round_to_tick(entry + tick, tick, direction="up")
    eff = abs(entry - stop_r)
    return stop_r, target_r, eff


RUNTIME_STATE_VERSION = 3

# Persisted lifecycle — no unresolved state may enable new entries.
LIFECYCLE_IDLE = "idle"
LIFECYCLE_INTENT_CREATED = "intent_created"
LIFECYCLE_ORDER_SUBMITTED = "order_submitted"
LIFECYCLE_FILL_CONFIRMED = "fill_confirmed"
LIFECYCLE_PROTECTION_CONFIRMED = "protection_confirmed"
LIFECYCLE_EXIT_SUBMITTED = "exit_submitted"
LIFECYCLE_EXIT_CONFIRMED = "exit_confirmed"
LIFECYCLE_RECONCILED = "reconciled"
LIFECYCLE_UNRESOLVED = "unresolved"


@dataclass
class Position:
    side: str
    quantity: int              # broker order quantity (= lots for MCX commodity)
    entry_price: float
    stop_price: float
    target_price: float
    entry_time_utc: datetime
    lots: int = 1              # same as quantity for MCX; used for P&L / risk
    instrument_key: str = ""
    order_id: str = ""
    stop_order_id: str = ""    # broker-side SL-M protective order
    stop_distance: float = 0.0
    target_distance: float = 0.0
    breakout_key: str = ""
    tick_size: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    side: str
    quantity: int
    price: float
    timestamp_utc: datetime
    order_id: str = ""
    tag: str = ""


@dataclass
class ClosedTrade:
    side: str
    quantity: int
    entry_price: float
    exit_price: float
    entry_time_utc: datetime
    exit_time_utc: datetime
    pnl: float
    costs: float
    exit_reason: str
    stop_price: float
    target_price: float
    breakout_key: str = ""
    news_related: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class ExecutionEngine:
    """
    Paper by default. Live mode places MARKET orders via an Upstox-like client
    exposing place_order / quote / order_details.
    """

    def __init__(self, risk: RiskManager, client: Any = None, *,
                 live: bool = False, value_per_price_unit: float = 100.0,
                 estimated_cost_per_unit: float = 5.0,
                 persist_state: bool = True):
        self.risk = risk
        self.client = client
        self.live = live
        self.value_per_price_unit = value_per_price_unit
        self.estimated_cost_per_unit = estimated_cost_per_unit
        self.persist_state = bool(persist_state)
        self.position: Optional[Position] = None
        self.closed: list[ClosedTrade] = []
        self.fills: list[Fill] = []
        self.emergency_halt = False
        self.last_breakout_key: Optional[str] = None
        self._pending_intent: Optional[dict[str, Any]] = None
        self.state_corrupt: bool = False  # True → refuse entries until operator recovery
        self.lifecycle_state: str = LIFECYCLE_IDLE

    @property
    def has_open_position(self) -> bool:
        return self.position is not None

    def _halt(self, reason: str) -> None:
        self.emergency_halt = True
        log.error("EMERGENCY HALT: %s", reason)
        if self.persist_state:
            try:
                self.save_runtime_state()
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to persist halt: %s", exc)

    def open_trade(self, plan: TradePlan, now_utc: datetime,
                   instrument_key: str = "", breakout_key: str = "",
                   news_related: bool = False,
                   tick_size: float = 1.0) -> Optional[Position]:
        if plan.rejected or plan.quantity <= 0:
            return None
        if self.position is not None:
            return None
        if self.emergency_halt:
            return None
        # P0 #8: only idle / reconciled may accept a new entry
        ls = str(getattr(self, "lifecycle_state", "") or LIFECYCLE_IDLE)
        if ls not in (LIFECYCLE_IDLE, LIFECYCLE_RECONCILED, LIFECYCLE_EXIT_CONFIRMED, ""):
            log.error("open_trade blocked — lifecycle=%s (entries only from idle)", ls)
            self.emergency_halt = True
            return None
        # Execution-boundary gates
        if plan.side not in ("BUY", "SELL"):
            return None
        if not (plan.entry_price > 0 and plan.stop_distance > 0):
            return None

        order_id = ""
        fill_price = plan.entry_price
        filled_qty = int(plan.quantity)
        stop_order_id = ""
        intent_tag = ""
        if self.live:
            if self.client is None:
                raise RuntimeError("Live execution requires an Upstox client")
            if not getattr(self.client, "auth_ok", True):
                self._halt("auth_unhealthy")
                return None
            txn = "BUY" if plan.side == "BUY" else "SELL"
            import uuid
            intent_id = uuid.uuid4().hex  # collision-resistant; persist full id
            intent_tag = intent_id[:20]   # broker tag length limit
            # Persist intent BEFORE broker call (survives timeout / crash).
            self.lifecycle_state = LIFECYCLE_INTENT_CREATED
            self._pending_intent = {
                "intent_id": intent_id,
                "tag": intent_tag,
                "side": plan.side,
                "quantity": int(plan.quantity),
                "instrument_key": instrument_key,
                "breakout_key": breakout_key,
                "submitted_at": (now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)).isoformat(),
                "order_id": "",
                "state": "submitting",
                "lifecycle": self.lifecycle_state,
            }
            if not self._halt_soft_persist():
                return None
            try:
                order_id = self.client.place_order(
                    instrument_token=instrument_key,
                    transaction_type=txn,
                    quantity=int(plan.quantity),  # MCX: lots
                    order_type="MARKET",
                    tag=intent_tag,
                ) or ""
            except Exception as exc:  # noqa: BLE001
                # Ambiguous: may have been accepted — reconcile by tag, never retry place.
                log.error("Live entry place_order error: %s — reconciling by tag", exc)
                found = self._resolve_order_by_tag(
                    intent_tag, instrument_key=instrument_key,
                    side=plan.side, quantity=int(plan.quantity),
                )
                if found:
                    order_id = found
                else:
                    self._pending_intent["state"] = "unknown"
                    self.lifecycle_state = LIFECYCLE_UNRESOLVED
                    self._halt(f"entry_place_ambiguous:{exc}")
                    return None
            if not order_id:
                self._pending_intent["state"] = "no_order_id"
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt("entry_no_order_id")
                return None
            self._pending_intent["order_id"] = order_id
            self._pending_intent["state"] = "accepted"
            self.lifecycle_state = LIFECYCLE_ORDER_SUBMITTED
            if not self._halt_soft_persist():
                # Order already at broker — halt; do not continue unprotected
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                return None
            confirmed = self._confirm_fill(order_id, plan.quantity)
            if confirmed is None:
                self._pending_intent["state"] = "fill_unknown"
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt(f"entry_fill_unknown:{order_id}")
                return None
            fill_price, filled_qty, status = confirmed
            if filled_qty <= 0:
                self._pending_intent["state"] = "zero_fill"
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt(f"entry_zero_fill:{order_id}")
                return None
            self.lifecycle_state = LIFECYCLE_FILL_CONFIRMED
            if filled_qty < plan.quantity:
                log.warning(
                    "Partial ENTRY fill %s/%s status=%s — protect filled qty; halt",
                    filled_qty, plan.quantity, status,
                )
                self.emergency_halt = True
            # Recompute stop/target from actual fill; direction-aware tick rounding.
            stop_price, target_price, _ = stop_and_target(
                plan.side, fill_price, plan.stop_distance,
                self.risk.cfg.reward_risk_ratio,
            )
            plan_lots = int(filled_qty)  # broker qty == lots for MCX commodity
            stop_price, target_price, eff_dist = _round_stop_target(
                plan.side, fill_price, stop_price, target_price, tick_size,
            )
            # Recheck risk after rounding (distance may widen)
            loss_est = plan_lots * (
                eff_dist * self.value_per_price_unit + self.estimated_cost_per_unit
            )
            budget = self.risk.mtm_equity() * self.risk.cfg.risk_per_trade_pct / 100.0
            if bool(getattr(self.risk.cfg, "enforce_risk_budget", True)) and loss_est > budget * 1.05:
                log.error("Post-round risk %s > budget %s — flatten + halt", loss_est, budget)
                self.emergency_halt = True
                self._emergency_flatten(instrument_key, plan.side, filled_qty)
                self._pending_intent["state"] = "risk_reject_after_fill"
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt("post_round_risk")
                return None
            stop_order_id = self._place_broker_stop(
                instrument_key, plan.side, filled_qty, stop_price,
            )
            if bool(getattr(self.risk.cfg, "require_broker_stop", True)) and not stop_order_id:
                log.error("Broker SL-M unavailable after entry — verified flatten + halt")
                self.emergency_halt = True
                flat_ok = self._emergency_flatten(instrument_key, plan.side, filled_qty)
                self._pending_intent["state"] = "flatten_after_stop_fail"
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt("stop_place_failed" if flat_ok else "stop_fail_flatten_unknown")
                return None
            self.lifecycle_state = LIFECYCLE_PROTECTION_CONFIRMED
            self._pending_intent = None
        else:
            stop_price, target_price, _ = _round_stop_target(
                plan.side, plan.entry_price, plan.stop_price, plan.target_price, tick_size,
            )
            plan_lots = plan.lots

        entry_cost = self.estimated_cost_per_unit * plan_lots
        self.risk.book_cost(entry_cost)

        pos = Position(
            side=plan.side,
            quantity=filled_qty,
            lots=plan_lots,
            entry_price=fill_price,
            stop_price=stop_price,
            target_price=target_price,
            entry_time_utc=now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=UTC),
            instrument_key=instrument_key,
            order_id=order_id,
            stop_order_id=stop_order_id,
            stop_distance=plan.stop_distance,
            target_distance=plan.target_distance,
            breakout_key=breakout_key,
            tick_size=float(tick_size or 1.0),
            meta={
                "news_related": news_related,
                "entry_cost": entry_cost,
                "live_confirmed": bool(self.live),
                "planned_qty": int(plan.quantity),
                "intent_tag": intent_tag,
            },
        )
        self.position = pos
        self.risk.open_positions = 1
        self.last_breakout_key = breakout_key or self.last_breakout_key
        self.fills.append(Fill(
            side=plan.side, quantity=filled_qty, price=fill_price,
            timestamp_utc=pos.entry_time_utc, order_id=order_id, tag="entry",
        ))
        log.info(
            "OPEN %s qty=%s @ %.2f SL=%.2f TP=%.2f live=%s broker_sl=%s",
            plan.side, filled_qty, fill_price, stop_price, target_price,
            self.live, stop_order_id or "-",
        )
        if self.persist_state:
            try:
                self.save_runtime_state()
                self.save_risk_ledger()
            except Exception as exc:  # noqa: BLE001
                log.warning("runtime_state save after open failed: %s", exc)
        return pos

    def _halt_soft_persist(self) -> bool:
        """
        Persist intent/state before broker submission.

        Returns False if durable write fails — caller must NOT place the order.
        """
        if not self.persist_state:
            return True
        try:
            self.save_runtime_state()
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("CRITICAL persist failed before order — blocking submission: %s", exc)
            self._halt(f"persist_failed:{exc}")
            return False

    def _resolve_order_by_tag(
        self, tag: str, *, instrument_key: str = "", side: str = "",
        quantity: int = 0,
    ) -> str:
        """Resolve ambiguous place only when tag+instrument+side(+qty) uniquely match."""
        if not tag or self.client is None:
            return ""
        try:
            rows = self.client.find_orders_by_tag(tag)
        except Exception as exc:  # noqa: BLE001
            log.warning("find_orders_by_tag(%s): %s", tag, exc)
            return ""
        matches: list[str] = []
        for row in rows or []:
            key = str(row.get("instrument_token") or row.get("instrument_key") or "")
            if instrument_key and key and key != instrument_key:
                continue
            txn = str(row.get("transaction_type") or row.get("transactionType") or "").upper()
            if side and txn and txn != side.upper():
                continue
            if quantity > 0:
                try:
                    q = int(float(row.get("quantity") or 0))
                except (TypeError, ValueError):
                    q = 0
                if q and q != quantity:
                    continue
            oid = row.get("order_id") or row.get("orderId")
            if oid:
                matches.append(str(oid))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            log.error("Ambiguous tag %s matched %s orders — refuse auto-bind", tag, len(matches))
        return ""

    def _emergency_flatten(self, instrument_key: str, side: str, quantity: int) -> bool:
        """
        Market exit after stop state + position are reconciled.

        Returns True only when broker net qty is verified zero and no reversing
        working orders remain. ``not_found`` stop cancel is NOT permission to flatten.
        """
        import uuid
        if not self.live or self.client is None or quantity <= 0:
            return False
        # Reconcile current broker exposure first
        broker_qty = self.client.net_position_qty(instrument_key)
        if broker_qty is None:
            log.error("Cannot flatten: broker position unknown")
            self._halt("flatten_pos_unknown")
            return False
        if int(broker_qty) == 0:
            log.warning("Emergency flatten: broker already flat")
            return True

        if self.position and self.position.stop_order_id:
            outcome = self._cancel_broker_stop(self.position)
            if outcome == "filled":
                # Stop may have closed some/all — re-read residual
                broker_qty = self.client.net_position_qty(instrument_key)
                if broker_qty is None:
                    self._halt("flatten_after_stop_fill_unknown")
                    return False
                if int(broker_qty) == 0:
                    return True
                quantity = abs(int(broker_qty))
                side = "BUY" if int(broker_qty) > 0 else "SELL"
            elif outcome == "partial":
                broker_qty = self.client.net_position_qty(instrument_key)
                if broker_qty is None or int(broker_qty) == 0:
                    self._halt("flatten_partial_stop")
                    return int(broker_qty or 0) == 0
                quantity = abs(int(broker_qty))
                side = "BUY" if int(broker_qty) > 0 else "SELL"
            elif outcome != "cancelled":
                # not_found / pending / unknown / rejected — do NOT assume inactive
                log.error("Cannot flatten: stop cancel outcome=%s — halt", outcome)
                self._halt(f"flatten_stop_{outcome}")
                return False

        flat_tag = uuid.uuid4().hex[:20]
        self._pending_intent = {
            "intent_id": flat_tag,
            "tag": flat_tag,
            "side": ("SELL" if side == "BUY" else "BUY"),
            "quantity": int(quantity),
            "instrument_key": instrument_key,
            "state": "flatten_submitting",
            "lifecycle": LIFECYCLE_EXIT_SUBMITTED,
        }
        self.lifecycle_state = LIFECYCLE_EXIT_SUBMITTED
        if not self._halt_soft_persist():
            return False
        txn = "SELL" if side == "BUY" else "BUY"
        try:
            oid = self.client.place_order(
                instrument_token=instrument_key,
                transaction_type=txn,
                quantity=int(quantity),
                order_type="MARKET",
                tag=flat_tag,
            ) or ""
        except Exception as exc:  # noqa: BLE001
            found = self._resolve_order_by_tag(
                flat_tag, instrument_key=instrument_key, side=txn, quantity=int(quantity),
            )
            if not found:
                log.error("Emergency flatten place failed: %s — MANUAL INTERVENTION", exc)
                self._halt(f"flatten_ambiguous:{exc}")
                return False
            oid = found
        if not oid:
            self._halt("flatten_no_order_id")
            return False
        confirmed = self._confirm_fill(oid, quantity)
        if confirmed is None:
            log.error("Emergency flatten fill unknown order=%s", oid)
            self._halt(f"flatten_fill_unknown:{oid}")
            return False
        # Authoritative flat check
        final_qty = self.client.net_position_qty(instrument_key)
        if final_qty is None or int(final_qty) != 0:
            self._halt(f"flatten_residual:{final_qty}")
            return False
        # Ensure no working reverse/orphan orders
        orphans = self._discover_orphan_stops(instrument_key)
        if orphans is None:
            self._halt("flatten_orderbook_unknown")
            return False
        if orphans:
            self._halt(f"flatten_working_orders:{orphans}")
            return False
        self._pending_intent = None
        self.lifecycle_state = LIFECYCLE_EXIT_CONFIRMED
        return True

    def _confirm_fill(
        self, order_id: str, expected_qty: int, *, attempts: int = 10, sleep_s: float = 0.5,
    ) -> Optional[tuple[float, int, str]]:
        """
        Poll broker until fill is known.

        Returns (avg_price, filled_qty, status) or None.
        Never treats order ``quantity`` (requested size) as filled quantity.
        """
        import time
        last: dict = {}
        for i in range(attempts):
            try:
                last = self.client.order_details(order_id) or {}
            except Exception as exc:  # noqa: BLE001
                log.warning("order_details(%s) attempt %s: %s", order_id, i + 1, exc)
                time.sleep(sleep_s)
                continue
            status = str(
                last.get("status") or last.get("order_status") or ""
            ).lower().replace(" ", "_")
            avg = (
                last.get("average_price")
                or last.get("avg_price")
                or last.get("averagePrice")
            )
            # Explicit fill fields ONLY — never fall back to order size "quantity"
            filled_raw = (
                last.get("filled_quantity")
                if last.get("filled_quantity") is not None
                else last.get("filled_qty")
                if last.get("filled_qty") is not None
                else last.get("traded_quantity")
                if last.get("traded_quantity") is not None
                else last.get("filledQuantity")
            )
            pending_raw = (
                last.get("pending_quantity")
                if last.get("pending_quantity") is not None
                else last.get("pending_qty")
                if last.get("pending_qty") is not None
                else last.get("pendingQuantity")
            )
            try:
                avg_f = float(avg) if avg not in (None, "", 0, 0.0, "0") else None
            except (TypeError, ValueError):
                avg_f = None
            try:
                filled_i = int(float(filled_raw)) if filled_raw not in (None, "") else 0
            except (TypeError, ValueError):
                filled_i = 0
            try:
                pending_i = int(float(pending_raw)) if pending_raw not in (None, "") else None
            except (TypeError, ValueError):
                pending_i = None

            rejected = status in (
                "rejected", "cancelled", "canceled", "failed", "expired",
            )
            if rejected and filled_i <= 0:
                log.error("Order %s rejected/cancelled with no fill: %s", order_id, last)
                return None

            terminal = status in (
                "complete", "completed", "filled", "traded", "closed",
                "rejected", "cancelled", "canceled",  # may still have partial fill
            )
            # Fully filled
            if filled_i > 0 and avg_f is not None and avg_f > 0:
                if filled_i >= expected_qty or (pending_i == 0) or terminal:
                    return avg_f, filled_i, status or "filled"
            time.sleep(sleep_s)
        log.error(
            "Order %s not confirmed after %s attempts (last=%s)",
            order_id, attempts, last,
        )
        return None

    def _place_broker_stop(
        self, instrument_key: str, side: str, quantity: int, stop_price: float,
    ) -> str:
        import uuid
        if not self.live or self.client is None:
            return ""
        if not bool(getattr(self.risk.cfg, "place_broker_stop", True)):
            return ""
        txn = "SELL" if side == "BUY" else "BUY"
        stop_tag = uuid.uuid4().hex[:20]
        # Persist stop intent before submission (recovery if timeout after accept)
        prev_intent = self._pending_intent
        self._pending_intent = {
            "intent_id": stop_tag,
            "tag": stop_tag,
            "kind": "stop",
            "side": txn,
            "quantity": int(quantity),
            "instrument_key": instrument_key,
            "trigger_price": float(stop_price),
            "state": "stop_submitting",
            "order_id": "",
        }
        if not self._halt_soft_persist():
            self._pending_intent = prev_intent
            return ""
        try:
            oid = self.client.place_stop_loss_market(
                instrument_token=instrument_key,
                transaction_type=txn,
                quantity=quantity,
                trigger_price=stop_price,
                tag=stop_tag,
            ) or ""
        except Exception as exc:  # noqa: BLE001
            found = self._resolve_order_by_tag(
                stop_tag, instrument_key=instrument_key, side=txn, quantity=int(quantity),
            )
            if not found:
                log.error("Broker SL-M place failed/ambiguous: %s", exc)
                self._halt(f"stop_place_ambiguous:{exc}")
                return ""
            oid = found
        if not oid:
            # No order id ≠ no stop — try tag recover once more
            oid = self._resolve_order_by_tag(
                stop_tag, instrument_key=instrument_key, side=txn, quantity=int(quantity),
            ) or ""
        if not oid:
            self._halt("stop_no_order_id")
            return ""
        self._pending_intent["order_id"] = oid
        self._pending_intent["state"] = "stop_accepted"
        self._halt_soft_persist()
        active = self._verify_stop_active(
            oid, quantity, instrument_key=instrument_key,
            exit_side=txn, trigger_price=stop_price,
            require_all_fields=True,
        )
        if active is True:
            log.info(
                "Broker SL-M ACTIVE id=%s trigger=%.2f qty=%s tag=%s",
                oid, stop_price, quantity, stop_tag,
            )
            self._pending_intent = prev_intent
            return oid
        if active == "filled":
            log.error("SL-M %s already FILLED — not active protection", oid)
            self._pending_intent = prev_intent
            return ""
        log.error("SL-M id=%s UNVERIFIED — cancelling before any flatten", oid)
        outcome = self.client.cancel_order(oid)
        from upstox_client import CancelOutcome
        if outcome == CancelOutcome.FILLED:
            log.error("SL-M filled during failed-verify cancel")
            self._halt(f"stop_filled_unverified:{oid}")
            return ""
        if outcome != CancelOutcome.CONFIRMED_CANCELLED:
            self._halt(f"orphan_stop:{oid}:{outcome}")
            return ""
        self._pending_intent = prev_intent
        return ""

    def _verify_stop_active(
        self, order_id: str, expected_qty: int, *, attempts: int = 8, sleep_s: float = 0.4,
        instrument_key: str = "", exit_side: str = "", trigger_price: float = 0.0,
        require_all_fields: bool = True, tick_size: float = 0.0,
    ):
        """
        Positive confirmation of working SL-M protection.

        When ``require_all_fields`` (default), missing instrument / side / type /
        qty / trigger → UNVERIFIED (False). Trigger compared within 1 tick.
        Returns True | False | \"filled\".
        """
        import time
        if not order_id:
            return False
        tick = float(tick_size or 0.0)
        if tick <= 0 and self.position is not None:
            tick = float(self.position.tick_size or 0.0)
        if tick <= 0:
            tick = 1.0
        for _ in range(attempts):
            try:
                details = self.client.order_details(order_id) or {}
            except Exception as exc:  # noqa: BLE001
                log.warning("verify SL-M %s: %s", order_id, exc)
                time.sleep(sleep_s)
                continue
            if not details:
                time.sleep(sleep_s)
                continue
            status = str(
                details.get("status") or details.get("order_status") or ""
            ).lower().replace(" ", "_")
            ik = str(details.get("instrument_token") or details.get("instrument_key") or "")
            txn = str(details.get("transaction_type") or "").upper()
            otype = str(details.get("order_type") or "").upper().replace(" ", "").replace("_", "")
            qty_raw = details.get("quantity")
            if qty_raw is None:
                qty_raw = details.get("pending_quantity")
            try:
                qty_i = int(float(qty_raw)) if qty_raw not in (None, "") else 0
            except (TypeError, ValueError):
                qty_i = 0
            try:
                tp = float(details.get("trigger_price")) if details.get("trigger_price") not in (None, "") else None
            except (TypeError, ValueError):
                tp = None

            if status in ("rejected", "cancelled", "canceled", "failed", "expired"):
                return False
            if status in ("complete", "completed", "filled", "traded"):
                return "filled"
            if status not in (
                "trigger_pending", "open", "pending", "after_market_order_req",
                "modify_pending",
            ):
                time.sleep(sleep_s)
                continue

            # Positive field checks — absent fields are UNVERIFIED (never assume OK)
            if require_all_fields:
                if not order_id:
                    return False
                if not ik or (instrument_key and ik != instrument_key):
                    log.warning("SL-M %s instrument unverified (got=%r want=%r)", order_id, ik, instrument_key)
                    return False
                if not txn or (exit_side and txn != exit_side.upper()):
                    log.warning("SL-M %s side unverified (got=%r want=%r)", order_id, txn, exit_side)
                    return False
                if not otype or (
                    otype not in ("SL-M", "SLM", "SL", "STOPLOSSMARKET", "STOPLOSS_MARKET")
                    and "SL" not in otype and "STOP" not in otype
                ):
                    log.warning("SL-M %s order_type unverified (%r)", order_id, otype)
                    return False
                if qty_i <= 0 or qty_i != expected_qty:
                    log.warning("SL-M %s qty unverified expected=%s got=%s", order_id, expected_qty, qty_i)
                    return False
                # Trigger always required for positive verification
                if tp is None or tp <= 0:
                    log.warning("SL-M %s trigger missing — UNVERIFIED", order_id)
                    return False
                if trigger_price > 0:
                    if abs(tp - trigger_price) > tick + 1e-9:
                        log.warning(
                            "SL-M %s trigger mismatch expected=%.4f got=%.4f tick=%.4f",
                            order_id, trigger_price, tp, tick,
                        )
                        return False
                return True

            # Lenient path (should rarely be used)
            if qty_i > 0 and qty_i == expected_qty:
                return True
            return False
        return False

    def save_runtime_state(self, path: Optional[Path] = None) -> Path:
        """Atomic persist of halt, position, pending intent."""
        from config import ENGINE_DIR
        import json
        import os
        path = Path(path) if path else ENGINE_DIR / "runtime_state.json"
        pos = self.position
        payload = {
            "schema_version": RUNTIME_STATE_VERSION,
            "emergency_halt": self.emergency_halt,
            "last_breakout_key": self.last_breakout_key,
            "lifecycle_state": self.lifecycle_state,
            "pending_intent": self._pending_intent,
            "position": None if pos is None else {
                "side": pos.side,
                "quantity": pos.quantity,
                "lots": pos.lots,
                "entry_price": pos.entry_price,
                "stop_price": pos.stop_price,
                "target_price": pos.target_price,
                "entry_time_utc": pos.entry_time_utc.isoformat(),
                "instrument_key": pos.instrument_key,
                "order_id": pos.order_id,
                "stop_order_id": pos.stop_order_id,
                "stop_distance": pos.stop_distance,
                "target_distance": pos.target_distance,
                "breakout_key": pos.breakout_key,
                "tick_size": pos.tick_size,
                "meta": pos.meta,
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        bak = path.with_suffix(path.suffix + ".bak")
        text = json.dumps(payload, indent=2)
        tmp.write_text(text, encoding="utf-8")
        if path.exists():
            try:
                if bak.exists():
                    bak.unlink()
                os.replace(path, bak)
            except OSError:
                pass
        os.replace(tmp, path)
        return path

    def save_risk_ledger(self, path: Optional[Path] = None) -> Path:
        from config import ENGINE_DIR
        import json
        import os
        path = Path(path) if path else ENGINE_DIR / "risk_ledger.json"
        d = self.risk.daily
        payload = {
            "schema_version": RUNTIME_STATE_VERSION,
            "equity": self.risk.equity,
            "trading_day": d.trading_day.isoformat() if d.trading_day else None,
            "day_start_equity": d.day_start_equity,
            "realized_pnl": d.realized_pnl,
            "unrealized_pnl": d.unrealized_pnl,
            "trading_costs": d.trading_costs,
            "blocked": d.blocked,
            "block_reason": d.block_reason,
            "open_positions": self.risk.open_positions,
            "saved_at_utc": datetime.now(tz=UTC).isoformat(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load_risk_ledger(self, path: Optional[Path] = None) -> bool:
        from config import ENGINE_DIR
        import json
        path = Path(path) if path else ENGINE_DIR / "risk_ledger.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.risk.equity = float(data["equity"])
            day_s = data.get("trading_day")
            self.risk.daily = DailyRiskState(
                trading_day=date.fromisoformat(day_s) if day_s else None,
                day_start_equity=float(data.get("day_start_equity") or self.risk.equity),
                realized_pnl=float(data.get("realized_pnl") or 0),
                unrealized_pnl=float(data.get("unrealized_pnl") or 0),
                trading_costs=float(data.get("trading_costs") or 0),
                blocked=bool(data.get("blocked")),
                block_reason=str(data.get("block_reason") or ""),
            )
            self.risk.open_positions = int(data.get("open_positions") or 0)
            log.info(
                "Restored risk ledger equity=%.2f day=%s blocked=%s",
                self.risk.equity, self.risk.daily.trading_day, self.risk.daily.blocked,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("risk_ledger corrupt: %s", exc)
            self.state_corrupt = True
            self.emergency_halt = True
            return False

    def load_runtime_state(self, path: Optional[Path] = None) -> str:
        """
        Restore halt/position.

        Returns: ``missing`` | ``ok`` | ``corrupt`` | ``incompatible``.
        Corrupt/incompatible → fail closed (state_corrupt + halt).
        """
        from config import ENGINE_DIR
        import json
        path = Path(path) if path else ENGINE_DIR / "runtime_state.json"
        if not path.exists():
            return "missing"
        try:
            raw_text = path.read_text(encoding="utf-8")
            if not raw_text.strip():
                raise ValueError("empty state file")
            data = json.loads(raw_text)
            if not isinstance(data, dict):
                raise ValueError("state root not an object")
        except Exception as exc:  # noqa: BLE001
            log.error("runtime_state CORRUPT: %s — fail closed", exc)
            self.state_corrupt = True
            self.emergency_halt = True
            return "corrupt"
        ver = int(data.get("schema_version") or 1)
        if ver > RUNTIME_STATE_VERSION:
            log.error("runtime_state schema %s newer than %s — fail closed",
                      ver, RUNTIME_STATE_VERSION)
            self.state_corrupt = True
            self.emergency_halt = True
            return "incompatible"
        self.emergency_halt = bool(data.get("emergency_halt"))
        self.last_breakout_key = data.get("last_breakout_key") or self.last_breakout_key
        self.lifecycle_state = str(data.get("lifecycle_state") or LIFECYCLE_IDLE)
        self._pending_intent = data.get("pending_intent")
        raw = data.get("position")
        if not raw:
            if self._pending_intent or self.lifecycle_state in (
                LIFECYCLE_INTENT_CREATED, LIFECYCLE_ORDER_SUBMITTED,
                LIFECYCLE_UNRESOLVED, LIFECYCLE_EXIT_SUBMITTED,
            ):
                self.emergency_halt = True
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                log.warning("Unresolved lifecycle=%s — halt until reconcile", self.lifecycle_state)
            return "ok"
        try:
            et = datetime.fromisoformat(str(raw["entry_time_utc"]))
            if et.tzinfo is None:
                et = et.replace(tzinfo=UTC)
            self.position = Position(
                side=str(raw["side"]),
                quantity=int(raw["quantity"]),
                lots=int(raw.get("lots") or raw["quantity"]),
                entry_price=float(raw["entry_price"]),
                stop_price=float(raw["stop_price"]),
                target_price=float(raw["target_price"]),
                entry_time_utc=et,
                instrument_key=str(raw.get("instrument_key") or ""),
                order_id=str(raw.get("order_id") or ""),
                stop_order_id=str(raw.get("stop_order_id") or ""),
                stop_distance=float(raw.get("stop_distance") or 0),
                target_distance=float(raw.get("target_distance") or 0),
                breakout_key=str(raw.get("breakout_key") or ""),
                tick_size=float(raw.get("tick_size") or 1),
                meta=dict(raw.get("meta") or {}),
            )
            self.risk.open_positions = 1
            self.emergency_halt = True
            log.warning(
                "Restored local position %s qty=%s — emergency_halt until reconcile",
                self.position.side, self.position.quantity,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to restore position: %s — fail closed", exc)
            self.state_corrupt = True
            self.emergency_halt = True
            return "corrupt"
        return "ok"

    def _cancel_broker_stop(self, pos: Position) -> str:
        """
        Cancel protective SL-M.

        Returns: cancelled | filled | partial | pending | unknown | not_found | rejected
        Does NOT clear stop_order_id unless confirmed cancelled with zero fill.
        """
        if not pos.stop_order_id:
            # No protective order outstanding — safe to proceed (paper or live).
            return "cancelled"
        if not self.live or self.client is None:
            pos.stop_order_id = ""
            return "cancelled"
        from upstox_client import CancelOutcome
        try:
            outcome = self.client.cancel_order(pos.stop_order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cancel SL-M %s failed: %s", pos.stop_order_id, exc)
            outcome = CancelOutcome.UNKNOWN
        if outcome == CancelOutcome.CONFIRMED_CANCELLED:
            log.info("Cancelled broker SL-M %s", pos.stop_order_id)
            pos.stop_order_id = ""
            return "cancelled"
        if outcome == CancelOutcome.FILLED:
            log.warning("SL-M %s FILLED — do not send conflicting exit", pos.stop_order_id)
            return "filled"
        if outcome == CancelOutcome.PARTIAL_FILL:
            return "partial"
        if outcome == CancelOutcome.STILL_PENDING:
            return "pending"
        if outcome == CancelOutcome.REJECTED:
            return "rejected"
        if outcome == CancelOutcome.NOT_FOUND:
            # Keep local stop id — absence of details ≠ confirmed inactive.
            return "not_found"
        log.error("SL-M %s cancel unresolved (%s)", pos.stop_order_id, outcome)
        return "unknown"

    def reconcile_broker(
        self, instrument_key: str = "", *, clear_halt_on_ok: bool = False,
        discover_orphans: bool = False,
    ) -> dict[str, Any]:
        """
        Compare local position with broker net qty and protective stop status.

        Halts on mismatch / unknown. Never treats API failure as flat.
        """
        if self.state_corrupt:
            return {"ok": False, "reason": "state_corrupt", "halt": True}
        key = instrument_key or (self.position.instrument_key if self.position else "")
        if not self.live or self.client is None or not key:
            report = {
                "ok": True,
                "skipped": True,
                "local_signed_qty": (
                    0 if self.position is None
                    else (self.position.quantity if self.position.side == "BUY"
                          else -self.position.quantity)
                ),
                "broker_net_qty": 0,
                "stop_ok": True,
                "orphan_stops": 0,
            }
            if clear_halt_on_ok and not self.state_corrupt:
                self.emergency_halt = False
                self.lifecycle_state = LIFECYCLE_RECONCILED
            return report
        broker_qty = self.client.net_position_qty(key)
        local_qty = 0
        if self.position is not None:
            local_qty = (
                self.position.quantity
                if self.position.side == "BUY"
                else -self.position.quantity
            )
        report: dict[str, Any] = {
            "ok": True,
            "local_signed_qty": local_qty,
            "broker_net_qty": broker_qty,
            "instrument_key": key,
            "stop_ok": True,
            "orphan_stops": 0,
        }
        if broker_qty is None:
            report["ok"] = False
            report["reason"] = "broker_positions_unavailable"
            self._halt("broker_positions_unavailable")
            return report
        if int(broker_qty) != int(local_qty):
            if self.position is None and self._pending_intent and int(broker_qty) != 0:
                report["ok"] = False
                report["reason"] = "pending_intent_exposure"
                self._halt("pending_intent_exposure")
                return report
            report["ok"] = False
            report["reason"] = "position_mismatch"
            self._halt(f"position_mismatch local={local_qty} broker={broker_qty}")
            return report
        if self.position is not None and self.position.stop_order_id:
            exit_side = "SELL" if self.position.side == "BUY" else "BUY"
            stop_state = self._verify_stop_active(
                self.position.stop_order_id, self.position.quantity,
                attempts=3, sleep_s=0.25,
                instrument_key=key, exit_side=exit_side,
                trigger_price=float(self.position.stop_price),
            )
            report["stop_order_id"] = self.position.stop_order_id
            if stop_state is True:
                report["stop_ok"] = True
            elif stop_state == "filled":
                report["ok"] = False
                report["stop_ok"] = False
                report["reason"] = "stop_filled_local_still_open"
                self._halt("stop_filled_reconcile_required")
                return report
            else:
                report["ok"] = False
                report["stop_ok"] = False
                report["reason"] = "protective_stop_missing"
                self._halt("protective_stop_missing")
                return report
        elif self.position is not None and bool(
            getattr(self.risk.cfg, "require_broker_stop", True)
        ):
            report["ok"] = False
            report["reason"] = "protective_stop_absent"
            report["stop_ok"] = False
            self._halt("protective_stop_absent")
            return report

        # Discover all working orders for instrument (order-book failure ≠ empty)
        if discover_orphans:
            orphans = self._discover_orphan_stops(key)
            if orphans is None:
                report["ok"] = False
                report["reason"] = "order_book_unavailable"
                self._halt("order_book_unavailable")
                return report
            report["orphan_stops"] = len(orphans)
            known = set()
            if self.position and self.position.stop_order_id:
                known.add(self.position.stop_order_id)
            if self.position and self.position.order_id:
                known.add(self.position.order_id)
            unknown = [o for o in orphans if o not in known]
            if unknown and self.position is None:
                report["ok"] = False
                report["reason"] = "orphan_working_orders"
                report["orphan_ids"] = unknown
                self._halt(f"orphan_orders:{unknown}")
                return report
            if unknown and self.position is not None:
                # Extra working orders beyond known entry/stop
                report["ok"] = False
                report["reason"] = "unexpected_working_orders"
                report["orphan_ids"] = unknown
                self._halt(f"unexpected_orders:{unknown}")
                return report

        if clear_halt_on_ok and report["ok"] and not self.state_corrupt:
            if self.lifecycle_state not in (LIFECYCLE_UNRESOLVED,):
                self.emergency_halt = False
                self.lifecycle_state = LIFECYCLE_RECONCILED
                log.info("RECONCILE ok — emergency_halt cleared")
        return report

    def _discover_orphan_stops(self, instrument_key: str) -> Optional[list[str]]:
        """
        Working order ids for instrument that can create/reverse exposure.

        Returns None if order book unavailable (fail closed — not empty list).
        """
        if self.client is None:
            return None
        try:
            from upstox_client import OrderBookResult
            res = self.client.fetch_order_book()
        except Exception as exc:  # noqa: BLE001
            log.warning("order_book for orphan scan failed: %s", exc)
            return None
        if not getattr(res, "ok", False):
            return None
        out: list[str] = []
        for row in res.rows or []:
            key = str(row.get("instrument_token") or row.get("instrument_key") or "")
            if key != instrument_key:
                continue
            status = str(row.get("status") or row.get("order_status") or "").lower().replace(" ", "_")
            if status not in (
                "trigger_pending", "open", "pending", "after_market_order_req",
                "modify_pending",
            ):
                continue
            # Any working order on the instrument (entry/exit/stop) is relevant
            oid = row.get("order_id") or row.get("orderId")
            if oid:
                out.append(str(oid))
        return out

    def unrealized_pnl(self, bid: float, ask: float) -> float:
        pos = self.position
        if pos is None:
            return 0.0
        px = self.risk.exit_price_for_side(pos.side, bid, ask)
        direction = 1.0 if pos.side == "BUY" else -1.0
        return direction * (px - pos.entry_price) * pos.lots * self.value_per_price_unit

    def monitor(self, now_utc: datetime, bid: float, ask: float,
                bar_high: Optional[float] = None,
                bar_low: Optional[float] = None,
                bar_open: Optional[float] = None,
                data_valid: bool = True,
                *,
                allow_software_exits: bool = True) -> Optional[ClosedTrade]:
        """
        Exit on stop, target, or daily-loss policy.

        When ``data_valid`` / ``allow_software_exits`` is false (stale bar or
        quote outage), do NOT software-flatten: halt new entries, mark MTM if
        possible, check daily-loss only, and rely on broker protection +
        reconcile (audit P0 #12).

        Same-candle ambiguity: stop preferred over target (worse outcome).
        Gap-through stop: if bar opens beyond the stop, fill at the open
        (worse than stop) rather than assuming a perfect stop fill.
        """
        pos = self.position
        if pos is None:
            return None

        if not data_valid or not allow_software_exits:
            self.emergency_halt = True
            # Roll + MTM + daily loss still apply; no bar-based stop/target.
            self.risk.roll_day(now_utc)
            try:
                self.risk.mark_unrealized(self.unrealized_pnl(bid, ask))
            except Exception:  # noqa: BLE001
                pass
            if self.risk.daily_loss_reached(now_utc):
                return self._close(now_utc, bid, ask, "daily_loss_limit")
            return None

        # Roll trading day BEFORE marking unrealized (audit §31)
        self.risk.roll_day(now_utc)
        self.risk.mark_unrealized(self.unrealized_pnl(bid, ask))
        if self.risk.daily_loss_reached(now_utc):
            return self._close(now_utc, bid, ask, "daily_loss_limit")

        high = bar_high if bar_high is not None else max(bid, ask)
        low = bar_low if bar_low is not None else min(bid, ask)
        o = float(bar_open) if bar_open is not None else None

        stop_hit = target_hit = False
        if pos.side == "BUY":
            stop_hit = low <= pos.stop_price
            target_hit = high >= pos.target_price
        else:
            stop_hit = high >= pos.stop_price
            target_hit = low <= pos.target_price

        def stop_fill() -> float:
            if pos.side == "BUY":
                if o is not None and o < pos.stop_price:
                    return o
                return pos.stop_price
            if o is not None and o > pos.stop_price:
                return o
            return pos.stop_price

        if stop_hit and target_hit:
            return self._close(now_utc, bid, ask, "stop_loss",
                               fill_override=stop_fill())
        if stop_hit:
            return self._close(now_utc, bid, ask, "stop_loss",
                               fill_override=stop_fill())
        if target_hit:
            return self._close(now_utc, bid, ask, "take_profit",
                               fill_override=pos.target_price)
        return None

    def _close(self, now_utc: datetime, bid: float, ask: float, reason: str,
               fill_override: Optional[float] = None) -> Optional[ClosedTrade]:
        pos = self.position
        assert pos is not None
        # Bid/ask-aware exit; fill_override (stop/gap) taken only if it is worse.
        side_exit = self.risk.exit_price_for_side(pos.side, bid, ask)
        if fill_override is not None:
            fo = float(fill_override)
            if pos.side == "BUY":
                exit_price = min(fo, side_exit)  # long exit: lower is worse
            else:
                exit_price = max(fo, side_exit)  # short exit: higher is worse
        else:
            exit_price = side_exit

        close_qty = int(pos.quantity)
        # Cancel protective SL before market exit — classified outcomes only.
        cancel_state = self._cancel_broker_stop(pos)
        if cancel_state == "filled":
            # Stop filled — reconcile residual BEFORE assuming flat
            log.warning("Close (%s): stop FILLED — reconciling residual", reason)
            broker_px = exit_price
            filled_stop = 0
            try:
                if self.live and self.client and pos.stop_order_id:
                    det = self.client.order_details(pos.stop_order_id) or {}
                    ap = det.get("average_price") or det.get("avg_price")
                    if ap not in (None, "", 0, 0.0):
                        broker_px = float(ap)
                    filled_stop = int(float(
                        det.get("filled_quantity") or det.get("filled_qty") or 0
                    ))
            except Exception:  # noqa: BLE001
                pass
            exit_price = broker_px
            if self.live and self.client is not None:
                bq = self.client.net_position_qty(pos.instrument_key)
                if bq is None:
                    self._halt("stop_fill_pos_unknown")
                    return None
                if int(bq) != 0:
                    # Residual remains — protect exact residual, do not second-guess flat
                    residual = abs(int(bq))
                    log.error("Stop filled but residual broker qty=%s — protect residual", bq)
                    pos.quantity = residual
                    pos.lots = residual
                    pos.stop_order_id = self._place_broker_stop(
                        pos.instrument_key, pos.side, residual, pos.stop_price,
                    )
                    self.lifecycle_state = LIFECYCLE_UNRESOLVED
                    self._halt("stop_fill_residual")
                    return None
            close_qty = filled_stop if filled_stop > 0 else pos.quantity
            reason = reason + "+stop_filled"
        elif cancel_state in ("pending", "unknown", "partial", "rejected", "not_found"):
            log.error(
                "Abort market exit (%s): stop cancel state=%s — reconcile, no second order",
                reason, cancel_state,
            )
            self.lifecycle_state = LIFECYCLE_UNRESOLVED
            self._halt(f"stop_cancel_{cancel_state}")
            if self.live and self.client is not None:
                bq = self.client.net_position_qty(pos.instrument_key)
                if bq == 0:
                    log.warning("Broker flat after ambiguous stop — clearing local via reconcile")
                    close_qty = pos.quantity
                    reason = reason + "+broker_flat_reconcile"
                else:
                    return None
            else:
                return None

        if (
            self.live and self.client is not None
            and "stop_filled" not in reason
            and "broker_flat_reconcile" not in reason
        ):
            import uuid
            txn = "SELL" if pos.side == "BUY" else "BUY"
            exit_tag = uuid.uuid4().hex[:20]
            self.lifecycle_state = LIFECYCLE_EXIT_SUBMITTED
            self._pending_intent = {
                "intent_id": exit_tag, "tag": exit_tag, "kind": "exit",
                "side": txn, "quantity": int(pos.quantity),
                "instrument_key": pos.instrument_key, "state": "exit_submitting",
            }
            if not self._halt_soft_persist():
                return None
            try:
                oid = self.client.place_order(
                    instrument_token=pos.instrument_key,
                    transaction_type=txn,
                    quantity=pos.quantity,
                    order_type="MARKET",
                    tag=exit_tag,
                ) or ""
            except Exception as exc:  # noqa: BLE001
                oid = self._resolve_order_by_tag(
                    exit_tag, instrument_key=pos.instrument_key,
                    side=txn, quantity=int(pos.quantity),
                )
                if not oid:
                    log.error("Live close failed (%s): %s — resolve before re-arm", reason, exc)
                    self.lifecycle_state = LIFECYCLE_UNRESOLVED
                    self._halt(f"close_exception:{exc}")
                    return None
            if not oid:
                oid = self._resolve_order_by_tag(
                    exit_tag, instrument_key=pos.instrument_key,
                    side=txn, quantity=int(pos.quantity),
                ) or ""
            if not oid:
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt(f"close_no_order_id:{reason}")
                return None
            confirmed = self._confirm_fill(oid, pos.quantity)
            if confirmed is None:
                # Resolve by tag/details before any replacement stop
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt(f"close_fill_unknown:{oid}")
                bq = self.client.net_position_qty(pos.instrument_key)
                if bq is not None and int(bq) != 0:
                    residual = abs(int(bq))
                    pos.quantity = residual
                    pos.lots = residual
                    # Only protect verified residual after exit order resolved unknown
                    pos.stop_order_id = self._place_broker_stop(
                        pos.instrument_key, pos.side, residual, pos.stop_price,
                    )
                return None
            exit_price, close_qty, status = confirmed
            if close_qty <= 0:
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt(f"close_zero_fill:{oid}")
                return None
            if close_qty < pos.quantity:
                log.error(
                    "PARTIAL CLOSE fill %s/%s status=%s — residual stays open + protected; "
                    "NOT marking flat",
                    close_qty, pos.quantity, status,
                )
                self.lifecycle_state = LIFECYCLE_UNRESOLVED
                self._halt(f"close_partial:{close_qty}/{pos.quantity}")
            else:
                # Confirm broker flat before marking exit confirmed
                bq = self.client.net_position_qty(pos.instrument_key)
                if bq is None or int(bq) != 0:
                    self.lifecycle_state = LIFECYCLE_UNRESOLVED
                    self._halt(f"close_broker_not_flat:{bq}")
                    if bq is not None and int(bq) != 0:
                        residual = abs(int(bq))
                        pos.quantity = residual
                        pos.lots = residual
                        pos.stop_order_id = self._place_broker_stop(
                            pos.instrument_key, pos.side, residual, pos.stop_price,
                        )
                    return None
                self.lifecycle_state = LIFECYCLE_EXIT_CONFIRMED
                self._pending_intent = None

        # MCX: broker qty == lots
        closed_lots = int(close_qty)
        if close_qty < pos.quantity:
            closed_lots = max(1, int(close_qty))

        direction = 1.0 if pos.side == "BUY" else -1.0
        gross = direction * (exit_price - pos.entry_price) * closed_lots * self.value_per_price_unit
        exit_cost = self.estimated_cost_per_unit * closed_lots
        entry_cost_share = float(pos.meta.get("entry_cost", 0.0)) * (
            close_qty / max(pos.quantity, 1)
        )
        self.risk.book_cost(exit_cost)
        self.risk.book_realized(gross)

        trade = ClosedTrade(
            side=pos.side,
            quantity=close_qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time_utc=pos.entry_time_utc,
            exit_time_utc=now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=UTC),
            pnl=gross - entry_cost_share - exit_cost,
            costs=entry_cost_share + exit_cost,
            exit_reason=reason + ("_partial" if close_qty < pos.quantity else ""),
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            breakout_key=pos.breakout_key,
            news_related=bool(pos.meta.get("news_related")),
        )
        self.closed.append(trade)
        self.fills.append(Fill(
            side=("SELL" if pos.side == "BUY" else "BUY"),
            quantity=close_qty, price=exit_price,
            timestamp_utc=trade.exit_time_utc, tag=trade.exit_reason,
        ))
        log.info("CLOSE %s qty=%s/%s @ %.2f reason=%s pnl=%.2f",
                 pos.side, close_qty, pos.quantity, exit_price, trade.exit_reason, trade.pnl)

        remaining = pos.quantity - close_qty
        if remaining > 0:
            pos.quantity = remaining
            pos.lots = max(1, remaining)
            pos.meta["entry_cost"] = float(pos.meta.get("entry_cost", 0.0)) - entry_cost_share
            pos.stop_order_id = self._place_broker_stop(
                pos.instrument_key, pos.side, pos.quantity, pos.stop_price,
            )
            self.risk.open_positions = 1
            self.risk.mark_unrealized(self.unrealized_pnl(bid, ask))
            log.warning(
                "Residual local position qty=%s after partial close — halt active",
                remaining,
            )
        else:
            self.position = None
            self.risk.open_positions = 0
            self.risk.mark_unrealized(0.0)
            if self.lifecycle_state in (
                LIFECYCLE_EXIT_CONFIRMED, LIFECYCLE_RECONCILED, "",
            ):
                self.lifecycle_state = LIFECYCLE_IDLE
            elif not self.live:
                self.lifecycle_state = LIFECYCLE_IDLE
        if self.persist_state:
            try:
                self.save_runtime_state()
                self.save_risk_ledger()
            except Exception as exc:  # noqa: BLE001
                log.warning("runtime_state save after close failed: %s", exc)
        return trade
