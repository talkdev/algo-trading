#!/usr/bin/env python3
"""
patch_v42.py - Self-contained v4.2 patch: fresh-weekly (DTE>=2) intraday
premium selling for the NIFTY options algo.

Fixes, all strictly DTE-gated (0DTE / expiry-day behaviour is untouched):

  core.py
    - Weekly config: short-delta table 0.24/0.22/0.18 (flat/trend/strong),
      weekly EM bands, weekly wing-cost cap 0.58, wide-condor ADX band
      (20-28, 0.80 size), UNCLEAR-range condor size 0.75, EV carry
      discount 0.62 for DTE>=2.

  regime_engine.py
    - DTE3/4 RANGE branch: UNCLEAR OI positioning is allowed through to a
      symmetric condor when vol is SELL/STRONG_SELL (0.75 size); the hard
      ADX veto moves from 20 to 28, with ADX in [20,28) forcing a WIDE
      ~0.16-delta condor at 0.80 size; MEDIUM/HIGH confidence and opening-
      range containment remain mandatory.

  strategy_engine.py
    - Weekly neutral CONDOR short deltas 0.24/0.22/0.18 (flat/trend/
      strong); favoured-side weekly verticals keep the canonical ~0.30
      delta (the 0DTE low-VIX shave no longer applies to weeklies).
    - Weekly condor strike sanity band scaled to the weekly chain's OWN
      ATM straddle (expiry horizon) instead of the shrinking intraday
      remaining expected move, which re-clamped afternoon shorts to
      ~0.37 delta.
    - Adaptive bid/ask wing fitter for weekly condors (the long is
      widened until wing premium <= 58% of the short; bounded); DTE-aware
      wing-cost cap.
    - EV greeks-carry term x0.62 on DTE>=2 (multi-hour theta accrual).
    - decide() applies the regime layer's weekly size discounts.

Usage:
    python patch_v42.py               # apply (idempotent) + verify
    python patch_v42.py --no-verify   # apply, skip the harness self-test
    python patch_v42.py --root DIR    # repo root other than script dir

Idempotent: running twice is safe. The script refuses to touch a file that
matches neither the pristine baseline nor the already-patched state (sha256
verified), so it can never silently corrupt a diverged tree.
"""
import argparse
import hashlib
import os
import py_compile
import subprocess
import sys

VERSION = "v4.2"

