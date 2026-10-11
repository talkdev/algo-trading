"""Historical simulation without look-ahead bias."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from config import EngineConfig
from feeds import EconomicEvent, events_known_as_of, validate_candle_frame, value_at_or_before
from strategy import build_signal_from_market
from trading import ExecutionEngine, PerformanceReport, RiskManager, build_performance

UTC = timezone.utc
log = logging.getLogger("gold_news.backtest")


@dataclass
class BacktestResult:
    trades: list
    rejects: list[dict[str, Any]]
    equity_curve: list[tuple[datetime, float]]
    performance: PerformanceReport
    cfg: EngineConfig
    meta: dict[str, Any] = field(default_factory=dict)


def run_backtest(
    gold_candles: pd.DataFrame,
    dxy: pd.Series,
    yields: pd.Series,
    events: list[EconomicEvent],
    cfg: Optional[EngineConfig] = None,
    real_yields: Optional[pd.Series] = None,
    usdinr: Optional[pd.Series] = None,
    bid_ask_spread: float = 0.0,
) -> BacktestResult:
    """
    Walk forward over completed gold candles.

    Decision at the close of bar i uses only data available then.
    Entry fills at the next bar's open (executable post-signal price) with
    spread + adverse slippage — never at the same bar's close.
    Open positions are managed on every bar including the fill bar.
    """
    cfg = cfg or EngineConfig.from_mapping()
    slip = max(0.0, float(getattr(cfg, "backtest_slippage_points", 0.0) or 0.0))
    fee_extra = max(0.0, float(getattr(cfg, "backtest_fee_per_lot", 0.0) or 0.0))
    risk = RiskManager(cfg)
    exe = ExecutionEngine(
        risk,
        live=False,
        value_per_price_unit=cfg.value_per_price_unit,
        estimated_cost_per_unit=cfg.estimated_cost_per_unit + fee_extra,
        persist_state=False,
    )
    equity_curve: list[tuple[datetime, float]] = []
    rejects: list[dict[str, Any]] = []

    if gold_candles is None or gold_candles.empty:
        perf = build_performance([], [], cfg.initial_equity)
        return BacktestResult([], [], [], perf, cfg, meta={"error": "no_gold_data"})

    # P1 #39: reject malformed candle timeline before simulation
    if not validate_candle_frame(gold_candles):
        perf = build_performance([], [], cfg.initial_equity)
        return BacktestResult(
            [], [], [], perf, cfg,
            meta={"error": "invalid_gold_candles"},
        )

    # P1 #36: spread must be explicit — default 0 is optimistic and disallowed
    # for research reports (wiring/synthetic callers must pass a positive spread
    # or set meta allow_zero_spread).
    if bid_ask_spread is None or float(bid_ask_spread) < 0:
        raise ValueError("bid_ask_spread must be >= 0")
    if float(bid_ask_spread) == 0.0 and not bool(
        getattr(cfg, "backtest_allow_zero_spread", False)
    ):
        # Use conservative default from max spread / 2 rather than silent zero
        bid_ask_spread = max(2.0, float(cfg.maximum_spread_points) * 0.1)
        log.warning(
            "backtest bid_ask_spread was 0 — using conservative %.2f "
            "(set backtest_allow_zero_spread=true only for wiring tests)",
            bid_ask_spread,
        )

    if gold_candles.index.tz is None:
        gold_candles = gold_candles.copy()
        gold_candles.index = gold_candles.index.tz_localize("UTC")
    else:
        gold_candles = gold_candles.tz_convert("UTC")

    tf = int(cfg.timeframe_minutes)
    min_bars = max(cfg.gold_breakout_lookback_bars + 1, cfg.atr_period + 2)
    half = float(bid_ask_spread) / 2.0
    # Share live entry-age gate (P1.2)
    trade_max = float(getattr(cfg, "macro_trade_max_age_minutes", 60) or 60)
    max_age = timedelta(minutes=trade_max)

    # Pending signal to fill on next bar open
    pending: Optional[dict[str, Any]] = None

    for i in range(min_bars, len(gold_candles)):
        bar_ts = gold_candles.index[i]
        bar = gold_candles.iloc[i]
        now_utc = (bar_ts + pd.Timedelta(minutes=tf)).to_pydatetime()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)

        hist = gold_candles.iloc[: i + 1]
        completed = hist
        open_px = float(bar["open"])
        # Executable bid/ask at this bar's open (for fills + monitoring)
        bid = open_px - half - slip
        ask = open_px + half + slip

        # Fill pending signal from prior completed bar at THIS bar's open.
        if pending is not None and not exe.has_open_position:
            plan = pending["plan"]
            # Rebuild plan at executable prices
            plan2 = risk.build_trade_plan(
                pending["side"], bid, ask, pending["completed"],
            )
            if plan2.rejected:
                rejects.append({
                    "time": now_utc.isoformat(),
                    "reason": f"fill_reject:{plan2.reject_reason}",
                    "signal": pending["side"],
                })
            else:
                exe.open_trade(
                    plan2, now_utc,
                    breakout_key=pending["breakout_key"],
                    news_related=pending["news_related"],
                )
            pending = None

        if not validate_candle_frame(completed):
            rejects.append({"time": now_utc.isoformat(), "reason": "invalid_candles"})
            if exe.has_open_position:
                exe.monitor(
                    now_utc, bid, ask,
                    bar_high=float(bar["high"]), bar_low=float(bar["low"]),
                    bar_open=open_px, data_valid=False,
                )
            equity_curve.append((now_utc, risk.mtm_equity()))
            continue

        # Manage open position on this bar (intrabar high/low + gap-aware stop).
        if exe.has_open_position:
            exe.monitor(
                now_utc, bid, ask,
                bar_high=float(bar["high"]), bar_low=float(bar["low"]),
                bar_open=open_px,
                data_valid=True,
            )

        risk.roll_day(now_utc)
        daily_hit = risk.daily_loss_reached(now_utc)

        visible_events = events_known_as_of(events, now_utc, strict_retrieval=True)

        dxy_v, _ = value_at_or_before(dxy, now_utc, max_age=max_age)
        y_v, _ = value_at_or_before(yields, now_utc, max_age=max_age)
        data_ok = dxy_v is not None and y_v is not None and validate_candle_frame(completed)

        signal = build_signal_from_market(
            now_utc=now_utc,
            events=visible_events,
            dxy=dxy,
            yields=yields,
            gold_candles=completed,
            data_is_valid=data_ok,
            daily_loss_limit_reached=daily_hit,
            has_open_position=exe.has_open_position or pending is not None,
            real_yields=real_yields,
            cfg=cfg,
            last_breakout_key=exe.last_breakout_key,
        )

        if signal.decision == "NO_TRADE":
            rejects.append({
                "time": now_utc.isoformat(),
                "reason": signal.reason,
                "bias": signal.bias,
                "dxy": signal.dxy_direction,
                "yield": signal.yield_direction,
            })
        elif exe.has_open_position or pending is not None:
            rejects.append({
                "time": now_utc.isoformat(),
                "reason": "already_pending_or_open",
                "signal": signal.decision,
            })
        else:
            # Decision uses completed bar close context; fill deferred to next open.
            # Use mid of close only for preliminary plan rejection checks.
            mid_c = float(bar["close"])
            prelim_bid = mid_c - half - slip
            prelim_ask = mid_c + half + slip
            plan = risk.build_trade_plan(signal.decision, prelim_bid, prelim_ask, completed)
            if plan.rejected:
                rejects.append({
                    "time": now_utc.isoformat(),
                    "reason": plan.reject_reason,
                    "signal": signal.decision,
                })
            else:
                news_related = bool(
                    signal.meta.get("inside_news_block") or signal.meta.get("news_day")
                )
                pending = {
                    "side": signal.decision,
                    "plan": plan,
                    "completed": completed.copy(),
                    "breakout_key": str(signal.meta.get("breakout_key") or ""),
                    "news_related": news_related,
                    "decision_time": now_utc.isoformat(),
                }

        equity_curve.append((now_utc, risk.mtm_equity()))

    # No next bar for a trailing pending signal — drop it (never fill at last close).
    if pending is not None:
        rejects.append({
            "time": pending["decision_time"],
            "reason": "no_next_bar_for_fill",
            "signal": pending["side"],
        })

    if exe.has_open_position and len(gold_candles):
        last = gold_candles.iloc[-1]
        ts = gold_candles.index[-1].to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        mid = float(last["close"])
        exe._close(
            ts, mid - half - slip, mid + half + slip, "end_of_data",
        )
        equity_curve.append((ts, risk.mtm_equity()))
    elif equity_curve:
        last_ts = equity_curve[-1][0]
        equity_curve.append((last_ts, risk.mtm_equity()))

    perf = build_performance(exe.closed, equity_curve, cfg.initial_equity, rejects)
    # P1.34 — report validation
    if abs(perf.net_profit - (perf.ending_equity - perf.starting_equity)) > 1e-6:
        log.error("Performance net_profit inconsistent with equity change")
    trade_pnl_sum = sum(float(t.pnl) for t in exe.closed)
    # Costs are inside trade.pnl; equity change includes costs booked separately —
    # allow small float tolerance on wiring check.
    meta = {
        "usdinr_available": usdinr is not None and not getattr(usdinr, "empty", True),
        "slippage_points": slip,
        "fee_per_lot_extra": fee_extra,
        "bid_ask_spread": bid_ask_spread,
        "entry_model": "next_bar_open",
        "macro_max_age_minutes": trade_max,
        "trade_pnl_sum": trade_pnl_sum,
    }
    return BacktestResult(
        trades=exe.closed,
        rejects=rejects,
        equity_curve=equity_curve,
        performance=perf,
        cfg=cfg,
        meta=meta,
    )


def generate_synthetic_dataset(
    n_bars: int = 500,
    seed: int = 42,
    start: str = "2026-01-05T03:30:00+00:00",
) -> dict[str, Any]:
    """
    Offline dataset with intentional DXY/yield/gold relationships for self-tests.

    Not for research conclusions — only to prove the engine wires correctly.
    """
    import numpy as np
    from feeds import EconomicEvent

    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, periods=n_bars, freq="5min", tz="UTC")

    dxy = 104.0 + np.cumsum(rng.normal(0, 0.005, size=n_bars))
    yld = 4.20 + np.cumsum(rng.normal(0, 0.001, size=n_bars))
    gold_close = 72_000.0 + np.cumsum(rng.normal(0, 1.5, size=n_bars))

    def apply_macro_gold_shock(i0: int, dxy_delta: float, yld_delta: float,
                               gold_delta: float, width: int = 8) -> None:
        nonlocal dxy, yld, gold_close
        for k in range(width):
            i = i0 + k
            if i >= n_bars:
                break
            frac = min(1.0, (k + 1) / 3.0)
            dxy[i] = dxy[i0] + dxy_delta * frac
            yld[i] = yld[i0] + yld_delta * frac
            gold_close[i] = gold_close[i0] + gold_delta * min(1.0, (k + 1) / 4.0)
        end = min(n_bars, i0 + width)
        if end < n_bars:
            dxy[end:] = dxy[end - 1] + np.cumsum(rng.normal(0, 0.002, size=n_bars - end))
            yld[end:] = yld[end - 1] + np.cumsum(rng.normal(0, 0.0005, size=n_bars - end))
            gold_close[end:] = gold_close[end - 1] + np.cumsum(
                rng.normal(0, 1.0, size=n_bars - end)
            )

    shock_i = n_bars // 3
    apply_macro_gold_shock(shock_i, dxy_delta=-0.08, yld_delta=-0.04, gold_delta=40.0)
    shock_j = (2 * n_bars) // 3
    apply_macro_gold_shock(shock_j, dxy_delta=0.08, yld_delta=0.04, gold_delta=-40.0)

    noise = np.abs(rng.normal(0, 0.8, size=n_bars)) + 1.0
    open_ = np.roll(gold_close, 1)
    open_[0] = gold_close[0]
    high = np.maximum(open_, gold_close) + noise
    low = np.minimum(open_, gold_close) - noise
    gold = pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": gold_close,
        "volume": rng.integers(10, 200, size=n_bars),
    }, index=idx)

    dxy_s = pd.Series(dxy, index=idx, name="dxy")
    yld_s = pd.Series(yld, index=idx, name="us10y_yield_pct")
    ry_s = pd.Series(yld - 2.0, index=idx, name="us10y_real_yield_pct")

    ev_time = idx[shock_i + 2].to_pydatetime()
    events = [
        EconomicEvent(
            event_id="SYN-CPI-1",
            event_name="US CPI",
            currency="USD",
            importance="high",
            scheduled_time_utc=ev_time,
            forecast=0.3,
            previous=0.2,
            actual=None,
            status="scheduled",
            source="synthetic",
            retrieved_at_utc=idx[0].to_pydatetime(),
        )
    ]
    return {
        "gold": gold,
        "dxy": dxy_s,
        "yields": yld_s,
        "real_yields": ry_s,
        "events": events,
    }
