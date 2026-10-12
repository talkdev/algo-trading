"""
Decision engine — ordered entry gates.

Signal ≠ order. Only an approved instruction leaves this module.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from market_signals import MarketSignal
from news_assessment import NewsAssessment, news_supports_side

UTC = timezone.utc
log = logging.getLogger("gold_news.decision_gates")


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class DecisionResult:
    # Spec vocabulary: BUY|SELL|WAIT|HOLD|EXIT|HALT_DATA|HALT_RISK|BUY_READY|SELL_READY
    action: str = "WAIT"
    reason: str = ""
    side: str = ""
    signal_id: str = ""
    system_state: str = "WAIT"
    gates: list[GateResult] = field(default_factory=list)
    timestamp_utc: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "side": self.side,
            "signal_id": self.signal_id,
            "system_state": self.system_state,
            "timestamp_utc": self.timestamp_utc,
            "gates": [asdict(g) for g in self.gates],
            "meta": self.meta,
        }


def _gate(name: str, passed: bool, detail: str = "") -> GateResult:
    return GateResult(name=name, passed=passed, detail=detail)


def evaluate_entry_gates(
    *,
    now_utc: datetime,
    signal: MarketSignal,
    news: NewsAssessment,
    session_ok: bool,
    session_detail: str = "",
    mcx_data_valid: bool,
    data_detail: str = "",
    event_block_active: bool,
    event_detail: str = "",
    broker_ok: bool = True,
    broker_detail: str = "",
    risk_ok: bool = True,
    risk_detail: str = "",
    emergency_halt: bool = False,
    kill_switch: bool = False,
    last_signal_id: str = "",
    require_news_confirmation: bool = True,
    allow_range_regime: bool = False,
) -> DecisionResult:
    """
    Evaluate gates in plan order. First failure → WAIT/HALT with reason.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    result = DecisionResult(timestamp_utc=now_utc.isoformat())
    result.signal_id = signal.signal_id or ""
    result.meta["signal"] = signal.to_dict()
    result.meta["news"] = news.to_dict()

    def fail(action: str, reason: str, system_state: str = "") -> DecisionResult:
        result.action = action
        result.reason = reason
        result.system_state = system_state or action
        # Human-readable audit line (e.g. WAIT — bullish trend, no breakout)
        result.meta["decision_line"] = f"{action} — {reason}"
        return result

    # 0) Hard safety
    if kill_switch or emergency_halt:
        result.gates.append(_gate("SAFETY", False, "kill_switch_or_halt"))
        return fail("HALT_RISK", "safety_halt", "HALT_RISK")

    # 1) Session
    g = _gate("SESSION", session_ok, session_detail or ("ok" if session_ok else "outside_session"))
    result.gates.append(g)
    if not g.passed:
        return fail("WAIT", "session_gate — outside entry window or flatten cutoff", "SESSION_CLOSED")

    # 2) Data
    g = _gate("DATA", mcx_data_valid and signal.data_valid,
              data_detail or ("ok" if mcx_data_valid else "mcx_invalid"))
    result.gates.append(g)
    if not g.passed:
        return fail("HALT_DATA", f"data_gate — {data_detail or 'mcx invalid/stale'}", "HALT_DATA")

    # 3) Market (trend + breakout + regime)
    market_ok = bool(signal.valid and signal.direction in ("bullish", "bearish"))
    if signal.regime == "abnormal" or (signal.regime == "range" and not allow_range_regime):
        market_ok = False
    market_detail = (
        f"dir={signal.direction} trend={signal.trend} brk={signal.breakout} regime={signal.regime}"
    )
    g = _gate("MARKET", market_ok, market_detail)
    result.gates.append(g)
    if not g.passed:
        why = "no completed 5-minute breakout" if signal.breakout == "none" else "trend/breakout disagree or regime blocks"
        return fail(
            "WAIT",
            f"market_gate — trend={signal.trend}, {why}",
            "WAIT",
        )

    side = "BUY" if signal.direction == "bullish" else "SELL"

    # 4) News confirmation
    if require_news_confirmation:
        news_ok = news_supports_side(news, side)
        detail = (
            f"bias={news.bias} score={news.score:.2f} fresh={news.fresh} "
            f"conflict={news.conflicting}"
        )
        g = _gate("NEWS", news_ok, detail)
        result.gates.append(g)
        if not g.passed:
            return fail("WAIT", f"news_gate — {detail}", "WAIT")
    else:
        result.gates.append(_gate("NEWS", True, "news_gate_disabled"))

    # 5) Event blackout
    g = _gate("EVENT", not event_block_active, event_detail or ("clear" if not event_block_active else "blackout"))
    result.gates.append(g)
    if not g.passed:
        return fail("WAIT", f"event_gate — {event_detail or 'blackout'}", "WAIT")

    # 6) Execution / broker
    g = _gate("EXECUTION", broker_ok, broker_detail or ("ok" if broker_ok else "broker_unhealthy"))
    result.gates.append(g)
    if not g.passed:
        return fail("HALT_RISK", f"execution_gate — {broker_detail or 'broker unhealthy'}", "HALT_RISK")

    # 7) Risk
    g = _gate("RISK", risk_ok, risk_detail or ("ok" if risk_ok else "risk_blocked"))
    result.gates.append(g)
    if not g.passed:
        return fail("WAIT", f"risk_gate — {risk_detail or 'blocked'}", "HALT_RISK" if "daily" in (risk_detail or "") else "WAIT")

    # 8) Duplicate signal
    dup = bool(last_signal_id and signal.signal_id and last_signal_id == signal.signal_id)
    g = _gate("DUPLICATE", not dup, f"last={last_signal_id or '-'} cur={signal.signal_id or '-'}")
    result.gates.append(g)
    if not g.passed:
        return fail("WAIT", "duplicate_signal — already acted this candle", "WAIT")

    ready = "BUY_READY" if side == "BUY" else "SELL_READY"
    result.action = side
    result.side = side
    result.system_state = ready
    result.reason = "all_gates_passed"
    result.meta["decision_line"] = f"{ready} — all long/short entry conditions passed"
    return result