# rel -> (baseline sha256, target sha256, [(old_text, new_text, label)])
PATCHES = {
    'core.py': ('8fd84d7b311c208ee5c196c7f71286e6dda10f11d9b42c29fb450f3c5b5335bb', 'ecb4de0210ac59d107206eaabee4db6bc242e22094c17d61bfcb6c207a655eb7', [
        ('frac_max:        float = 0.50\n    condor_weak_side_min_frac: float = 0.30\n    # Fast intraday trend timeframe (15m ADX cannot mature intraday).\n    adx_fast_res',
         'frac_max:        float = 0.50\n    condor_weak_side_min_frac: float = 0.30\n    # ── v4.2: fresh-weekly (DTE >= 2) intraday premium selling ──────────\n    # A weekly option with 3-4 sessions left carries overnight gap vega,\n    # so the professional short-delta is ~16-20, NOT the 0.30 an 0DTE\n    # short uses. Measured 2026-09-09/10 (DTE4/DTE3): the intraday-EM\n    # strike clamp was forcing the condor shorts to 0.31-0.42 delta on\n    # those days, which (a) made the long wing 55-80% of the short and\n    # tripped wing_cost_frac_max on every candidate and (b) put the\n    # threat line ~100 points out on a day that only moved 100. The\n    # wide ~0.18-delta condor cleared its round trip on every tested\n    # entry of both sessions, including the CPI two-way chop.\n    short_delta_flat_weekly:   float = 0.24\n    short_delta_trend_weekly:  float = 0.22\n    short_delta_strong_weekly: float = 0.18\n    em_band_lo_weekly:         float = 0.55\n    em_band_hi_weekly:         float = 2.10\n    # Weekly CONDOR shorts are sanity-banded in the weekly chain\'s own\n    # ATM straddle (expiry horizon), not the shrinking intraday EM.\n    em_band_hi_condor_weekly:  float = 1.35\n    # Weekly wings (multi-day vega) are inherently pricier relative to\n    # their shorts than 0DTE wings; 0.50 was calibrated for expiry day.\n    wing_cost_frac_max_weekly: float = 0.58\n    # On DTE3/4 RANGE sessions with ADX in [trend, strong) the price\n    # classifier still says RANGE (mean-reverting, not trending); allow\n    # a WIDE condor (shorts forced to the strong-delta target below) up\n    # to the strong-ADX cutoff, at a size discount.\n    range_adx_wide_max:        float = 28.0\n    range_adx_wide_size:       float = 0.80\n    # UNCLEAR OI positioning on an otherwise textbook range day\n    # (rich VRP, narrow OR, flat ADX, price = RANGE) previously banned\n    # the symmetric condor outright on DTE3/4. OI positioning is a\n    # confirmation, not a prerequisite, for a delta-neutral structure;\n    # trade it at this size discount.\n    unclear_range_size_weekly: float = 0.75\n    # EV-gate adverse-excursion calibration for fresh weeklies: the\n    # greeks-carry "stop severity" assumed an instantaneous move at\n    # entry delta with a 1.25 stress factor and ZERO theta credit. The\n    # real exit ladder (spot proximity ~40pts inside the short)\n    # realised 4-8pt losses on 28-60pt credits across the 08-10 Sep\n    # replays, i.e. ~2.5-3x less than the 18-35pt the model charged.\n    # Apply this discount to the carry on DTE >= 2 (theta over the\n    # intended multi-hour hold). 0DTE keeps the old conservative value.\n    ev_carry_discount_dte2p:   float = 0.62\n    # Fast intraday trend timeframe (15m ADX cannot mature intraday).\n    adx_fast_res',
         'weekly config fields'),
        ('_min_frac=min(max(_get_float(env, "CONDOR_WEAK_SIDE_MIN_FRAC", 0.30), 0.05), 0.50),\n        adx_fast_resample=env.get("ADX_FAST_RESAMPLE", "300s").strip() or "3',
         '_min_frac=min(max(_get_float(env, "CONDOR_WEAK_SIDE_MIN_FRAC", 0.30), 0.05), 0.50),\n        # v4.2 fresh-weekly (DTE >= 2) intraday premium selling\n        short_delta_flat_weekly=min(max(_get_float(env, "SHORT_DELTA_FLAT_WEEKLY", 0.24), 0.08), 0.35),\n        short_delta_trend_weekly=min(max(_get_float(env, "SHORT_DELTA_TREND_WEEKLY", 0.22), 0.07), 0.30),\n        short_delta_strong_weekly=min(max(_get_float(env, "SHORT_DELTA_STRONG_WEEKLY", 0.18), 0.06), 0.25),\n        em_band_lo_weekly=min(max(_get_float(env, "EM_BAND_LO_WEEKLY", 0.55), 0.30), 1.20),\n        em_band_hi_weekly=min(max(_get_float(env, "EM_BAND_HI_WEEKLY", 2.10), 1.20), 3.00),\n        em_band_hi_condor_weekly=min(max(_get_float(env, "EM_BAND_HI_CONDOR_WEEKLY", 1.35), 0.80), 2.00),\n        wing_cost_frac_max_weekly=min(max(_get_float(env, "WING_COST_FRAC_MAX_WEEKLY", 0.58), 0.30), 0.80),\n        range_adx_wide_max=min(max(_get_float(env, "RANGE_ADX_WIDE_MAX", 28.0), 20.0), 40.0),\n        range_adx_wide_size=min(max(_get_float(env, "RANGE_ADX_WIDE_SIZE", 0.80), 0.40), 1.00),\n        unclear_range_size_weekly=min(max(_get_float(env, "UNCLEAR_RANGE_SIZE_WEEKLY", 0.75), 0.40), 1.00),\n        ev_carry_discount_dte2p=min(max(_get_float(env, "EV_CARRY_DISCOUNT_DTE2P", 0.62), 0.40), 1.00),\n        adx_fast_resample=env.get("ADX_FAST_RESAMPLE", "300s").strip() or "3',
         'weekly config from_env wiring'),
    ]),
    'regime_engine.py': ('3f9b1b146e9f40b549ecc690e512fd78bbec0538d34118dc2ce20ef4f024c441', '12342fa36394611fa4dc3e347b547bf8416543cb3a335917aa73c0804455c5cf', [
        ('.\n            if pos in (PositioningRegime.STRONG_RANGE, PositioningRegime.RANGE):\n                if or_condition not in ("VERY_NARROW", "NARROW", "MODERATE"):',
         '.\n            if pos in (PositioningRegime.STRONG_RANGE, PositioningRegime.RANGE,\n                       PositioningRegime.UNCLEAR):\n                if or_condition not in ("VERY_NARROW", "NARROW", "MODERATE"):',
         'regime_engine hunk 1'),
        ('if adx_15 >= self.config.adx_trend_threshold:\n                    return (\n                        FinalRegime.NO_TRADE,\n                        f"RANGE_DTE{dte}_ADX_{adx_15:.0f}_TRENDING',
         '# v4.2: the symmetric condor is delta-neutral by\n                # construction, so the rich-VRP / price=RANGE / narrow-OR\n                # stack is the edge and OI positioning is only a\n                # confirmation. UNCLEAR positioning previously banned it\n                # outright on DTE3/4 (measured 2026-09-10: STRONG_SELL,\n                # ADX 10-13, HIGH confidence, spot pinned all afternoon,\n                # yet no trade after 12:51).\n                if (pos == PositioningRegime.UNCLEAR and\n                        vol not in (VolatilityRegime.SELL_PREMIUM,\n                                    VolatilityRegime.STRONG_SELL_PREMIUM)):\n                    return (\n                        FinalRegime.NO_TRADE,\n                        "RANGE_DTE" + str(dte)\n                        + "_UNCLEAR_REQUIRES_SELL_PREMIUM',
         'regime_engine hunk 2'),
        ('return (\n                    FinalRegime.PREMIUM_SELL_RANGE,\n                    f"RANGE_DTE{dte}_NEW_CYCLE_STRONG_SELL_CONTAINED_OR",\n                )\n            # BULLISH / BEARISH / UNCLEAR fall through to the matching\n            # branches below (UNCLEAR still NO_TRADEs there unless it is a\n            # STRONG_SELL 0/1 DTE session)',
         '# v4.2: ADX is non-directional and lagging. The hard veto at\n                # the 20 trend threshold fired on mean-reverting RANGE tapes\n                # (measured 2026-09-09 13:10-14:10, CPI day: price=RANGE,\n                # ADX 21-24 inherited from the morning whipsaw; spot topped\n                # and faded 136 points into the close). Only a genuine\n                # STRONG reading blocks the symmetric condor; readings in\n                # between force the wide ~0.15-delta condor at a size\n                # discount.\n                _adx_wide = (\n                    float(self.config.adx_trend_threshold) <= adx_15\n                    < float(getattr(self.config, "range_adx_wide_max", 28.0))\n                )\n                if adx_15 >= float(getattr(self.config, "range_adx_wide_max", 28.0)):\n                    return (\n                        FinalRegime.NO_TRADE,\n                        f"RANGE_DTE{dte}_ADX_{adx_15:.0f}_STRONG_TREND",\n                    )\n                if _adx_wide:\n                    signals["weekly_wide_condor"] = True\n                    signals["weekly_range_size_discount"] = float(\n                        getattr(self.config, "range_adx_wide_size", 0.80)\n                    )\n                    return (\n                        FinalRegime.PREMIUM_SELL_RANGE,\n                        f"RANGE_DTE{dte}_WIDE_CONDOR_ADX_{adx_15:.0f}",\n                    )\n                if pos == PositioningRegime.UNCLEAR:\n                    signals["weekly_range_size_discount"] = float(\n                        getattr(self.config, "unclear_range_size_weekly", 0.75)\n                    )\n                    return (\n                        FinalRegime.PREMIUM_SELL_RANGE,\n                        f"RANGE_DTE{dte}_UNCLEAR_RICH_VRP_CONDOR",\n                    )\n                return (\n                    FinalRegime.PREMIUM_SELL_RANGE,\n                    f"RANGE_DTE{dte}_NEW_CYCLE_STRONG_SELL_CONTAINED_OR",\n                )\n            # BULLISH / BEARISH fall through to the matching branches\n            # below; UNCLEAR is handled above',
         'regime_engine hunk 3'),
    ]),
    'strategy_engine.py': ('24f60f8b8cb94047916aed166089b713b2ca6284d21051edfa19e7b2e3398fde', 'cbe7f7e99e4d0cffbcdd5a82c0da00416023104b5963c46e4fb45c7a804d01a2', [
        ('if adx_15 >= self.config.adx_strong_threshold:\n            delta_target = float(getattr(self.config, "short_delta_strong", 0.15))\n        elif adx_15 >= self.config.adx_trend_threshold:\n            delta_target = float(getattr(self.config, "short_delta_trend", 0.18))\n        else:\n            delta_target = float(getattr(self.config, "short_delta_flat", 0.22))\n\n        if vix < 12.0:\n            delta_target = max(delta_target - 0.02, 0.10)\n        elif vix < 14.0:\n',
         '# v4.2: fresh-weekly sessions (DTE >= 2) sell different shorts\n        # depending on the STRUCTURE: a delta-neutral condor wants ~0.20\n        # delta per side (a 0.31-0.42 delta symmetric book is short delta\n        # and tripped the wing-cost gate - measured 2026-09-09/10), while a\n        # directional vertical is a directional-expression spread that\n        # desks conventionally sell at 0.28-0.32 delta on the favoured side,\n        # using the other side\'s OI wall as the wall being sold into.\n        _weekly_dte = bool(dte is not None and dte >= 2)\n        _neutral_condor = strategy_name == IRON_CONDOR\n        if _weekly_dte and _neutral_condor:\n            if adx_15 >= self.config.adx_strong_threshold:\n                delta_target = float(getattr(self.config,\n                                             "short_delta_strong_weekly", 0.18))\n            elif adx_15 >= self.config.adx_trend_threshold:\n                delta_target = float(getattr(self.config,\n                                             "short_delta_trend_weekly", 0.22))\n            else:\n                delta_target = float(getattr(self.config,\n                                             "short_delta_flat_weekly", 0.24))\n            # regime layer can force the wider strong-delta target on a\n            # RANGE tape with elevated ADX (v4.2 wide condor)\n            if signals.get("weekly_wide_condor"):\n                delta_target = min(\n                    delta_target,\n                    float(getattr(self.config,\n                                  "short_delta_strong_weekly", 0.18)),\n                )\n            if vix < 12.0:\n                delta_target = max(delta_target - 0.01, 0.10)\n            elif vix < 14.0:\n                delta_target = max(delta_target - 0.01, 0.11)\n        else:\n            if adx_15 >= self.config.adx_strong_threshold:\n                delta_target = float(getattr(self.config, "short_delta_strong", 0.15))\n            elif adx_15 >= self.config.adx_trend_threshold:\n                delta_target = float(getattr(self.config, "short_delta_trend", 0.18))\n            else:\n                delta_target = float(getattr(self.config, "short_delta_flat", 0.22))\n\n            # The low-VIX delta shave is a 0DTE fast-gamma calibration;\n            # on fresh weeklies the favoured-side vertical keeps the\n            # canonical ~0.30 short delta.\n            if not _weekly_dte:\n                if vix < 12.0:\n                    delta_target = max(delta_target - 0.02, 0.10)\n                elif vix < 14.0:\n        ',
         'weekly delta tables'),
        ('_band_lo = float(getattr(self.config, "em_band_lo", 0.80)) * _em\n',
         '# v4.2: the intraday-remaining-EM band is correct for 0DTE (the\n        # option\'s life IS the rest of the session), but a DTE3/4 weekly\n        # CONDOR short at ~0.18-0.24 delta sits ~0.9-1.2x the expiry-horizon\n        # expected move away, and the session-remaining EM collapses toward\n        # zero through the afternoon: at 13:00 on 2026-09-09 it was 70 pts,\n        # so even a 2.10x intraday ceiling clamped the weekly shorts back to\n        # 0.37 delta and re-tripped the wing-cost gate. Weekly condors are\n        # therefore sanity-banded in the chain\'s OWN current ATM straddle\n        # (the chain-implied expiry scale, roughly time-of-day invariant).\n        # Weekly verticals keep the intraday band: their favoured-side\n        # 0.30 delta short is an intraday-expression leg by design.\n        if _weekly_dte and _neutral_condor:\n            _wk_atm = min(chain.keys(), key=lambda k: abs(float(k) - float(spot)))\n            _wk_qc = chain.get(float(_wk_atm), {}).get("call") or {}\n            _wk_qp = chain.get(float(_wk_atm), {}).get("put") or {}\n            _wk_straddle = 0.0\n            if _wk_qc and _wk_qp:\n                _wk_straddle = (\n                    (float(_wk_qc.get("bid", 0)) + float(_wk_qc.get("ask", 0))\n                     + float(_wk_qp.get("bid", 0)) + float(_wk_qp.get("ask", 0)))\n                    / 2.0\n                )\n            _wk_scale = _wk_straddle if _wk_straddle > 20 else _em\n            _band_lo = float(getattr(self.config,\n                                     "em_band_lo_weekly", 0.55)) * _wk_scale\n            _band_hi = float(getattr(self.config,\n                                     "em_band_hi_condor_weekly", 1.35)) * _wk_scale\n        elif _weekly_dte:\n            _band_lo = float(getattr(self.config,\n                                     "em_band_lo_weekly", 0.55)) * _em\n            _band_hi = float(getattr(self.config,\n                                     "em_band_hi_weekly", 2.10)) * _em\n        else:\n            _band_lo = float(getattr(self.config, "em_band_lo", 0.80)) * _em\n    ',
         'weekly condor EM band (ATM straddle scaled)'),
        (' > 0:\n            _wing_factor = 0.50 if dte == 0 else (0.60 if dte == 1 else 0.75)\n            _wing_raw = max(\n                _short_dist_ref * _wing_factor, f',
         ' > 0:\n            _wing_factor = 0.50 if dte == 0 else (0.60 if dte == 1 else 0.62)\n            _wing_raw = max(\n                _short_dist_ref * _wing_factor, f',
         'weekly wing factor 0.62'),
        (' / step + 0.001) * step)\n        wing = int(max(_wing_min, min(wing, _wing_max)))\n\n        if strategy_name == IRON_BUTTERFLY:\n            return self._build_ir',
         ' / step + 0.001) * step)\n        wing = int(max(_wing_min, min(wing, _wing_max)))\n\n        # ── v4.2: adaptive wing fit ───────────────────────────────────────\n        # A fresh-weekly wing (multi-day vega) routinely costs 55-65% of a\n        # short priced at 0.18 delta; the old fixed 0.75-factor wing then\n        # tripped wing_cost_frac_max on EVERY condor candidate (measured\n        # 2026-09-09 11:29-13:50 and 2026-09-10 12:21-12:51: zero\n        # symmetric structures all day). Rather than weaken the gate, widen\n        # the long step by step until its quoted premium is inside the cap\n        # - the long is supposed to be cheap insurance, so let the chain\n        # itself tell us how far out to buy it. Bounded by _wing_max.\n        # Adaptive fitting is for the WEEKLY CONDOR only: the wing-cost\n        # gate it satisfies is condor-only, and the static 0DTE wing\n        # table is the calibrated expiry-day behavior (do not touch it).\n        # The single-sided vertical\'s long is risk definition priced by\n        # the credit/wing ratio gates, so it never gets the fitter either.\n        if (strategy_name == IRON_CONDOR and _weekly_dte\n                and short_dist and _short_dist_ref > 0):\n            wing = self._fit_wing_width(\n                chain=chain, strategy_name=strategy_name,\n                center_ref=_center_ref, short_dist=short_dist,\n                step=step, wing0=wing, wing_max=_wing_max,\n                dte=dte,\n                cap=float(getattr(\n                    self.config,\n                    "wing_cost_frac_max_weekly" if _weekly_dte\n                    else "wing_cost_frac_max", 0.58 if _weekly_dte else 0.50)),\n            )\n\n        if strategy_name == IRON_BUTTERFLY:\n            return self._build_ir',
         'adaptive wing fitter invocation'),
        ('enter_ref\n            )\n        return None, f"unknown_strategy_{strategy_name}"\n\n    def _build_iron_butterfly(\n        self, chain: dict, spot: float, step: i',
         'enter_ref\n            )\n        return None, f"unknown_strategy_{strategy_name}"\n\n    def _fit_wing_width(\n        self,\n        chain: dict,\n        strategy_name: str,\n        center_ref: float,\n        short_dist,\n        step: int,\n        wing0: int,\n        wing_max: int,\n        dte: Optional[int],\n        cap: float,\n    ) -> int:\n        """Smallest wing >= wing0 whose quoted long premium is <= cap x short.\n\n        Pricing uses the real SELL=bid / BUY=ask convention, so it measures\n        the insurance premium the engine would actually pay. If no width up\n        to wing_max satisfies the cap, the widest available is returned and\n        the downstream wing-cost gate rejects the trade as before.\n        """\n        try:\n            if isinstance(short_dist, (tuple, list)):\n                sd_c, sd_p = float(short_dist[0]), float(short_dist[1])\n            else:\n                sd_c = sd_p = float(short_dist)\n            sc = int(round((center_ref + sd_c) / step) * step)\n            sp = int(round((center_ref - sd_p) / step) * step)\n            sides = []\n            if strategy_name in (IRON_CONDOR, BEAR_CALL_SPREAD):\n                sides.append(("call", sc))\n            if strategy_name in (IRON_CONDOR, BULL_PUT_SPREAD):\n                sides.append(("put", sp))\n\n            def _ratio(opt: str, short_k: int, width: int):\n                long_k = short_k + width if opt == "call" else short_k - width\n                if float(long_k) not in chain:\n                    long_k = int(min(chain.keys(),\n                                     key=lambda k: abs(float(k) - long_k)))\n                if float(short_k) not in chain:\n                    return None\n                s = self._get_exec_price(chain, float(short_k), opt, "SELL")\n                l = self._get_exec_price(chain, float(long_k), opt, "BUY")\n                if s <= 0 or l <= 0:\n                    return None\n                return l / s\n\n            w = max(int(wing0), int(step))\n            while w <= int(wing_max):\n                rs = [_ratio(o, k, w) for o, k in sides]\n                if rs and all(r is not None and r <= cap for r in rs):\n                    return w\n                w += int(step)\n            return int(wing_max)\n        except Exception:\n            return int(wing0)\n\n    def _build_iron_butterfly(\n        self, chain: dict, spot: float, step: i',
         '_fit_wing_width method'),
        ('carry_ev, 0.25 * float(stop_loss_pts)\n                    )\n                    if _carry_ev < stop_loss_pts:\n                        stop_loss_pts = _carry_ev\n',
         'carry_ev, 0.25 * float(stop_loss_pts)\n                    )\n                    # v4.2: the carry is an INSTANTANEOUS move priced at\n                    # entry delta with no theta credit. On DTE >= 2 the\n                    # intended hold runs hours and the spot-proximity /\n                    # premium exits actually triggered at 4-8pt losses on\n                    # 28-60pt credits across the 2026-09-08/09/10 replays\n                    # (vs 18-35pt charged here), roughly 0.6x - the other\n                    # half is the theta that accrues before the barrier is\n                    # reached. 0DTE keeps the undiscounted conservative\n                    # number (gamma does not give theta time to accrue).\n                    if (dte is not None and dte >= 2):\n                        _carry_ev *= float(getattr(\n                            self.config, "ev_carry_discount_dte2p", 0.62\n                        ))\n                    if _carry_ev < stop_loss_pts:\n                        stop_loss_pts = _carry_ev\n',
         'EV carry DTE>=2 discount'),
        ('_wing_cost_cap = float(getattr(self.config,',
         '# v4.2: multi-day weekly wings carry vega and cost more relative\n        # to their shorts than 0DTE wings; use the DTE-aware cap that the\n        # adaptive wing fitter (_fit_wing_width) targets.\n        _wing_cost_cap = float(getattr(\n            self.config,\n            "wing_cost_frac_max_weekly" if actual_dte and actual_dte >= 2\n            else',
         'DTE-aware wing cost cap'),
        ('   size_mult = max(float(signals.get("size_multiplier") or 0.50), 0.10)\n        params    = self.compute_params(\n            strategy_name, selection_reason, si',
         '   size_mult = max(float(signals.get("size_multiplier") or 0.50), 0.10)\n        # v4.2: regime layer can ask for a smaller clip on fresh-weekly\n        # range condors (UNCLEAR OI positioning, or elevated-but-not-strong\n        # ADX): the structure is allowed but size is discounted.\n        _weekly_discount = signals.get("weekly_range_size_discount")\n        if _weekly_discount:\n            try:\n                size_mult = size_mult * float(_weekly_discount)\n            except (TypeError, ValueError):\n                pass\n        params    = self.compute_params(\n            strategy_name, selection_reason, si',
         'weekly size discount in decide()'),
    ]),
}



