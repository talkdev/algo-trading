# -*- coding: utf-8 -*-
"""
strategies/buyers.py — the four premium-buying / defensive strategies (spec):

  E Long Put Butterfly (0.30 / 0.20 / 0.10 deltas)  (BUY_VOL default)
  F Reduce Shorts 60% + Long ATM Put                (BUY_VOL alternative)
  G Long 1-Month ATM Straddle, 3-day hold           (STRONG_BUY_VOL default)
  H Long 25-Delta Strangle Backspread (directional) (STRONG_BUY_VOL alternative)
"""
import statistics

import config as C
from common_utils import round_to_nearest
from strategies import BaseStrategy, TradePlan, Leg, credit_for


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def vix_above_sma(vix_series, vix, n):
    if len(vix_series) < n:
        return None
    sma = statistics.mean(vix_series[-n:])
    return vix, sma, vix > sma * 1.0


# ---------------------------------------------------------------------------
# E — Long Put Butterfly (0.30 / 0.20 / 0.10)
# ---------------------------------------------------------------------------
class LongPutButterfly(BaseStrategy):
    name = "LONG_PUT_BUTTERFLY"
    regime = C.REGIME_BUY

    def validate(self, env):
        if env.days_to_expiry_near is None or env.days_to_expiry_near > 7:
            return False, "weekly expiry needed (<= 7 DTE)"
        a = env.strike_by_delta("PE", 0.30)
        b = env.strike_by_delta("PE", 0.20)
        c = env.strike_by_delta("PE", 0.10)
        if not (a and b and c):
            return False, "0.30/0.20/0.10 put strikes not found"
        if not (abs(b - a) == abs(c - b)):
            return False, f"strikes not equidistant ({a},{b},{c})"
        return True, ""

    def build_plan(self, env, lots=1):
        a = env.strike_by_delta("PE", 0.30)
        b = env.strike_by_delta("PE", 0.20)
        c = env.strike_by_delta("PE", 0.10)
        expiry = env.near_expiry
        legs = [
            Leg(self.make_key(expiry, a, "PE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, b, "PE"), "SELL", 2 * lots, "short", "core"),
            Leg(self.make_key(expiry, c, "PE"), "BUY", lots, "long", "wing"),
        ]
        credit, debit = credit_for(env, expiry, [(b, "PE", "sell")])
        debit += (env.near_leg(a, "PE").get("ask") or 0.0)
        debit += (env.near_leg(c, "PE").get("ask") or 0.0)
        net_debit = debit - (credit or 0.0) * 2
        max_profit = (b - a) - net_debit
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_near,
            total_credit=0.0, total_debit=round(net_debit * lots, 2),
            max_risk_points=round(net_debit * lots, 2), lot_size=env.lot_size,
            meta={"wing_a": a, "body_b": b, "wing_c": c,
                  "net_debit_per_lot": round(net_debit, 2),
                  "max_profit_per_lot": round(max_profit, 2)},
            exit_rules={"time_exit_days": 2})
        return plan


# ---------------------------------------------------------------------------
# F — Reduce Shorts 60% (delta-weighted) + Long ATM Put (defensive overlay)
# ---------------------------------------------------------------------------
class ReduceShortsHedge(BaseStrategy):
    name = "REDUCE_SHORTS_PUT_HEDGE"
    regime = C.REGIME_BUY

    def validate(self, env):
        if not env.open_shorts:
            return False, "no existing short positions (use Long Put Butterfly instead)"
        if env.gamma_ratio <= C.GAMMA_LIMIT_PCT:
            return False, f"gamma exposure {env.gamma_ratio:.0%} <= 50% of limit"
        return True, ""

    def build_plan(self, env, lots=1):
        """Defensive overlay: BUY-to-close 60% (delta-weighted) of each short leg,
        then BUY ATM puts sized on the remaining delta."""
        expiry = env.near_expiry
        legs = []
        total_delta = sum(abs(s.get("delta", 0.0)) * s.get("qty", 0) for s in env.open_shorts)
        for s in env.open_shorts:
            d = abs(s.get("delta", 0.0) or 0.0)
            # spec: close_qty = min(qty, round(total_portfolio_delta * 0.60 / |delta_per_leg|))
            close_qty = max(1, min(s["qty"], int(round(total_delta * C.HEDGE_REDUCE_PCT
                                                       / max(d, 0.01)))))
            legs.append(Leg(s["instrument_key"], "BUY", close_qty, "long", "hedge",
                            slippage_bps=100.0))
        # ATM put hedge for remaining delta
        remaining = sum(s["qty"] * abs(s.get("delta", 0.0) or 0.0) for s in env.open_shorts) \
            * (1 - C.HEDGE_REDUCE_PCT)
        atm = round_to_nearest(env.spot)
        put_leg = env.near_leg(atm, "PE")
        put_delta = abs((put_leg or {}).get("delta", -0.5) or -0.5)
        hedge_qty = max(1, int(math_ceil(remaining / max(put_delta, 0.1))))
        legs.append(Leg(self.make_key(expiry, atm, "PE"), "BUY", hedge_qty, "long", "hedge",
                        slippage_bps=100.0))
        debit = 0.0
        for L in legs:
            leg_q = (env.monthly_leg(L.strike, L.option_type) if L.expiry == env.monthly_expiry
                     else env.near_leg(L.strike, L.option_type))
            if leg_q and L.strike:
                debit += (leg_q.get("ask") or 0.0) * L.qty
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_near,
            total_credit=0.0, total_debit=round(debit, 2),
            max_risk_points=0.0, lot_size=env.lot_size,
            meta={"hedge": True, "atm_put": atm, "reduction_pct": C.HEDGE_REDUCE_PCT},
            exit_rules={"time_exit_days": 3, "viX_exit_sma": C.VIX_SMA_FAST})
        return plan


