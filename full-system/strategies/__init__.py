# -*- coding: utf-8 -*-
"""
strategies/__init__.py — strategy base classes, leg/plan models and the
MarketEnv snapshot passed to every strategy module.

The eight spec strategies live in `sellers.py` (premium-selling) and
`buyers.py` (premium-buying / defensive overlays).
"""
from dataclasses import dataclass, field

import config as C
from common_utils import bs_delta


# ---------------------------------------------------------------------------
# Execution primitives
# ---------------------------------------------------------------------------
@dataclass
class Leg:
    instrument_key: str
    side: str                # BUY / SELL (broker view)
    qty: int                 # in lots (option lots)
    kind: str                # 'long' (debit/hedge) | 'short' (credit/core)
    role: str = "core"       # 'core' | 'hedge' | 'wing'
    slippage_bps: float = 100.0  # default: hedge-grade slip (core shorts -> 20)
    order_id: str = None
    fill_price: float = None
    status: str = "PENDING"  # PENDING / COMPLETE / PARTIAL / REJECTED / CANCELLED

    def __post_init__(self):
        # core shorts get the cheap 20 bps slip; everything long gets 100 bps
        if self.kind == "short" and self.role == "core":
            self.slippage_bps = 20.0
        elif self.kind == "short":
            self.slippage_bps = 100.0
        else:
            self.slippage_bps = 100.0

    @property
    def strike(self):
        try:
            return float(self.instrument_key.split("|")[3])
        except (IndexError, ValueError):
            return None

    @property
    def option_type(self):
        try:
            return self.instrument_key.split("|")[4].upper()
        except IndexError:
            return None

    @property
    def expiry(self):
        try:
            return self.instrument_key.split("|")[2]
        except IndexError:
            return None


@dataclass
class TradePlan:
    strategy_name: str
    legs: list = field(default_factory=list)
    expiry_date: str = None
    days_to_expiry: float = None
    total_credit: float = 0.0     # premium received (positive) at plan prices
    total_debit: float = 0.0      # premium paid (positive) at plan prices
    max_risk_points: float = 0.0  # worst-case loss per lot (points)
    lot_size: int = C.NIFTY_LOT_SIZE
    meta: dict = field(default_factory=dict)
    exit_rules: dict = field(default_factory=dict)
    # exit_rules keys: profit_target (pts), static_stop_up/down (spot),
    # time_exit_days, hold_days, trail_start, trail_retain, adjustments

    @property
    def net_premium(self):
        return self.total_credit - self.total_debit


# ---------------------------------------------------------------------------
# Market environment
# ---------------------------------------------------------------------------
@dataclass
class MarketEnv:
    spot: float
    vix: float
    near_expiry: str
    monthly_expiry: str
    chain_near: list
    chain_monthly: list
    days_to_expiry_near: float
    days_to_expiry_monthly: float
    scores: dict                  # confirmed module scores
    composite: float
    regime: str
    adx: float = None
    atr: float = None
    atr_5d: float = None
    skew25: float = None          # IV(25d put) - IV(25d call)
    skew_flat: bool = None
    term_spread: float = None     # V_fwd - V_spot
    v_fwd: float = None
    vix_series: list = field(default_factory=list)
    open_shorts: list = field(default_factory=list)
    open_longs: list = field(default_factory=list)
    portfolio_delta: float = 0.0
    gamma_ratio: float = 0.0      # gamma exposure / risk limit (0..1+)
    lot_size: int = C.NIFTY_LOT_SIZE
    trend_score: int = 0

    def chain_side(self, chain, optype):
        """-> [(strike, leg), ...] for a given side of a chain."""
        if not chain:
            return []
        out = []
        for s in chain:
            leg = s.get("c" if optype == "CE" else "p")
            if leg and leg.get("oi") is not None and leg.get("bid") and leg.get("ask"):
                out.append((s["strike"], leg))
        return out

    def side(self, optype):
        """Near-expiry chain side."""
        return self.chain_side(self.chain_near, optype)

    def side_monthly(self, optype):
        return self.chain_side(self.chain_monthly, optype)

    def strike_by_delta(self, optype, target_delta, monthly=False):
        side = self.side_monthly(optype) if monthly else self.side(optype)
        tgt = abs(target_delta) * (1 if optype == "CE" else -1)
        best, best_d = None, None
        for strike, leg in side:
            d = leg.get("delta")
            if d is None or not (0.01 < abs(d) < 0.99):
                continue
            if optype == "CE" and d <= 0:
                continue
            if optype == "PE" and d >= 0:
                continue
            diff = abs(d - tgt)
            if best_d is None or diff < best_d:
                best, best_d = strike, diff
        return best

    def leg_at(self, chain, strike, optype):
        if not chain:
            return None
        for s in chain:
            if s["strike"] == strike:
                return s.get("c" if optype == "CE" else "p")
        return None

    def near_leg(self, strike, optype):
        return self.leg_at(self.chain_near, strike, optype)

    def monthly_leg(self, strike, optype):
        return self.leg_at(self.chain_monthly, strike, optype)

    def spread(self, strike, optype, monthly=False):
        leg = self.monthly_leg(strike, optype) if monthly else self.near_leg(strike, optype)
        if not leg:
            return None
        return (leg.get("ask"), leg.get("bid"))


# ---------------------------------------------------------------------------
# Strategy base
# ---------------------------------------------------------------------------
class BaseStrategy:
    name = "BASE"
    regime = None

    def __init__(self, ctx):
        self.ctx = ctx            # ExecutionContext (feed, broker, clock, state, loggers, risk)

    def validate(self, env: MarketEnv):
        """-> (ok: bool, reason: str)."""
        return True, ""

    def build_plan(self, env: MarketEnv, lots: int = 1) -> TradePlan:
        raise NotImplementedError

    def make_key(self, expiry, strike, optype):
        return f"NSE_FO|NIFTY|{expiry}|{strike:.0f}|{optype}"


# ---------------------------------------------------------------------------
# shared cost helpers
# ---------------------------------------------------------------------------
def credit_for(env: MarketEnv, expiry, legs_spec):
    """legs_spec: [(strike, optype, 'buy'|'sell'), ...] on the given expiry.
    Returns (credit, debit). Buys priced at ask, sells at bid."""
    credit = debit = 0.0
    for strike, optype, action in legs_spec:
        leg = env.leg_at(env.chain_monthly if expiry == env.monthly_expiry else env.chain_near,
                         strike, optype)
        if not leg:
            return None, None
        if action == "sell":
            credit += (leg.get("bid") or 0.0)
        else:
            debit += (leg.get("ask") or 0.0)
    return credit, debit