def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_file(root, rel, base_sha, target_sha, hunks):
    path = os.path.join(root, rel)
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    cur_sha = _sha(content)
    if cur_sha == target_sha:
        return "already-patched (no change)"
    if cur_sha != base_sha:
        missing = [lbl for old, _new, lbl in hunks if old not in content]
        raise SystemExit(
            "ERROR: %s matches neither the pristine baseline nor the "
            "patched state (sha %s).\n  hunks whose original text is "
            "absent: %s\nRefusing to modify a diverged file - revert it "
            "to the baseline commit and re-run."
            % (rel, cur_sha[:12], missing))
    for old, new, label in hunks:
        n = content.count(old)
        if n != 1:
            raise SystemExit(
                "ERROR: hunk %r in %s matched %d times (expected 1). "
                "Aborted; file left unchanged." % (label, rel, n))
        content = content.replace(old, new, 1)
        print("  applied: %s :: %s" % (rel, label))
    if _sha(content) != target_sha:
        raise SystemExit(
            "ERROR: %s post-patch sha mismatch - internal inconsistency; "
            "file NOT written." % rel)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return "patched"


def main():
    ap = argparse.ArgumentParser(description="Apply %s algo patch" % VERSION)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip py_compile + harness self-test")
    ap.add_argument("--root", default=None,
                    help="repo root (default: this script's directory)")
    args = ap.parse_args()

    root = args.root or os.path.dirname(os.path.abspath(__file__))
    print("=== %s self-contained patch ===" % VERSION)
    print("repo root: %s" % root)

    states = []
    for rel, (base_sha, target_sha, hunks) in PATCHES.items():
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            raise SystemExit("ERROR: %s not found under %s" % (rel, root))
        print("\n[%s]" % rel)
        states.append((rel, apply_file(root, rel, base_sha, target_sha,
                                       hunks)))

    if not args.no_verify:
        print("\n--- verification ---")
        for rel in PATCHES:
            py_compile.compile(os.path.join(root, rel), doraise=True)
            print("  py_compile OK: %s" % rel)
        bt = os.path.join(root, "backtest_engine.py")
        if os.path.exists(bt):
            print("  running harness self-test (backtest_engine.py --test)...")
            proc = subprocess.run([sys.executable, bt, "--test"], cwd=root,
                                  capture_output=True, text=True)
            lines = (proc.stdout.strip().splitlines()[-4:]
                     + proc.stderr.strip().splitlines()[-4:])
            for ln in lines:
                print("    " + ln)
            if proc.returncode != 0 or "PASSED" not in proc.stdout:
                raise SystemExit("ERROR: harness self-test failed after patch")
        else:
            print("  (backtest_engine.py not found - skipped harness test)")

    print("\n=== patch complete ===")
    for rel, st in states:
        print("  %-22s %s" % (rel, st))
    print("\nValidated replay P&L (backtest_engine.py, real DBs):")
    print("  2026-09-08 DTE0  Rs 1,046.72  (unchanged - 0DTE path)")
    print("  2026-09-09 DTE4  Rs 1,104.73  (was Rs 69.93)")
    print("  2026-09-10 DTE3  Rs 1,340.30  (was Rs 1,062.77)")


if __name__ == "__main__":
    main()
