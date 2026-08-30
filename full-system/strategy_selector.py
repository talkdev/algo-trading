# -*- coding: utf-8 -*-
"""
strategy_selector.py — listens for confirmed regime changes and picks the
specific strategy per spec §5:

  STRONG_SELL_VOL -> A 45d ATM straddle   | B wide iron condor
  MILD_SELL_VOL   -> C credit spreads     | D 1x2 ratio spread
  BUY_VOL         -> E long put butterfly | F reduce shorts + ATM put
  STRONG_BUY_VOL  -> G long ATM straddle  | H 25-delta backspread

Master overrides applied to all:
  1. days-to-expiry < 5  -> reject every SELL regime (theta filter)
  2. bid-ask spread > 3 pts (ATM) / > 5 pts (OTM) -> fall back to the simpler
     strategy of the pair.
"""
import json
import statistics
from dataclasses import dataclass, field

import config as C
from common_utils import adx14, atr14, ema_series, expected_move
from common_utilsimport fx
from strategies import MarketEnv
from strategies.sellers import (Short45DayStraddle, WideIronCondor,
                                CreditSpreads, RatioSpread1x2)
from strategies.buyers import (LongPutButterfly, ReduceShortsHedge,
                               LongATMStraddle, Backspread)

STRATEGY_MAP = {
    C.REGIME_STRONG_SELL: [Short45DayStraddle, WideIronCondor],
    C.REGIME_MILD_SELL: [CreditSpreads, RatioSpread1x2],
    C.REGIME_BUY: [LongPutButterfly, ReduceShortsHedge],
    C.REGIME_STRONG_BUY: [LongATMStraddle, Backspread],
}

COMPLEX_STRATEGIES = {WideIronCondor.name, LongPutButterfly.name, Backspread.name}


@dataclass
class SelectionResult:
    regime: str
    plan: object = None
    strategy_name: str = None
    rationale: str = ""
    env: MarketEnv = None