def math_ceil(x):
    import math
    return math.ceil(x)


# ---------------------------------------------------------------------------
# G — Long 1-Month ATM Straddle (3-day hold)
# ---------------------------------------------------------------------------
class LongATMStraddle(BaseStrategy):
    name = "LONG_ATM_STRADDLE_3D"
    regime = C.REGIME_STRONG_BUY

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        demo = bool(getattr(self, "ctx", None) and getattr(self.ctx, "demo", False))
        lo, hi = (25, 60) if demo else (25, 40)
        if not (lo <= env.days_to_expiry_monthly <= hi):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside {lo}-{hi}"
        atm = env.strike_by_delta("CE", 0.50, monthly=True) or round_to_nearest(env.spot)
        ce, pe = env.monthly_leg(atm, "CE"), env.monthly_leg(atm, "PE")
        if not ce or not pe:
            return False, "ATM legs missing"
        debit = (ce.get("ask") or 0.0) + (pe.get("ask") or 0.0)
        if debit > env.spot * C.LONG_STRADDLE_MAX_DEBIT_PCT:
            return False, f"straddle debit {debit:.0f} > 2.5% of spot"
        # VIX risen >20% above 10-SMA in last 24h (approximation with daily series)
        if len(env.vix_series) >= 11:
            sma10 = statistics.mean(env.vix_series[-11:-1])
            if env.vix < sma10 * 1.20:
                return False, f"VIX {env.vix:.1f} not >20% above 10-SMA {sma10:.1f}"
        return True, ""

    def build_plan(self, env, lots=1):
        atm = env.strike_by_delta("CE", 0.50, monthly=True) or round_to_nearest(env.spot)
        expiry = env.monthly_expiry
        ce, pe = env.monthly_leg(atm, "CE"), env.monthly_leg(atm, "PE")
        debit = (ce.get("ask") or 0.0) + (pe.get("ask") or 0.0)
        legs = [
            Leg(self.make_key(expiry, atm, "CE"), "BUY", lots, "long", "core"),
            Leg(self.make_key(expiry, atm, "PE"), "BUY", lots, "long", "core"),
        ]
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=0.0, total_debit=round(debit * lots, 2),
            max_risk_points=round(debit * lots, 2), lot_size=env.lot_size,
            meta={"atm": atm, "debit_per_lot": round(debit, 2)},
            exit_rules={"profit_target_mult": 1.50,     # +50% on debit
                        "stop_loss_mult": 0.50,         # -50% on debit
                        "hold_days": C.STRADDLE_DAY_HOLD})
        return plan


# ---------------------------------------------------------------------------
# H — Long 25-Delta Strangle Backspread (directional)
# ---------------------------------------------------------------------------
class Backspread(BaseStrategy):
    name = "BACKSPREAD_25D"
    regime = C.REGIME_STRONG_BUY

    def validate(self, env):
        if env.days_to_expiry_near is None or not (7 <= env.days_to_expiry_near <= 10):
            return False, f"weekly expiry 7-10 DTE needed (have {env.days_to_expiry_near})"
        if env.vix > 30:
            return False, f"VIX {env.vix:.1f} > 30 — backspread too expensive"
        if env.trend_score == 0:
            return False, "Trend_Score neutral — use Long ATM Straddle instead"
        if env.skew_flat is not True:
            return False, "skew not flat (< 0.5%)"
        over = "CE" if env.trend_score > 0 else "PE"
        d25 = env.strike_by_delta(over, 0.25)
        d10 = env.strike_by_delta(over, 0.10)
        if not d25 or not d10:
            return False, "25d/10d strikes not found on overweight side"
        if abs(d25 - d10) < C.BACKSPREAD_MIN_WIDTH:
            return False, f"25d-10d width {abs(d25-d10):.0f} < 100 pts"
        return True, ""

    def build_plan(self, env, lots=1):
        over = "CE" if env.trend_score > 0 else "PE"
        under = "PE" if over == "CE" else "CE"
        expiry = env.near_expiry
        o25 = env.strike_by_delta(over, 0.25)
        o10 = env.strike_by_delta(over, 0.10)
        u25 = env.strike_by_delta(under, 0.25)
        u10 = env.strike_by_delta(under, 0.10)
        legs = [
            # longs first: 3 lots on overweight side, 1 lot underweight
            Leg(self.make_key(expiry, o25, over), "BUY", 3 * lots, "long", "core"),
            Leg(self.make_key(expiry, u25, under), "BUY", lots, "long", "core"),
            # shorts
            Leg(self.make_key(expiry, o10, over), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, u10, under), "SELL", lots, "short", "core"),
        ]
        ask_o = (env.near_leg(o25, over).get("ask") or 0.0)
        ask_u = (env.near_leg(u25, under).get("ask") or 0.0)
        bid_o = (env.near_leg(o10, over).get("bid") or 0.0)
        bid_u = (env.near_leg(u10, under).get("bid") or 0.0)
        net_debit = (3 * ask_o + ask_u) - (bid_o + bid_u)
        if net_debit <= 0 or net_debit > C.BACKSPREAD_MAX_DEBIT:
            raise ValueError(f"backspread net debit {net_debit:.1f} outside (0, 30]")
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_near,
            total_credit=0.0, total_debit=round(net_debit * lots, 2),
            max_risk_points=round(net_debit * lots, 2), lot_size=env.lot_size,
            meta={"overweight": over, "net_debit_per_lot": round(net_debit, 2),
                  "strikes": {"o25": o25, "o10": o10, "u25": u25, "u10": u10}},
            exit_rules={"profit_target_mult": 10.0,      # 10x net debit
                        "stop_move_pct": 0.015,          # 1.5% against overweight side
                        "time_exit_days": 2})
        return plan
