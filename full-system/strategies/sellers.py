# -*- coding: utf-8 -*-
"""
strategies/sellers.py — the four premium-selling strategies (spec):

  A 45-Day ATM Straddle with 10% static spot stop   (STRONG_SELL_VOL default)
  B Wide Iron Condor (300-pt wings)                 (STRONG_SELL_VOL alternative)
  C Bull Put + Bear Call Spreads (0.30 delta)       (MILD_SELL_VOL default)
  D Call/Put Ratio Spread (1x2)                     (MILD_SELL_VOL alternative)
"""
import math

import config as C
from common_utils import round_to_nearest, round_down, round_up, expected_move
from strategies import (BaseStrategy, TradePlan, Leg, credit_for)


# ---------------------------------------------------------------------------
# A — 45-day ATM straddle (short), 10% static spot stop
# ---------------------------------------------------------------------------
class Short45DayStraddle(BaseStrategy):
    name = "ATM_STRADDLE_45D"
    regime = C.REGIME_STRONG_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        demo = bool(getattr(self, "ctx", None) and getattr(self.ctx, "demo", False))
        lo, hi = (40, 60) if demo else (40, 50)
        if not (lo <= env.days_to_expiry_monthly <= hi):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside {lo}-{hi} (selector tie-break)"
        atm = env.strike_by_delta("CE", 0.50, monthly=True)
        if atm is None:
            return False, "no ATM call strike"
        spread = env.spread(atm, "CE", monthly=True)
        if spread and spread[0] - spread[1] > 3:
            return False, f"ATM call spread {spread[0]-spread[1]:.1f} pts > 3"
        return True, ""

    def build_plan(self, env, lots=1):
        atm = env.strike_by_delta("CE", 0.50, monthly=True) or round_to_nearest(env.spot)
        ce, pe = env.monthly_leg(atm, "CE"), env.monthly_leg(atm, "PE")
        if not ce or not pe:
            raise ValueError("ATM legs missing for straddle")
        credit = (ce.get("bid") or 0.0) + (pe.get("bid") or 0.0)
        expiry = env.monthly_expiry
        legs = [
            Leg(self.make_key(expiry, atm, "CE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, atm, "PE"), "SELL", lots, "short", "core"),
        ]
        max_risk = round(env.spot * C.STATIC_STOP_PCT, 2) * 2  # ~ both legs 10% ITM
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(credit * lots, 2), total_debit=0.0,
            max_risk_points=round(max_risk * lots, 2), lot_size=env.lot_size,
            meta={"atm_strike": atm, "credit_per_lot": round(credit, 2),
                  "entry_spot": env.spot, "entry_vix": env.vix},
            exit_rules={
                "static_stop_pct": C.STATIC_STOP_PCT,           # spot-based stop
                "profit_target_pct": C.PROFIT_TARGET_PCT,       # 50% of credit
                "time_exit_days": C.TIME_EXIT_DAYS_STRADDLE,    # 21 DTE
            })
        return plan


# ---------------------------------------------------------------------------
# B — Wide iron condor (300-pt wings)
# ---------------------------------------------------------------------------
class WideIronCondor(BaseStrategy):
    name = "WIDE_IRON_CONDOR"
    regime = C.REGIME_STRONG_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        if not (30 <= env.days_to_expiry_monthly <= 45):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside 30-45"
        if not (C.LOW_VIX <= env.vix <= 20.0):
            return False, f"VIX {env.vix:.1f} outside 12-20 band"
        if env.adx is not None and env.adx >= 25:
            return False, f"ADX {env.adx:.1f} >= 25 (not range-bound)"
        return True, ""

    def _strikes(self, env):
        em = expected_move(env.spot, env.vix, env.days_to_expiry_monthly)
        short_put = round_down(env.spot - 1.5 * em)
        short_call = round_up(env.spot + 1.5 * em)
        long_put = short_put - C.IRON_CONDOR_WING_WIDTH
        long_call = short_call + C.IRON_CONDOR_WING_WIDTH
        return short_put, short_call, long_put, long_call

    def build_plan(self, env, lots=1):
        sp, sc, lp, lc = self._strikes(env)
        expiry = env.monthly_expiry
        legs = [
            Leg(self.make_key(expiry, lp, "PE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, lc, "CE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, sp, "PE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, sc, "CE"), "SELL", lots, "short", "core"),
        ]
        credit = credit_for(env, expiry, [(sp, "PE", "sell"), (sc, "CE", "sell"),
                                          (lp, "PE", "buy"), (lc, "CE", "buy")])
        if credit is None or credit[0] is None:
            raise ValueError("condor legs missing (strikes outside chain)")
        credit, _ = credit
        max_risk = max(C.IRON_CONDOR_WING_WIDTH - credit, 0.0)
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(credit * lots, 2), total_debit=0.0,
            max_risk_points=round(max_risk * lots, 2), lot_size=env.lot_size,
            meta={"short_put": sp, "short_call": sc, "long_put": lp, "long_call": lc,
                  "credit_per_lot": round(credit, 2), "wing_width": C.IRON_CONDOR_WING_WIDTH},
            exit_rules={
                "profit_target_pct": C.PROFIT_TARGET_PCT,
                "time_exit_days": C.TIME_EXIT_DAYS_CONDOR,
                "adjust_test_side": True,
            })
        return plan


# ---------------------------------------------------------------------------
# C — Bull Put + Bear Call credit spreads (0.30 / 0.15 delta)
# ---------------------------------------------------------------------------
class CreditSpreads(BaseStrategy):
    name = "BULL_PUT_BEAR_CALL"
    regime = C.REGIME_MILD_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        if not (30 <= env.days_to_expiry_monthly <= 45):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside 30-45"
        if env.adx is not None and env.adx >= 25:
            return False, f"ADX {env.adx:.1f} >= 25"
        sp30 = env.strike_by_delta("PE", 0.30, monthly=True)
        sc30 = env.strike_by_delta("CE", 0.30, monthly=True)
        if not sp30 or not sc30:
            return False, "0.30-delta strikes not found"
        return True, ""

    def build_plan(self, env, lots=1):
        expiry = env.monthly_expiry
        sp30 = env.strike_by_delta("PE", 0.30, monthly=True)
        sp15 = env.strike_by_delta("PE", 0.15, monthly=True)
        sc30 = env.strike_by_delta("CE", 0.30, monthly=True)
        sc15 = env.strike_by_delta("CE", 0.15, monthly=True)
        # sanity: short closer to ATM than long (standard structure)
        if sp15 is None:
            sp15 = sp30 - 100
        if sc15 is None:
            sc15 = sc30 + 100
        legs = [
            Leg(self.make_key(expiry, sp15, "PE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, sc15, "CE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, sp30, "PE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, sc30, "CE"), "SELL", lots, "short", "core"),
        ]
        credit, debit = credit_for(env, expiry, [(sp30, "PE", "sell"), (sc30, "CE", "sell"),
                                                 (sp15, "PE", "buy"), (sc15, "CE", "buy")])
        if credit is None:
            raise ValueError("credit spread legs missing")
        width_p = abs(sp30 - sp15)
        width_c = abs(sc30 - sc15)
        # per-side credits and max risk per spec: max loss = width - credit
        put_legs = [(sp30, "PE", "sell"), (sp15, "PE", "buy")]
        call_legs = [(sc30, "CE", "sell"), (sc15, "CE", "buy")]
        pc, pd = credit_for(env, expiry, put_legs)
        cc, cd = credit_for(env, expiry, call_legs)
        put_net = (pc or 0.0) - (pd or 0.0)
        call_net = (cc or 0.0) - (cd or 0.0)
        net_credit = put_net + call_net
        max_risk = max(width_p - put_net, width_c - call_net, 0.0)
        if net_credit <= 0:
            raise ValueError(f"net credit {net_credit:.1f} <= 0 — abort")
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(net_credit * lots, 2), total_debit=0.0,
            max_risk_points=round(max_risk * lots, 2), lot_size=env.lot_size,
            meta={"short_put": sp30, "long_put": sp15, "short_call": sc30, "long_call": sc15,
                  "credit_per_lot": round(net_credit, 2),
                  "put_width": width_p, "call_width": width_c},
            exit_rules={"profit_target_pct": C.PROFIT_TARGET_PCT,
                        "time_exit_days": C.TIME_EXIT_DAYS_CONDOR,
                        "roll_delta": 0.35})
        return plan


# ---------------------------------------------------------------------------
# D — Call/Put Ratio Spread (1x2), 50-pt offset
# ---------------------------------------------------------------------------
class RatioSpread1x2(BaseStrategy):
    name = "RATIO_SPREAD_1x2"
    regime = C.REGIME_MILD_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        if not (30 <= env.days_to_expiry_monthly <= 45):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside 30-45"
        if env.skew_flat is not True:
            return False, "skew not flat (needs < 0.5% put-call IV diff)"
        if env.term_spread is None or env.term_spread <= 1.5:
            return False, f"contango {env.term_spread:.2f} not > 1.5 pts"
        if env.adx is not None and env.adx > 25:
            return False, f"ADX {env.adx:.1f} > 25"
        return True, ""

    def build_plan(self, env, lots=1):
        expiry = env.monthly_expiry
        atm = round_to_nearest(env.spot)
        legs = [
            # longs first per spec sequence (buyer of the wing)
            Leg(self.make_key(expiry, atm + 50, "CE"), "BUY", 2 * lots, "long", "wing"),
            Leg(self.make_key(expiry, atm - 50, "PE"), "BUY", 2 * lots, "long", "wing"),
            Leg(self.make_key(expiry, atm, "CE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, atm, "PE"), "SELL", lots, "short", "core"),
        ]
        credit, debit = credit_for(env, expiry, [(atm, "CE", "sell"), (atm, "PE", "sell"),
                                                 (atm + 50, "CE", "buy"), (atm - 50, "PE", "buy")])
        if credit is None:
            raise ValueError("ratio spread legs missing")
        net = credit - 2 * debit
        if net <= 0:
            raise ValueError("net debit ratio spread — abort")
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(credit * lots, 2), total_debit=round(2 * debit * lots, 2),
            max_risk_points=round(0.05 * env.spot * lots, 2), lot_size=env.lot_size,
            meta={"atm": atm, "net_per_lot": round(net, 2)},
            exit_rules={"profit_target_pct": 0.40,
                        "time_exit_days": C.RATIO_TIME_EXIT_DAYS,
                        "close_side_on_delta": 0.35})
        return plan