class StrategySelector:
    def __init__(self, ctx, detector):
        self.ctx = ctx
        self.detector = detector
        self.last = None

    # ------------------------------------------------------------- env build
    def build_env(self, res) -> MarketEnv:
        spot, vix = res.spot, res.vix or 0.0
        daily_closes = [b["c"] for b in res.daily]

        # ADX / ATR from 5-min bars
        adx_v = None
        if len(res.bars5) >= 30:
            ax = adx14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in res.bars5[-90:]])
            if ax:
                adx_v = ax[0]
        atr_now = atr14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in res.bars5[-30:]]) if res.bars5 else None
        atr_5d = None
        if len(res.daily) >= 6:
            atr_5d = atr14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in res.daily[-6:]])

        # 25-delta skew on near chain
        skew25 = None
        best_c = best_p = None
        for s in res.chain_near:
            if s["c"].get("delta") is not None and 0.1 < abs(s["c"]["delta"]) < 0.5:
                if best_c is None or abs(abs(s["c"]["delta"]) - 0.25) < abs(abs(best_c[0]) - 0.25):
                    best_c = (s["c"]["delta"], s["c"].get("iv"))
            if s["p"].get("delta") is not None and 0.1 < abs(s["p"]["delta"]) < 0.5:
                if best_p is None or abs(abs(s["p"]["delta"]) - 0.25) < abs(abs(best_p[0]) - 0.25):
                    best_p = (s["p"]["delta"], s["p"].get("iv"))
        if best_c and best_p and best_c[1] and best_p[1]:
            skew25 = best_p[1] - best_c[1]

        # forward vol / term spread from monthly chain
        v_fwd = term_spread = None
        if res.chain_monthly:
            ivs = []
            for s in res.chain_monthly:
                if abs(s["strike"] - spot) <= 100:
                    for k in ("c", "p"):
                        if s[k].get("iv"):
                            ivs.append(s[k]["iv"])
            if ivs:
                v_fwd = statistics.mean(ivs)
                term_spread = v_fwd - vix

        # open positions -> shorts/longs with deltas
        open_shorts, open_longs = [], []
        for pos in self.ctx.state.get_open_positions():
            try:
                legs = json.loads(pos.get("legs_json") or "[]")
            except (TypeError, ValueError):
                legs = []
            for leg in legs:
                delta = self._delta_from_chains(leg.get("instrument_key", ""), res)
                if delta is None:
                    delta = 0.0
                if leg.get("side") == "SELL":
                    open_shorts.append({"instrument_key": leg["instrument_key"],
                                        "qty": leg.get("qty", 0),
                                        "avg_price": leg.get("fill_price"),
                                        "delta": delta, "trade_id": pos["trade_id"]})
                else:
                    open_longs.append({"instrument_key": leg["instrument_key"],
                                       "qty": leg.get("qty", 0),
                                       "avg_price": leg.get("fill_price"),
                                       "delta": delta, "trade_id": pos["trade_id"]})

        gamma_ratio = self.ctx.risk.gamma_ratio(open_shorts, spot)
        portfolio_delta = sum((s.get("delta") or 0.0) * s.get("qty", 0) for s in open_shorts)

        # 200-day EMA
        ema200 = None
        if len(daily_closes) >= 200:
            e = ema_series(daily_closes, 200)
            if e:
                ema200 = e[-1]

        days_near = _days_to(res.near_expiry, res.ts) if res.near_expiry else None
        days_monthly = _days_to(res.monthly_expiry, res.ts) if res.monthly_expiry else None

        env = MarketEnv(
            spot=spot, vix=vix,
            near_expiry=res.near_expiry, monthly_expiry=res.monthly_expiry,
            chain_near=res.chain_near, chain_monthly=res.chain_monthly,
            days_to_expiry_near=days_near, days_to_expiry_monthly=days_monthly,
            scores=res.confirmed_scores, composite=res.composite, regime=res.regime,
            adx=adx_v, atr=atr_now, atr_5d=atr_5d,
            skew25=skew25, skew_flat=(abs(skew25) < 0.5 if skew25 is not None else None),
            term_spread=term_spread, v_fwd=v_fwd, vix_series=res.vix_series,
            open_shorts=open_shorts, open_longs=open_longs,
            portfolio_delta=portfolio_delta, gamma_ratio=gamma_ratio,
            lot_size=self.ctx.lot_size,
            trend_score=int(res.trend.confirmed or 0),
        )
        env.meta_ema200 = ema200
        return env

    def _delta_from_chains(self, instrument_key, res):
        parts = instrument_key.split("|")
        if len(parts) < 5:
            return None
        expiry, strike, otype = parts[2], float(parts[3]), parts[4].upper()
        chain = res.chain_near if expiry == res.near_expiry else res.chain_monthly
        for s in chain:
            if s["strike"] == strike:
                return s.get("c" if otype == "CE" else "p", {}).get("delta")
        return None

    # ------------------------------------------------------------ evaluation
    def evaluate(self, res):
        env = self.build_env(res)
        regime = res.regime
        rationale = []

        # ---- master override 1: theta filter ------------------------------
        # All SELL strategies trade the monthly expiry (30-45 DTE); the filter
        # rejects short premium when that traded contract is inside the last
        # week (gamma risk too high to be a seller).
        traded_dte = env.days_to_expiry_monthly if env.days_to_expiry_monthly is not None \
            else env.days_to_expiry_near
        if regime in (C.REGIME_STRONG_SELL, C.REGIME_MILD_SELL) and traded_dte is not None \
                and traded_dte < 5:
            rationale.append(f"theta filter: traded DTE {traded_dte:.0f} < 5 -> SELL rejected")
            regime = C.REGIME_NEUTRAL

        # ---- master override 2: liquidity filter ---------------------------
        atm_spread = self._atm_spread(env)
        if atm_spread is not None and atm_spread > 3:
            rationale.append(f"liquidity filter: ATM spread {atm_spread:.1f} pts > 3")

        strat_cls, pick_note = self._pick(env, regime)
        rationale.append(pick_note)

        if strat_cls is None:
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last

        # complex strategy rejected by liquidity -> fall back to the simpler one
        if strat_cls.name in COMPLEX_STRATEGIES and atm_spread is not None and atm_spread > 3:
            pair = STRATEGY_MAP.get(regime, [])
            fallback = next((k for k in pair if k.name not in COMPLEX_STRATEGIES), None)
            if fallback:
                strat_cls = fallback
                rationale.append(f"liquidity fallback -> {fallback.name}")

        strategy = strat_cls(self.ctx)
        ok, reason = strategy.validate(env)
        if not ok:
            pair = STRATEGY_MAP.get(regime, [])
            alt = next((k for k in pair if k.name != strat_cls.name), None)
            if alt:
                alt_strat = alt(self.ctx)
                ok2, reason2 = alt_strat.validate(env)
                if ok2:
                    strategy, strat_cls = alt_strat, alt
                    rationale.append(f"primary invalid ({reason}) -> alt {alt.name}")
                else:
                    rationale.append(f"primary invalid ({reason}); alt invalid ({reason2})")
                    self.last = SelectionResult(regime=regime,
                                                rationale=" | ".join(rationale), env=env)
                    return self.last
            else:
                rationale.append(f"no alternative for invalid {strat_cls.name}: {reason}")
                self.last = SelectionResult(regime=regime,
                                            rationale=" | ".join(rationale), env=env)
                return self.last

        # size + build
        try:
            probe = strategy.build_plan(env, lots=1)
        except ValueError as e:
            rationale.append(f"plan rejected: {e}")
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last
        existing_risk = self._existing_risk_rupees(env)
        lots = self.ctx.risk.suggest_lots(probe, existing_risk)
        try:
            plan = strategy.build_plan(env, lots=lots)
        except ValueError as e:
            rationale.append(f"plan rejected: {e}")
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last

        # post-build viability checks (min credit etc.)
        reason2 = self._post_checks(plan)
        if reason2:
            rationale.append(reason2)
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last

        self.last = SelectionResult(regime=regime, plan=plan, strategy_name=strategy.name,
                                    rationale=" | ".join(rationale), env=env)
        return self.last

    # ------------------------------------------------------------------ pick
    def _pick(self, env, regime):
        if regime == C.REGIME_STRONG_SELL:
            dte = env.days_to_expiry_monthly or 0
            if dte > 30:
                return WideIronCondor, f"STRONG_SELL: DTE {dte:.0f} > 30 tie-break -> Wide Iron Condor"
            adx = env.adx or 0
            atr_contracting = env.atr is not None and env.atr_5d is not None and env.atr < env.atr_5d
            skew_asym = env.skew25 is not None and abs(env.skew25) >= 2.0
            if adx < C.RANGE_ADX and atr_contracting:
                return Short45DayStraddle, "STRONG_SELL: ADX<22 & ATR contracting -> ATM Straddle"
            if (C.RANGE_ADX <= adx <= 28) or skew_asym:
                return WideIronCondor, f"STRONG_SELL: ADX {adx:.0f} 22-28 or skew asym -> Iron Condor"
            return Short45DayStraddle, "STRONG_SELL: default -> ATM Straddle"

        if regime == C.REGIME_MILD_SELL:
            if env.skew25 is not None and env.skew25 >= 2.0:
                return CreditSpreads, f"MILD_SELL: skew steep ({env.skew25:+.1f}%) -> Credit Spreads"
            if env.skew_flat and env.term_spread is not None and env.term_spread > 1.5:
                return RatioSpread1x2, f"MILD_SELL: skew flat & contango {env.term_spread:.1f} -> Ratio Spread"
            return CreditSpreads, "MILD_SELL: default -> Credit Spreads"

        if regime == C.REGIME_BUY:
            ema200 = getattr(env, "meta_ema200", None)
            if env.open_shorts and env.gamma_ratio > C.GAMMA_LIMIT_PCT:
                return ReduceShortsHedge, f"BUY_VOL: shorts & gamma {env.gamma_ratio:.0%} > 50% -> Reduce+ATM Put"
            if not env.open_shorts and ema200 is not None and env.spot > ema200:
                return LongPutButterfly, "BUY_VOL: flat book & spot>200EMA -> Long Put Butterfly"
            if not env.open_shorts:
                return LongPutButterfly, "BUY_VOL: flat book -> Long Put Butterfly"
            return None, "BUY_VOL: shorts exist but gamma <= 50% -> hold, tighten stops only"

        if regime == C.REGIME_STRONG_BUY:
            if env.trend_score == 0:
                return LongATMStraddle, "STRONG_BUY: Trend neutral -> Long ATM Straddle (3d)"
            if env.skew_flat:
                return Backspread, f"STRONG_BUY: Trend {env.trend_score:+d} & skew flat -> Backspread"
            return LongATMStraddle, "STRONG_BUY: skew not flat -> Long ATM Straddle (3d)"

        return None, f"{regime}: no new entries (hold / defensive)"

    # --------------------------------------------------------------- helpers
    def _atm_spread(self, env):
        if not env.chain_near:
            return None
        best = min(env.chain_near, key=lambda s: abs(s["strike"] - env.spot))
        spreads = []
        for k in ("c", "p"):
            leg = best.get(k) or {}
            if leg.get("bid") and leg.get("ask"):
                spreads.append(leg["ask"] - leg["bid"])
        return max(spreads) if spreads else None

    def _existing_risk_rupees(self, env):
        total = 0.0
        for pos in self.ctx.state.get_open_positions():
            meta = json.loads(pos.get("meta_json") or "{}")
            total += float(meta.get("risk_rupees") or 0.0)
        return total

    def _post_checks(self, plan):
        if plan.strategy_name in ("WIDE_IRON_CONDOR", "BULL_PUT_BEAR_CALL") \
                and plan.total_credit < C.MIN_CREDIT * max(plan.meta.get("lots", 1), 1):
            return f"credit {plan.total_credit:.0f} < MIN_CREDIT {C.MIN_CREDIT}"
        if plan.strategy_name == "LONG_PUT_BUTTERFLY" and plan.meta.get("net_debit_per_lot", 0) > C.BUTTERFLY_MAX_DEBIT:
            return f"butterfly debit {plan.meta['net_debit_per_lot']:.1f} > {C.BUTTERFLY_MAX_DEBIT}"
        return None


def _days_to(iso_date, now):
    from datetime import datetime
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
        return (d - now.date()).days
    except (TypeError, ValueError):
        return None
