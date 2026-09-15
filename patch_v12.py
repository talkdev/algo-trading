#!/usr/bin/env python3
"""patch_v12.py — NIFTY intraday options engine: profitability repair pass.

WHAT THIS IS
------------
A self-contained, idempotent repair patch for the algo-trading engine.
Running it applies every fix below to the working tree it is run from:

  1. Tuesday 0DTE discipline ............ no more pre-listing weekly trades
     (strategy_engine) and honest per-expiry marks/exits (execution +
     backtest harness). The 08-Sep +Rs 8,993 was ~80% a mixed-series
     pricing artefact: a Sep-15 spread sold at 09:51 was marked and
     exited on Sep-08 quotes.
  2. DTE-2 unification ................... the DTE2 special-cases in
     regime_engine (calibrated on a mislabelled event Friday) structurally
     closed Thursday 10-Sep, a textbook range day: one flicker entry,
     +Rs 92. DTE2 now flows through the standard path.
  3. Directional day-move ................ the range-consumed block is
     measured on the side that threatens the structure. A 250% down-range
     day is the SAFEST tape to be short calls on; the blanket block left
     15-Sep (a -458 crash) with zero trades.
  4. Wide-OR trend-side exemption ........ a wide opening range bans the
     delta-neutral condor, not the trend-side vertical.
  5. Fixed-cost floor discipline ......... the floor never fires on event
     days, is capped at 3 lots, and re-checks the structural guardrail.
     (11-Sep CPI: a 0.14 size schedule was floored to the 5-lot day
     maximum; the stop cost Rs 3,924.)
  6. Event-day trend confirmation ........ directional short premium on
     event days needs a MEASURED trend (mature ADX), not a 09:47 guess.
  7. Trend-flip exit for verticals ....... a bear call held into a
     measured uptrend (or bull put into a downtrend) exits early while
     underwater instead of riding to the premium stop.
  8. Momentum resurrection .............. DTE0 allowed (half risk, 2-lot
     cap), ADX 30->24, day-move cap 90->200, event/IV-expansion AND
     sell-side-economics refusals answerable, measured-strong trends
     exempt from the chase cap, event floor 0.40, event needs HIGH
     confidence — and main.py now consults decide() on regime refusals
     so the substitute can fire live exactly as in replay (previously
     backtest-only).
  9. Honest ADX .......................... fixed Wilder period-10 on the
     5-minute series; 0.0 (unknown) until mature instead of 30-92 prints
     on flat tapes that gates alternately obeyed and feared.
 10. Weekly exit realism ................. P6 ladder 48/40/32 -> 40/32/24
     and profit-lock 25% -> 22% on DTE1+: weekly decay can actually reach
     the rungs (both 09/10-Sep winners previously held to the bell).
 11. Series-aware opening straddle ...... re-taken when the active expiry
     changes mid-session (the v3.5 IV-baseline fix, extended to the
     straddle the day-move math prices off).
 12. Trend-persist debit hold ............ a momentum ticket rides while
     its thesis lives (mature ADX above the 15 death line, price still
     trend-side-or-napping, breakout side of the OR mid held): the
     ratchet locks only the free trade and the fixed target stands
     down. Banking resumes the moment the trend opposes, dies, or
     loses the breakout — restoring the designed 1.7R+ instead of
     +15% scalps on trend days.

HOW TO RUN
----------
    python3 patch_v12.py

Run from the repository root (the directory containing core.py). The patch
backs each touched file up to <file>.v12bak once, applies every hunk,
byte-verifies each application, and byte-compiles every touched file.
It is idempotent: re-running skips hunks that are already applied.

EXIT CODES: 0 = all hunks applied (or already applied), 1 = failure.
"""

from __future__ import annotations

import py_compile
import shutil
import sys
from pathlib import Path

PATCH_ID = "PATCH_V12"

# Files the patch is allowed to touch. Anything else is refused.
ALLOWED_FILES = {
    "core.py",
    "data_engine.py",
    "regime_engine.py",
    "strategy_engine.py",
    "execution_engine.py",
    "backtest_engine.py",
    "main.py",
}


def _h(file: str, label: str, old: str, new: str, check: str) -> dict:
    return {"file": file, "label": label, "old": old, "new": new, "check": check}


HUNKS: list = [
    # =====================================================================
    # core.py — version marker
    # =====================================================================
    _h(
        "core.py",
        "C0:version-marker",
        'NIFTY_ENGINE_PROFIT_PATCH_V39 = "3.9"',
        'NIFTY_ENGINE_PROFIT_PATCH_V39 = "3.9"\n'
        '# PATCH_V12 (2026-09-15): profitability repair pass — Tuesday-0DTE\n'
        '# discipline, DTE2 unification, directional day-move, floor\n'
        '# discipline, event-day confirmation, trend-flip exit, momentum\n'
        '# resurrection, trend-persist debit hold, honest ADX, weekly exit\n'
        '# realism. See patch_v12.py.\n'
        'NIFTY_ENGINE_PROFIT_PATCH_V12 = "12.0"',
        'NIFTY_ENGINE_PROFIT_PATCH_V12 = "12.0"',
    ),
    # =====================================================================
    # core.py — momentum recalibration (fields)
    # =====================================================================
    _h(
        "core.py",
        "C1a:momentum-min-dte-field",
        "    momentum_min_dte:              int   = 1",
        "    # PATCH_V12: 0DTE momentum allowed (was 1). A confirmed 0DTE\n"
        "    # breakout is the highest-expectancy intraday ticket on the\n"
        "    # board; banning it left 15-Sep (a -458 crash) with no long-\n"
        "    # premium route at all. Half risk + 2-lot cap, see below.\n"
        "    momentum_min_dte:              int   = 0",
        "momentum_min_dte:              int   = 0",
    ),
    _h(
        "core.py",
        "C2a:momentum-adx-min-field",
        "    momentum_adx_min:              float = 30.0",
        "    # PATCH_V12: 30 -> 24. With honest (mature-only) ADX, 30 is a\n"
        "    # bar a real intraday trend often never prints; 24 + OR-break\n"
        "    # + VWAP proof is the professional confirmation stack.\n"
        "    momentum_adx_min:              float = 24.0",
        "momentum_adx_min:              float = 24.0",
    ),
    _h(
        "core.py",
        "C3a:momentum-day-move-max-field",
        "    momentum_day_move_max_pct:     float = 90.0",
        "    # PATCH_V12: 90 -> 200. 90% of priced range is spent by\n"
        "    # mid-morning on every trend day worth trading (measured\n"
        "    # 15-Sep: 200%+ by 11:45 with another -230pts to come).\n"
        "    # Anti-chase protection comes from OR-break freshness, the IV\n"
        "    # gates and the stop — not from a cap that bans trend days.\n"
        "    momentum_day_move_max_pct:     float = 200.0",
        "90% of priced range is spent by",
    ),
    _h(
        "core.py",
        "C4:momentum-block-markers",
        '    momentum_block_markers:        tuple = (\n'
        '        "dte2", "no_exception", "immature", "buy_options",\n'
        '        "wide_or", "dangerous_to_sell",\n'
        "    )",
        "    # PATCH_V12: the substitute must also answer event-day and\n"
        "    # IV-expansion refusals. A confirmed trend THROUGH those blocks\n"
        "    # is exactly the tape a long-premium ticket is for (measured\n"
        "    # 11-Sep: CPI rally blocked all afternoon as\n"
        "    # EVENT:ONLY_RANGE_ALLOWED while the ATM call gained 43pts).\n"
        "    # The gate still refuses while IV is EXPANDING or the straddle\n"
        "    # is expanding — the markers only let the tape REACH the gate.\n"
        "    momentum_block_markers:        tuple = (\n"
        '        "dte2", "no_exception", "immature", "buy_options",\n'
        '        "wide_or", "dangerous_to_sell",\n'
        '        "event", "only_range_allowed", "iv_expanding", "straddle_exp",\n'
        "    )",
        '"event", "only_range_allowed", "iv_expanding", "straddle_exp",',
    ),
    _h(
        "core.py",
        "C5a:momentum-new-knobs-fields",
        "    momentum_structural_risk_cap_mult: float = 2.5\n"
        "    # The route substitutes for the sell side ONLY where the sell side was",
        "    momentum_structural_risk_cap_mult: float = 2.5\n"
        "    # PATCH_V12: 0DTE momentum economics + event-day floor.\n"
        "    momentum_dte0_risk_frac:       float = 0.50\n"
        "    momentum_dte0_max_lots:        int   = 2\n"
        "    momentum_event_size_floor:      float = 0.40\n"
        "    # The route substitutes for the sell side ONLY where the sell side was",
        "momentum_dte0_risk_frac:       float = 0.50",
    ),
    # =====================================================================
    # core.py — momentum recalibration (env loader)
    # =====================================================================
    _h(
        "core.py",
        "C1b:momentum-min-dte-loader",
        '        momentum_min_dte=_get_int(env, "MOMENTUM_MIN_DTE", 1),',
        '        momentum_min_dte=_get_int(env, "MOMENTUM_MIN_DTE", 0),  # PATCH_V12: was 1',
        'momentum_min_dte=_get_int(env, "MOMENTUM_MIN_DTE", 0),',
    ),
    _h(
        "core.py",
        "C2b:momentum-adx-min-loader",
        '        momentum_adx_min=min(max(_get_float(env, "MOMENTUM_ADX_MIN", 30.0), 15.0), 60.0),',
        '        momentum_adx_min=min(max(_get_float(env, "MOMENTUM_ADX_MIN", 24.0), 15.0), 60.0),  # PATCH_V12: was 30.0',
        '"MOMENTUM_ADX_MIN", 24.0)',
    ),
    _h(
        "core.py",
        "C3b:momentum-day-move-max-loader",
        '        momentum_day_move_max_pct=min(max(_get_float(env, "MOMENTUM_DAY_MOVE_MAX_PCT", 90.0), 10.0), 400.0),',
        '        momentum_day_move_max_pct=min(max(_get_float(env, "MOMENTUM_DAY_MOVE_MAX_PCT", 200.0), 10.0), 400.0),  # PATCH_V12: was 90.0',
        "# PATCH_V12: was 90.0",
    ),
    _h(
        "core.py",
        "C5b:momentum-new-knobs-loader",
        "        momentum_structural_risk_cap_mult=min(\n"
        '            max(_get_float(env, "MOMENTUM_STRUCTURAL_RISK_CAP_MULT", 2.5), 1.0), 5.0\n'
        "        ),",
        "        momentum_structural_risk_cap_mult=min(\n"
        '            max(_get_float(env, "MOMENTUM_STRUCTURAL_RISK_CAP_MULT", 2.5), 1.0), 5.0\n'
        "        ),\n"
        "        # PATCH_V12: 0DTE momentum economics + event-day floor.\n"
        '        momentum_dte0_risk_frac=min(max(_get_float(env, "MOMENTUM_DTE0_RISK_FRAC", 0.50), 0.10), 1.00),\n'
        '        momentum_dte0_max_lots=min(max(_get_int(env, "MOMENTUM_DTE0_MAX_LOTS", 2), 1), 10),\n'
        '        momentum_event_size_floor=min(max(_get_float(env, "MOMENTUM_EVENT_SIZE_FLOOR", 0.40), 0.10), 1.00),',
        '"MOMENTUM_DTE0_RISK_FRAC", 0.50)',
    ),
    _h(
        "core.py",
        "C6:profit-lock-dte1plus",
        '        profit_lock_pct_dte1plus=_get_float(env, "PROFIT_LOCK_PCT_DTE1PLUS", 0.25),',
        '        profit_lock_pct_dte1plus=_get_float(env, "PROFIT_LOCK_PCT_DTE1PLUS", 0.22),  # PATCH_V12: was 0.25',
        '"PROFIT_LOCK_PCT_DTE1PLUS", 0.22)',
    ),
    # =====================================================================
    # data_engine.py — honest ADX
    # =====================================================================
    _h(
        "data_engine.py",
        "D1:fixed-adx-period",
        "        # Adaptive Wilder period on the fast series: always chosen so the\n"
        "        # 2*period+1 requirement is satisfied by the bars available.\n"
        "        adx_5        = 0.0\n"
        "        adx_5_mature = False\n"
        "        _period_5    = 0\n"
        "        if not df5.empty and len(df5) >= 11:\n"
        "            _period_5 = max(5, min(self.config.adx_period, (len(df5) - 1) // 2))\n"
        "            if len(df5) >= 2 * _period_5 + 1:\n"
        "                adx_5 = TechnicalEngine.calculate_adx(df5, _period_5)\n"
        "                adx_5_mature = (\n"
        "                    adx_5 > 0.0 and _period_5 >= 9 and len(df5) >= 2 * _period_5 + 2\n"
        "                )\n"
        "\n"
        "        # The effective reading every downstream gate consumes: the\n"
        "        # 15-minute value when it is genuinely mature, otherwise the\n"
        "        # fast-series value, which on NIFTY intraday is the number a\n"
        "        # discretionary trader would actually be looking at.\n"
        "        if adx_15_mature and adx_15_raw > 0.0:\n"
        "            adx_15 = adx_15_raw\n"
        "        elif adx_5 > 0.0:\n"
        "            adx_15 = adx_5\n"
        "        else:\n"
        "            adx_15 = adx_15_raw\n"
        "        adx_15_mature = bool(adx_15_mature or adx_5_mature)",
        "        # PATCH_V12: fixed Wilder period on the fast series + honest\n"
        "        # immaturity. The adaptive period (5..14 by bar count)\n"
        "        # printed 30-92 on flat tapes (measured 2026-09-10 10:15: 46\n"
        "        # on a 30-point drift; 2026-09-15 10:15: 92) and the maturity\n"
        "        # flag flickered as the bar count grew, so gates reading the\n"
        "        # VALUE (DTE exceptions, momentum adx>=30, condor strong-adx\n"
        "        # veto) alternately blocked sound trades and passed noise. A\n"
        "        # fixed period-10 Wilder on 5-minute bars needs 21 bars\n"
        "        # (~10:55) and then stays mature; before that adx_15 reads\n"
        "        # 0.0 and the price classifier's immature path (ORB + VWAP\n"
        "        # confirmation) owns the read, exactly as designed.\n"
        "        _ADX5_PERIOD = 10\n"
        "        adx_5        = 0.0\n"
        "        adx_5_mature = False\n"
        "        _period_5    = _ADX5_PERIOD\n"
        "        if not df5.empty and len(df5) >= 2 * _ADX5_PERIOD + 1:\n"
        "            adx_5 = TechnicalEngine.calculate_adx(df5, _ADX5_PERIOD)\n"
        "            adx_5_mature = bool(adx_5 > 0.0)\n"
        "\n"
        "        # The effective reading every downstream gate consumes: the\n"
        "        # 15-minute value when it is genuinely mature, otherwise the\n"
        "        # fast-series value once THAT is mature, otherwise 0.0\n"
        "        # (unknown). Publishing an immature print as a number made\n"
        "        # every ADX gate a coin flip before ~11:00.\n"
        "        if adx_15_mature and adx_15_raw > 0.0:\n"
        "            adx_15 = adx_15_raw\n"
        "        elif adx_5_mature:\n"
        "            adx_15 = adx_5\n"
        "        else:\n"
        "            adx_15 = 0.0\n"
        "        adx_15_mature = bool(adx_15_mature or adx_5_mature)",
        "_ADX5_PERIOD = 10",
    ),
    # =====================================================================
    # data_engine.py — directional day-move
    # =====================================================================
    _h(
        "data_engine.py",
        "D2:directional-day-move-method",
        '        first_close = self.state.get("first_bar_close")\n'
        "        if first_close is None or first_close <= 0:\n"
        "            return 0.0\n"
        "        return round(abs(spot - first_close) / _straddle_ref * 100.0, 2)",
        '        first_close = self.state.get("first_bar_close")\n'
        "        if first_close is None or first_close <= 0:\n"
        "            return 0.0\n"
        "        return round(abs(spot - first_close) / _straddle_ref * 100.0, 2)\n"
        "\n"
        "    def _compute_directional_day_move(self, spot: Optional[float]) -> Tuple[float, float]:\n"
        '        """\n'
        "        PATCH_V12: upward and downward range consumed, each as % of\n"
        "        the priced move for the time elapsed — the _compute_day_move_used\n"
        "        normalisation, split by direction.\n"
        "\n"
        "        A short call is threatened by rallies, not by selloffs: on\n"
        "        a day that falls 400% of its straddle and never rallies,\n"
        "        the threat to a bear call is ~0, not 400% (measured\n"
        "        2026-09-15: the blanket block refused a correct\n"
        "        PREMIUM_SELL_BEAR regime all day).\n"
        "\n"
        "        Returns (up_pct, down_pct) measured from the session\n"
        "        reference (first bar close) to the day high/low so far.\n"
        "        Note there is deliberately NO 1.93 range factor here: that\n"
        "        factor converts a priced DISPLACEMENT into a priced RANGE,\n"
        "        and a one-sided excursion is already a displacement, so\n"
        "        100 means 'rallying exactly as priced'.\n"
        '        """\n'
        "        opening_straddle = (\n"
        '            self.state.get("opening_straddle_pts") or\n'
        '            self.state.get("_straddle_open_for_regime") or 0.0\n'
        "        )\n"
        "        if opening_straddle <= 0 or spot is None:\n"
        "            return 0.0, 0.0\n"
        '        _dte = self.state.get("actual_dte", 0) or 0\n'
        "        if _dte >= 2:\n"
        "            import math as _math\n"
        "            _theta_frac = max(1.0 / max(_dte, 1), 0.10)\n"
        "            _straddle_ref = max(\n"
        "                opening_straddle * _math.sqrt(_theta_frac),\n"
        "                60.0\n"
        "            )\n"
        "        else:\n"
        "            _straddle_ref = opening_straddle\n"
        "        import math as _math_dm\n"
        "        _elapsed_dm = max(0.0, (\n"
        "            datetime.combine(today_ist(), now_ist().time()) -\n"
        "            datetime.combine(today_ist(), dtime(9, 15))\n"
        "        ).total_seconds() / 60.0)\n"
        "        _frac_dm = min(max(_elapsed_dm / 375.0, 0.06), 1.0)\n"
        "        _straddle_ref = max(\n"
        "            _straddle_ref * _math_dm.sqrt(_frac_dm),\n"
        "            12.0,\n"
        "        )\n"
        '        ref = self.state.get("first_bar_close")\n'
        "        if ref is None or ref <= 0:\n"
        "            try:\n"
        "                ref = float(self._first_bar_close_today or 0.0)\n"
        "            except Exception:\n"
        "                ref = 0.0\n"
        "        if not ref or ref <= 0:\n"
        "            return 0.0, 0.0\n"
        "        today_str = today_ist().isoformat()\n"
        "        try:\n"
        '            bars = self._load_candles_from_db(today_str)\n'
        "            if bars is not None and not bars.empty and len(bars) >= 3:\n"
        '                market_bars = bars[bars["time"] >= "09:15:00"]\n'
        "                if not market_bars.empty:\n"
        '                    day_high = float(market_bars["high"].max())\n'
        '                    day_low = float(market_bars["low"].min())\n'
        "                    up = max(day_high - ref, 0.0) / _straddle_ref * 100.0\n"
        "                    down = max(ref - day_low, 0.0) / _straddle_ref * 100.0\n"
        "                    return round(up, 2), round(down, 2)\n"
        "        except Exception:\n"
        "            pass\n"
        "        return 0.0, 0.0",
        "def _compute_directional_day_move(self",
    ),
    _h(
        "data_engine.py",
        "D3a:directional-day-move-cycle",
        "        day_move_used_pct = self._compute_day_move_used(spot)",
        "        day_move_used_pct = self._compute_day_move_used(spot)\n"
        "        # PATCH_V12: directional components for the threat-aware gate.\n"
        "        day_up_used_pct, day_down_used_pct = self._compute_directional_day_move(spot)",
        "day_up_used_pct, day_down_used_pct = self._compute_directional_day_move(spot)",
    ),
    _h(
        "data_engine.py",
        "D3b:directional-day-move-signals",
        '            "day_move_used_pct":        day_move_used_pct,',
        '            "day_move_used_pct":        day_move_used_pct,\n'
        "            # PATCH_V12: one-sided excursion vs priced displacement.\n"
        '            "day_up_used_pct":          day_up_used_pct,\n'
        '            "day_down_used_pct":        day_down_used_pct,',
        '"day_up_used_pct":          day_up_used_pct,',
    ),
    # =====================================================================
    # data_engine.py — series-aware opening straddle
    # =====================================================================
    _h(
        "data_engine.py",
        "D4a:straddle-retake-on-expiry-change",
        "        # Record opening straddle (once per session, after 09:30)\n"
        "        current_time = now_ist().time()",
        "        # PATCH_V12: the opening straddle belongs to a SERIES, not\n"
        "        # the day. When the active expiry flips mid-session (Tuesday\n"
        "        # 0DTE listed at midday), re-take it on the new series — the\n"
        "        # same expiry-change bug v3.5 fixed for the IV baseline. On\n"
        "        # 2026-09-08 the engine priced the whole 0DTE afternoon off\n"
        "        # the Sep-15 series' 280pt open against a live 82pt chain.\n"
        "        try:\n"
        '            _soe = self.state.get("_straddle_open_expiry")\n'
        "            _axe = expiry.isoformat() if expiry else None\n"
        "            if (_soe and _axe and _soe != _axe\n"
        "                    and current_time >= dtime(9, 30) and not chain_stale\n"
        "                    and atm_straddle > 20 and atm_ce > 0 and atm_pe > 0):\n"
        '                self.state["_straddle_open_for_regime"] = atm_straddle\n'
        '                self.state["_straddle_open_for_summary"] = atm_straddle\n'
        '                self.state["opening_straddle_pts"] = atm_straddle\n'
        '                self.state["_straddle_open_expiry"] = _axe\n'
        "                self.logger.info(\n"
        '                    f"Opening straddle re-taken on expiry change {_soe} -> "\n'
        '                    f"{_axe}: {atm_straddle:.2f}pts"\n'
        "                )\n"
        "        except Exception:\n"
        "            pass\n"
        "        # Record opening straddle (once per session, after 09:30)\n"
        "        current_time = now_ist().time()",
        '"_straddle_open_expiry"] = _axe',
    ),
    _h(
        "data_engine.py",
        "D4b:straddle-open-expiry-record",
        '            self.state["_straddle_open_for_regime"]  = atm_straddle\n'
        '            self.state["_straddle_open_for_summary"] = atm_straddle\n'
        '            self.state["opening_straddle_pts"]       = atm_straddle\n'
        "            self.logger.info(",
        '            self.state["_straddle_open_for_regime"]  = atm_straddle\n'
        '            self.state["_straddle_open_for_summary"] = atm_straddle\n'
        '            self.state["opening_straddle_pts"]       = atm_straddle\n'
        "            self.state[\"_straddle_open_expiry\"] = expiry.isoformat() if expiry else None  # PATCH_V12\n"
        "            self.logger.info(",
        'self.state["_straddle_open_expiry"] = expiry.isoformat()',
    ),
    # =====================================================================
    # regime_engine.py — DTE2 unification
    # =====================================================================
    _h(
        "regime_engine.py",
        "R1:range-dte2-deleted",
        "        # ── DTE 2 exception ───────────────────────────────────────────────\n"
        "        # v1: the first route used to fire on STRONG_SELL + STRONG_RANGE\n"
        "        # alone, with no OR/ADX/confidence gates - it sold a condor into\n"
        "        # an elevated-ADX whipsaw dip (measured 2026-09-11 10:28-11:07:\n"
        "        # ADX 21-37, entered 10:35, lost Rs 142). Both Friday routes now\n"
        "        # require the same strict stack: rich VRP, range positioning, a\n"
        "        # contained opening range and a flat ADX.\n"
        "        if dte == 2:\n"
        "            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and\n"
        "                    pos == PositioningRegime.STRONG_RANGE and\n"
        '                    or_condition in ("VERY_NARROW", "NARROW") and\n'
        "                    adx_15 < self.config.adx_trend_threshold):\n"
        "                return (\n"
        "                    FinalRegime.PREMIUM_SELL_RANGE,\n"
        '                    f"RANGE_DTE2_EXCEPTION_STRONG_SELL_STRONG_RANGE",\n'
        "                )\n"
        "            # v3.1: a second, narrower DTE 2 route. Friday is DTE 2 on the\n"
        "            # Tuesday-expiry calendar and the old single condition (STRONG\n"
        "            # sell AND STRONG range together) is rare enough that Friday was\n"
        "            # effectively closed too.\n"
        "            # v1: the second route required SELL vol EXACTLY, so the\n"
        "            # stronger edge signal (STRONG_SELL + RANGE on a contained,\n"
        "            # flat-ADX tape) was blocked where the weaker one passed - an\n"
        "            # incoherence. STRONG_SELL joins the same strict stack; nothing\n"
        "            # else about Friday range caution changes (measured 2026-09-11:\n"
        "            # every broader Friday range route - elevated-ADX condor,\n"
        "            # UNCLEAR positioning, wide-ADX dip - lost money, so Friday\n"
        "            # stays a flat-ADX-or-nothing tape).\n"
        "            if (vol in (VolatilityRegime.SELL_PREMIUM,\n"
        "                        VolatilityRegime.STRONG_SELL_PREMIUM) and\n"
        "                    pos == PositioningRegime.RANGE and\n"
        '                    or_condition in ("VERY_NARROW", "NARROW") and\n'
        "                    adx_15 < self.config.adx_trend_threshold):\n"
        "                return (\n"
        "                    FinalRegime.PREMIUM_SELL_RANGE,\n"
        '                    "RANGE_DTE2_SELL_PREMIUM_NARROW_OR_FLAT_ADX",\n'
        "                )\n"
        '            return FinalRegime.NO_TRADE, "RANGE_DTE2_NO_EXCEPTION"',
        "        # ── PATCH_V12: the DTE 2 special-case is deleted ─────────────────\n"
        "        # It demanded STRONG_SELL + RANGE/STRONG_RANGE + narrow OR +\n"
        "        # flat ADX together — a stack calibrated on a mislabelled\n"
        "        # session (the 'Friday DTE2' in the comments above is DTE1 on\n"
        "        # a holiday week, and the Friday cited was a CPI event day).\n"
        "        # Measured 2026-09-10: a textbook range tape (STRONG_SELL,\n"
        "        # HIGH confidence, flat ADX, NARROW OR) was refused for two\n"
        "        # hours because OI positioning read BEARISH/UNCLEAR, and the\n"
        "        # single flicker entry made Rs 92. This book is flat by 15:20\n"
        "        # daily, so DTE 2 carries no overnight risk and is gated\n"
        "        # exactly like every other DTE below: condor needs rich vol,\n"
        "        # verticals need their positioning read, event days keep\n"
        "        # their own strict path.",
        "PATCH_V12: the DTE 2 special-case is deleted",
    ),
    _h(
        "regime_engine.py",
        "R2d:downtrend-dte2-deleted",
        "        # DTE 2 exception (v1: mature-ADX confirmation required). An\n"
        "        # immature fast-ADX print above 30 is drift noise, not a trend\n"
        "        # (measured 2026-09-11: period-6 Wilder on 13 five-minute bars\n"
        "        # printed 46 on a 24pt opening-range micro-break and the exception\n"
        "        # sold premium straight into an IV expansion). classify_price\n"
        "        # already treats immature ADX with suspicion (Step 3); the Friday\n"
        "        # trend exceptions must not trade what the price classifier doubts.\n"
        "        if dte == 2:\n"
        "            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and\n"
        "                    adx_15 > 30 and\n"
        '                    signals.get("adx_15_mature")):\n'
        "                return (\n"
        "                    FinalRegime.PREMIUM_SELL_BEAR,\n"
        '                    f"DOWNTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}_MATURE",\n'
        "                )\n"
        "            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and\n"
        "                    adx_15 > 30):\n"
        "                return (\n"
        "                    FinalRegime.NO_TRADE,\n"
        '                    f"DOWNTREND_DTE2_ADX_{adx_15:.0f}_IMMATURE",\n'
        "                )\n"
        '            return FinalRegime.NO_TRADE, "DOWNTREND_DTE2_NO_EXCEPTION"',
        "        # PATCH_V12: DTE 2 flows through the standard trend path (see\n"
        "        # _classify_range note — the exception was calibrated on a\n"
        "        # mislabelled DTE1 event Friday). The remaining discipline is\n"
        "        # for event days, where an unmeasured 'trend' is usually an\n"
        "        # ORB poke on zero information (measured 2026-09-11 09:47:\n"
        "        # adx 0, max-size bear call into a CPI rally, -Rs 3,924).\n"
        '        if signals.get("event_day") and not signals.get("adx_15_mature"):\n'
        '            return FinalRegime.NO_TRADE, "EVENT_TREND_NEEDS_MEASURED_ADX"',
        "The remaining discipline is",
    ),
    _h(
        "regime_engine.py",
        "R2u:uptrend-dte2-deleted",
        "        # DTE 2 exception (v1: mature-ADX confirmation required). An\n"
        "        # immature fast-ADX print above 30 is drift noise, not a trend\n"
        "        # (measured 2026-09-11: period-6 Wilder on 13 five-minute bars\n"
        "        # printed 46 on a 24pt opening-range micro-break and the exception\n"
        "        # sold premium straight into an IV expansion). classify_price\n"
        "        # already treats immature ADX with suspicion (Step 3); the Friday\n"
        "        # trend exceptions must not trade what the price classifier doubts.\n"
        "        if dte == 2:\n"
        "            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and\n"
        "                    adx_15 > 30 and\n"
        '                    signals.get("adx_15_mature")):\n'
        "                return (\n"
        "                    FinalRegime.PREMIUM_SELL_BULL,\n"
        '                    f"UPTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}_MATURE",\n'
        "                )\n"
        "            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and\n"
        "                    adx_15 > 30):\n"
        "                return (\n"
        "                    FinalRegime.NO_TRADE,\n"
        '                    f"UPTREND_DTE2_ADX_{adx_15:.0f}_IMMATURE",\n'
        "                )\n"
        '            return FinalRegime.NO_TRADE, "UPTREND_DTE2_NO_EXCEPTION"',
        "        # PATCH_V12: DTE 2 flows through the standard trend path (see\n"
        "        # _classify_range note). Event days need a MEASURED trend:\n"
        "        # an unmeasured ORB poke is not a directional edge.\n"
        '        if signals.get("event_day") and not signals.get("adx_15_mature"):\n'
        '            return FinalRegime.NO_TRADE, "EVENT_TREND_NEEDS_MEASURED_ADX"',
        "Event days need a MEASURED trend:",
    ),
    _h(
        "regime_engine.py",
        "R3b:event-range-bull-needs-adx",
        "        # ── BULLISH positioning → bull put (unless spot below OR midpoint) ─\n"
        "        if pos == PositioningRegime.BULLISH:\n"
        '            or_high = float(signals.get("or_high") or spot)',
        "        # ── BULLISH positioning → bull put (unless spot below OR midpoint) ─\n"
        "        if pos == PositioningRegime.BULLISH:\n"
        "            # PATCH_V12: on event days a positioning read with no\n"
        "            # measured trend behind it is not a directional edge.\n"
        '            if signals.get("event_day") and not signals.get("adx_15_mature"):\n'
        '                return FinalRegime.NO_TRADE, "EVENT_RANGE_VERTICAL_NEEDS_MEASURED_ADX"\n'
        '            or_high = float(signals.get("or_high") or spot)',
        "if pos == PositioningRegime.BULLISH:\n            # PATCH_V12: on event days",
    ),
    _h(
        "regime_engine.py",
        "R3c:event-range-bear-needs-adx",
        "        # ── BEARISH positioning → bear call (unless spot above OR midpoint) ─\n"
        "        if pos == PositioningRegime.BEARISH:\n"
        '            or_high = float(signals.get("or_high") or spot)',
        "        # ── BEARISH positioning → bear call (unless spot above OR midpoint) ─\n"
        "        if pos == PositioningRegime.BEARISH:\n"
        "            # PATCH_V12: on event days a positioning read with no\n"
        "            # measured trend behind it is not a directional edge.\n"
        '            if signals.get("event_day") and not signals.get("adx_15_mature"):\n'
        '                return FinalRegime.NO_TRADE, "EVENT_RANGE_VERTICAL_NEEDS_MEASURED_ADX"\n'
        '            or_high = float(signals.get("or_high") or spot)',
        "if pos == PositioningRegime.BEARISH:\n            # PATCH_V12: on event days",
    ),
    # =====================================================================
    # strategy_engine.py — Tuesday wait, directional day-move, wide-OR
    # =====================================================================
    _h(
        "strategy_engine.py",
        "S1:tuesday-0dte-wait",
        '        if signals.get("chain_stale"):\n'
        '            return "NO_TRADE", "chain_stale_cannot_validate_strikes"',
        '        if signals.get("chain_stale"):\n'
        '            return "NO_TRADE", "chain_stale_cannot_validate_strikes"\n'
        "\n"
        "        # PATCH_V12: on Tuesdays the tradeable contract is the 0DTE\n"
        "        # series. If the broker has not listed it yet, the engine\n"
        "        # used to trade the NEXT weekly as if it were a normal day\n"
        "        # and then get re-priced onto the 0DTE chain mid-position\n"
        "        # (measured 2026-09-08: Sep-15 spread sold at 09:51, marked\n"
        "        # on Sep-08 quotes by the afternoon — a phantom Rs 8,993).\n"
        "        # Wait for the real contract.\n"
        "        try:\n"
        "            _tue_wait = (\n"
        '                state.get("day_label") == "TUESDAY"\n'
        '                and signals.get("active_expiry") is not None\n'
        '                and signals.get("trading_date") is not None\n'
        '                and str(signals.get("active_expiry"))[:10] != str(signals.get("trading_date"))[:10]\n'
        "                and current_time < dtime(14, 0)\n"
        "            )\n"
        "        except Exception:\n"
        "            _tue_wait = False\n"
        "        if _tue_wait:\n"
        '            return "NO_TRADE", "tuesday_waiting_for_0dte_series_listed"',
        '"tuesday_waiting_for_0dte_series_listed"',
    ),
    _h(
        "strategy_engine.py",
        "S2:directional-day-move-gate",
        '        day_move_used = float(signals.get("day_move_used_pct") or 0.0)\n'
        "        if day_move_used >= self.config.day_move_used_block_pct:\n"
        '            return "NO_TRADE", (\n'
        '                f"day_move_used_{day_move_used:.0f}pct_of_opening_straddle_no_edge"\n'
        "            )",
        "        # PATCH_V12: the day-move block is measured on the side that\n"
        "        # threatens the structure, not the whole range. A 250%\n"
        "        # down-range day is the SAFEST tape to be short calls on\n"
        "        # (measured 2026-09-15: regime said PREMIUM_SELL_BEAR all\n"
        "        # day, the blanket block refused every cycle, zero trades on\n"
        "        # a -458 crash). Condors keep the total-range block.\n"
        '        day_move_used = float(signals.get("day_move_used_pct") or 0.0)\n'
        "        _dm_threat = day_move_used\n"
        "        try:\n"
        '            if final_regime == "PREMIUM_SELL_BEAR":\n'
        '                _dm_threat = float(signals.get("day_up_used_pct", day_move_used) or 0.0)\n'
        '            elif final_regime == "PREMIUM_SELL_BULL":\n'
        '                _dm_threat = float(signals.get("day_down_used_pct", day_move_used) or 0.0)\n'
        "        except (TypeError, ValueError):\n"
        "            _dm_threat = day_move_used\n"
        "        if _dm_threat >= self.config.day_move_used_block_pct:\n"
        '            return "NO_TRADE", (\n'
        '                f"day_move_used_{_dm_threat:.0f}pct_of_opening_straddle_no_edge"\n'
        "            )",
        "_dm_threat = day_move_used",
    ),
    _h(
        "strategy_engine.py",
        "S3:wide-or-trend-side-exemption",
        '        or_condition = signals.get("or_condition", "MODERATE")\n'
        '        if or_condition in ("WIDE", "VERY_WIDE"):\n'
        '            if not signals.get("gap_fade_opportunity"):\n'
        '                return "NO_TRADE", f"wide_or_{or_condition}_dangerous_to_sell_premium"',
        '        or_condition = signals.get("or_condition", "MODERATE")\n'
        '        if or_condition in ("WIDE", "VERY_WIDE"):\n'
        "            # PATCH_V12: a wide opening range bans the DELTA-NEUTRAL\n"
        "            # condor, not the trend-side vertical. Selling calls\n"
        "            # above a confirmed breakdown (or puts below a breakout)\n"
        "            # is how a wide-range trend day is harvested; the blanket\n"
        "            # ban left 2026-09-15 untradeable by every route.\n"
        '            _px = signals.get("price_regime", "")\n'
        "            _trend_side_ok = (\n"
        '                (final_regime == "PREMIUM_SELL_BEAR" and _px in ("DOWNTREND", "STRONG_DOWNTREND"))\n'
        '                or (final_regime == "PREMIUM_SELL_BULL" and _px in ("UPTREND", "STRONG_UPTREND"))\n'
        "            )\n"
        '            if not signals.get("gap_fade_opportunity") and not _trend_side_ok:\n'
        '                return "NO_TRADE", f"wide_or_{or_condition}_dangerous_to_sell_premium"',
        "_trend_side_ok = (",
    ),
    # =====================================================================
    # strategy_engine.py — floor discipline
    # =====================================================================
    _h(
        "strategy_engine.py",
        "S4:fixed-cost-floor-discipline",
        "        if (\n"
        "            final_lots == 1\n"
        "            and raw_lots >= 1.5\n"
        '            and signals.get("confidence_level") == "HIGH"\n'
        '            and not bool(signals.get("borderline_sell", False))\n'
        "        ):\n"
        "            _floor_lots = min(int(round(raw_lots)), day_cap)\n"
        "            if _floor_lots >= 2:\n"
        "                self.logger.info(\n"
        '                    f"Fixed-cost floor: budget supports {raw_lots:.2f} "\n'
        '                    f"risk-correct lots but size schedule left 1 lot; "\n'
        '                    f"sizing to {_floor_lots} lots (day cap {day_cap})"\n'
        "                )\n"
        "                final_lots = _floor_lots",
        "        # PATCH_V12: the fixed-cost floor never fires on event days,\n"
        "        # is capped at 3 lots, and re-checks the structural guardrail\n"
        "        # it used to jump over. Measured 2026-09-11 (CPI): a 0.14\n"
        "        # size schedule was floored to the 5-lot day MAXIMUM on a\n"
        "        # thin, unmeasured setup; the stop cost Rs 3,924.\n"
        '        _is_event_floor = bool(signals.get("event_day", False))\n'
        "        if (\n"
        "            final_lots == 1\n"
        "            and raw_lots >= 1.5\n"
        '            and signals.get("confidence_level") == "HIGH"\n'
        '            and not bool(signals.get("borderline_sell", False))\n'
        "            and not _is_event_floor\n"
        "        ):\n"
        "            _floor_lots = min(int(round(raw_lots)), day_cap, 3)\n"
        "            if _floor_lots >= 2:\n"
        "                self.logger.info(\n"
        '                    f"Fixed-cost floor: budget supports {raw_lots:.2f} "\n'
        '                    f"risk-correct lots but size schedule left 1 lot; "\n'
        '                    f"sizing to {_floor_lots} lots (day cap {day_cap})"\n'
        "                )\n"
        "                final_lots = _floor_lots\n"
        "                if structural_loss_per_lot * final_lots > max_risk * 1.5:\n"
        "                    final_lots = max(1, int(max_risk / structural_loss_per_lot))",
        "_is_event_floor = bool(signals.get(\"event_day\", False))",
    ),
    # =====================================================================
    # strategy_engine.py — momentum economics
    # =====================================================================
    _h(
        "strategy_engine.py",
        "S5a:momentum-event-high-conf",
        '        if str(signals.get("confidence_level") or "") not in ("HIGH", "MEDIUM"):\n'
        "            return False, (\n"
        '                f"momentum_confidence_{signals.get(\'confidence_level\')}_insufficient"\n'
        "            ), 0",
        '        if str(signals.get("confidence_level") or "") not in ("HIGH", "MEDIUM"):\n'
        "            return False, (\n"
        '                f"momentum_confidence_{signals.get(\'confidence_level\')}_insufficient"\n'
        "            ), 0\n"
        "        # PATCH_V12: event-day momentum needs HIGH conviction — the\n"
        "        # schedule is already softened for the substitute, so the\n"
        "        # read itself must be unambiguous.\n"
        '        if signals.get("event_day") and str(signals.get("confidence_level") or "") != "HIGH":\n'
        '            return False, "momentum_event_day_needs_high_confidence", 0',
        '"momentum_event_day_needs_high_confidence"',
    ),
    _h(
        "strategy_engine.py",
        "S5b:momentum-dte0-half-risk",
        "        budget  = float(cfg.max_risk_per_trade_pct or 0.006)\n"
        "        max_risk = (\n"
        "            current_capital * budget\n"
        '            * float(getattr(cfg, "momentum_risk_frac_of_budget", 1.0))\n'
        "        )",
        "        budget  = float(cfg.max_risk_per_trade_pct or 0.006)\n"
        "        # PATCH_V12: 0DTE momentum (newly allowed) risks half the\n"
        "        # ticket: the theta cliff is real, the stop is the plan.\n"
        '        _mom_risk_frac = float(getattr(cfg, "momentum_risk_frac_of_budget", 1.0))\n'
        "        try:\n"
        "            if int(actual_dte) == 0:\n"
        '                _mom_risk_frac = min(_mom_risk_frac, float(getattr(cfg, "momentum_dte0_risk_frac", 0.50)))\n'
        "        except (TypeError, ValueError):\n"
        "            pass\n"
        "        max_risk = current_capital * budget * _mom_risk_frac",
        "_mom_risk_frac = float(getattr(cfg,",
    ),
    _h(
        "strategy_engine.py",
        "S5c:momentum-dte0-lot-cap",
        "        day_cap = max(1, int(LOT_CAPS_BY_DAY.get(day_label, 3) * _eq))\n"
        "        final_lots = max(1, min(int(round(sized)), day_cap))",
        "        day_cap = max(1, int(LOT_CAPS_BY_DAY.get(day_label, 3) * _eq))\n"
        "        final_lots = max(1, min(int(round(sized)), day_cap))\n"
        "        # PATCH_V12: a 0DTE long-premium ticket is capped at 2 lots:\n"
        "        # the structural cap below is premium-multiple based and\n"
        "        # would let a cheap ticket size itself into a cliff.\n"
        "        try:\n"
        "            if int(actual_dte) == 0:\n"
        '                final_lots = min(final_lots, int(getattr(cfg, "momentum_dte0_max_lots", 2)))\n'
        "        except (TypeError, ValueError):\n"
        "            pass",
        '"momentum_dte0_max_lots", 2)))',
    ),
    _h(
        "strategy_engine.py",
        "S5d:momentum-event-size-floor",
        "        sched = max(float(size_mult or 1.0),\n"
        '                    float(getattr(cfg, "momentum_size_floor", 0.80)))',
        "        # PATCH_V12: the size floor respects event days (0.40): a CPI\n"
        "        # breakout is still sized with the schedule's fear, just not\n"
        "        # into the ground.\n"
        '        _mom_floor = float(getattr(cfg, "momentum_size_floor", 0.80))\n'
        '        if signals.get("event_day"):\n'
        '            _mom_floor = min(_mom_floor, float(getattr(cfg, "momentum_event_size_floor", 0.40)))\n'
        "        sched = max(float(size_mult or 1.0), _mom_floor)",
        '"momentum_event_size_floor", 0.40)))',
    ),
    _h(
        "strategy_engine.py",
        "S6:trend-confirmed-day-move-exemption",
        "        if _dm_threat >= self.config.day_move_used_block_pct:\n"
        '            return "NO_TRADE", (\n'
        '                f"day_move_used_{_dm_threat:.0f}pct_of_opening_straddle_no_edge"\n'
        "            )",
        "        # PATCH_V12 (round 2): a CONFIRMED trend exempts the\n"
        "        # trend-side vertical from the day-move block. An exhausted\n"
        "        # WITH-trend move is the thesis of the structure, not its\n"
        "        # risk: the opening spike that inflates the gauge is ancient\n"
        "        # history when spot sits 250pts below it (measured\n"
        "        # 2026-09-15: a +100pt opening spike annualised to 358% by\n"
        "        # the time-fraction normalisation, blocking a confirmed\n"
        "        # STRONG_DOWNTREND bear regime all session). Reversal risk is\n"
        "        # managed where it belongs — strike distance, the premium/\n"
        "        # price stops and the trend-flip exit — not by refusing the\n"
        "        # trend-side ticket. Condors and unconfirmed leans keep the\n"
        "        # block (directional threat for leans, total range for\n"
        "        # condors).\n"
        '        _dm_px = signals.get("price_regime", "")\n'
        "        _dm_trend_confirmed = (\n"
        '            (final_regime == "PREMIUM_SELL_BEAR" and _dm_px in ("DOWNTREND", "STRONG_DOWNTREND"))\n'
        '            or (final_regime == "PREMIUM_SELL_BULL" and _dm_px in ("UPTREND", "STRONG_UPTREND"))\n'
        "        )\n"
        "        if _dm_threat >= self.config.day_move_used_block_pct and not _dm_trend_confirmed:\n"
        '            return "NO_TRADE", (\n'
        '                f"day_move_used_{_dm_threat:.0f}pct_of_opening_straddle_no_edge"\n'
        "            )",
        "_dm_trend_confirmed = (",
    ),
    _h(
        "strategy_engine.py",
        "S7:max-pain-trend-through-exemption",
        '            max_pain = float(signals.get("max_pain") or 0)\n'
        "            if max_pain > 0 and abs(spot - max_pain) < 25:\n"
        "                return False, (\n"
        '                    f"bear_call_spot_within_25pts_of_max_pain_{max_pain:.0f}"\n'
        "                )",
        '            max_pain = float(signals.get("max_pain") or 0)\n'
        "            # PATCH_V12 (round 2): pin risk is a range-tape\n"
        "            # phenomenon. A tape printing a confirmed downtrend is\n"
        "            # TRENDING THROUGH max pain, not pinning to it (measured\n"
        "            # 2026-09-15: STRONG_DOWNTREND, mature ADX 33, spot\n"
        "            # falling through 23350 — the veto blocked the\n"
        "            # trend-side vertical for 20 minutes mid-trend).\n"
        '            _mp_px = signals.get("price_regime", "")\n'
        '            _mp_trend_through = _mp_px in ("DOWNTREND", "STRONG_DOWNTREND")\n'
        "            if max_pain > 0 and abs(spot - max_pain) < 25 and not _mp_trend_through:\n"
        "                return False, (\n"
        '                    f"bear_call_spot_within_25pts_of_max_pain_{max_pain:.0f}"\n'
        "                )",
        "_mp_trend_through = _mp_px in",
    ),
    _h(
        "core.py",
        "M1:momentum-economics-markers",
        '        "event", "only_range_allowed", "iv_expanding", "straddle_exp",\n'
        "    )",
        "        # PATCH_V12 (round 3): the substitute also answers\n"
        "        # sell-side ECONOMIC refusals. A thin credit, a binding\n"
        "        # brokerage ratio or a negative short-premium EV says the\n"
        "        # READ cannot be expressed short — it says nothing about\n"
        "        # the same read expressed long, which the momentum gate\n"
        "        # underwrites from scratch (own trend/OR/VWAP/IV stack, own\n"
        "        # budget, own stop). Measured 2026-09-15: the confirmed\n"
        "        # bear read died on credit_risk/brokerage/EV at 1 lot while\n"
        "        # the long-put ticket that fit the budget was never\n"
        "        # consulted. day_move is answerable too: the 125 sell-side\n"
        "        # bar and the momentum chase cap are different standards,\n"
        "        # and the gate's own cap arbitrates.\n"
        '        "event", "only_range_allowed", "iv_expanding", "straddle_exp",\n'
        '        "params_invalid", "strategy_rules_failed", "day_move_used",\n'
        "    )",
        '"params_invalid", "strategy_rules_failed", "day_move_used",',
    ),
    _h(
        "strategy_engine.py",
        "M3:momentum-on-rules-refusal",
        "        rules_ok, rules_reason = self._validate_entry_rules(strategy_name, signals)\n"
        "        if not rules_ok:\n"
        '            full_reason = f"strategy_rules_failed:{rules_reason}"\n'
        "            self._log_decision(signals, \"NO_TRADE\", full_reason)",
        "        rules_ok, rules_reason = self._validate_entry_rules(strategy_name, signals)\n"
        "        if not rules_ok:\n"
        '            full_reason = f"strategy_rules_failed:{rules_reason}"\n'
        "            # PATCH_V12 (round 3): the substitute is consulted on\n"
        "            # structure-rule refusals exactly as on hard-gate and\n"
        "            # economics refusals — the rules are sell-structure-\n"
        "            # specific (pin veto, OR-mid positioning, delta gates)\n"
        "            # and a long-premium ticket re-underwrites every one of\n"
        "            # them in its own gate.\n"
        "            alt = self._momentum_decision(signals, full_reason)\n"
        "            if alt is not None:\n"
        "                return alt\n"
        "            self._log_decision(signals, \"NO_TRADE\", full_reason)",
        "full_reason = f\"strategy_rules_failed:{rules_reason}\"\n            # PATCH_V12 (round 3)",
    ),
    _h(
        "strategy_engine.py",
        "M4:momentum-strong-trend-chase-exemption",
        "        # ── freshness: a day that has already spent its priced range is\n"
        "        #    not a breakout, it is the trade everyone is already in ───────\n"
        "        try:\n"
        '            used = float(signals.get("day_move_used_pct") or 0.0)\n'
        "        except (TypeError, ValueError):\n"
        "            used = 0.0\n"
        '        if used >= float(getattr(cfg, "momentum_day_move_max_pct", 90.0)):\n'
        '            return False, f"momentum_day_move_used_{used:.0f}pct_exhausted", 0',
        "        # ── freshness: a day that has already spent its priced range is\n"
        "        #    not a breakout, it is the trade everyone is already in ───────\n"
        "        # PATCH_V12: a MEASURED-STRONG trend is exempt from the chase\n"
        "        # cap. Mature ADX above the strong threshold with a STRONG_*\n"
        "        # price read is continuation, not exhaustion — the opening\n"
        "        # spike that inflates the gauge is ancient history by\n"
        "        # mid-morning (measured 2026-09-15: 2.26x consumed at 11:00\n"
        "        # with ADX 86 on a tape that fell 230pts further). The cap\n"
        "        # still refuses plain-trend and immature-read chases, and\n"
        "        # the 35% premium stop bounds every ticket. No new knob: the\n"
        "        # strong threshold and the maturity flag are reused.\n"
        "        try:\n"
        '            used = float(signals.get("day_move_used_pct") or 0.0)\n'
        "        except (TypeError, ValueError):\n"
        "            used = 0.0\n"
        "        _mom_strong = (\n"
        '            bool(signals.get("adx_15_mature", False))\n'
        '            and adx >= float(getattr(cfg, "adx_strong_threshold", 28.0))\n'
        '            and price in ("STRONG_UPTREND", "STRONG_DOWNTREND")\n'
        "        )\n"
        '        if used >= float(getattr(cfg, "momentum_day_move_max_pct", 90.0)) and not _mom_strong:\n'
        '            return False, f"momentum_day_move_used_{used:.0f}pct_exhausted", 0',
        "_mom_strong = (",
    ),
    # =====================================================================
    # execution_engine.py — trend-flip exit, weekly ladder, chain cache
    # =====================================================================
    _h(
        "execution_engine.py",
        "E1:trend-flip-exit",
        "        # ── Priority 3: Price stop ────────────────────────────────────────\n"
        "        # Price stop = 0.30 × opening straddle from short strike level",
        "        # ── PATCH_V12 Priority 2.5: trend-flip exit for verticals ─────────\n"
        "        # A BEAR_CALL held into a MEASURED uptrend (or BULL_PUT into a\n"
        "        # measured downtrend) is no longer the trade that was approved\n"
        "        # — the premium stop will take it eventually, at a worse\n"
        "        # price. Measured 2026-09-11: a bear call held 85 minutes\n"
        "        # into a CPI rally to a -Rs 3,924 premium stop; the flip was\n"
        "        # measurable ~25 minutes earlier at roughly half the loss.\n"
        "        # Fires only when the flip is measured (mature ADX >= 20),\n"
        "        # the position is underwater (never cut a winner on a regime\n"
        "        # flicker), and the trade is older than 10 minutes.\n"
        "        # Condors/flys are exempt: a trend does not invalidate both\n"
        "        # sides at once.\n"
        "        try:\n"
        '            _flip_name = str(position.get("strategy_name") or "")\n'
        '            _flip_px   = str(signals.get("price_regime") or "")\n'
        '            _flip_adx  = float(signals.get("adx_15") or 0.0)\n'
        '            _flip_mat  = bool(signals.get("adx_15_mature", False))\n'
        "            _flip_hold_min = 9999.0\n"
        "            try:\n"
        '                _flip_entry_t = position.get("entry_time")\n'
        "                if _flip_entry_t:\n"
        "                    _flip_hold_min = (\n"
        "                        now_ist() - datetime.fromisoformat(str(_flip_entry_t))\n"
        "                    ).total_seconds() / 60.0\n"
        "            except Exception:\n"
        "                _flip_hold_min = 9999.0\n"
        "            _flip_against = (\n"
        '                (_flip_name == "BEAR_CALL_SPREAD" and\n'
        '                 _flip_px in ("UPTREND", "STRONG_UPTREND"))\n'
        '                or (_flip_name == "BULL_PUT_SPREAD" and\n'
        '                    _flip_px in ("DOWNTREND", "STRONG_DOWNTREND"))\n'
        "            )\n"
        "            if (_flip_against and _flip_mat and _flip_adx >= 20.0\n"
        "                    and entry_credit > 0\n"
        "                    and liq_premium > entry_credit * 1.05\n"
        "                    and _flip_hold_min >= 10.0):\n"
        "                self.logger.warning(\n"
        '                    f"PATCH_V12 TREND FLIP: {_flip_name} held into "\n'
        '                    f"{_flip_px} (adx={_flip_adx:.0f} mature), "\n'
        '                    f"liq={liq_premium:.2f} vs credit={entry_credit:.2f} — closing early"\n'
        "                )\n"
        '                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {\n'
        '                    "reason_detail": (\n'
        '                        f"trend_flip_exit_{_flip_px}_adx_{_flip_adx:.0f}_"\n'
        '                        f"liq_{liq_premium:.2f}_vs_credit_{entry_credit:.2f}"\n'
        "                    ),\n"
        "                }\n"
        "        except Exception as _flip_exc:\n"
        '            self.logger.debug(f"trend-flip check skipped: {_flip_exc}")\n'
        "\n"
        "        # ── Priority 3: Price stop ────────────────────────────────────────\n"
        "        # Price stop = 0.30 × opening straddle from short strike level",
        "trend_flip_exit_",
    ),
    _h(
        "execution_engine.py",
        "E2:weekly-p6-ladder",
        "            else:\n"
        "                time_targets = [\n"
        "                    (dtime(12, 0),  0.48),\n"
        "                    (dtime(13, 0),  0.40),\n"
        "                    (dtime(14, 0),  0.32),\n"
        "                ]",
        "            else:\n"
        "                # PATCH_V12: weekly targets 48/40/32 -> 40/32/24. A\n"
        "                # DTE1-4 structure decays ~5-15% of credit intraday;\n"
        "                # the old ladder never fired (measured 09/10-Sep: both\n"
        "                # winners held to the bell, +Rs 1,121 and +Rs 92).\n"
        "                # The ladder still demands real decay — it just no\n"
        "                # longer demands the impossible.\n"
        "                time_targets = [\n"
        "                    (dtime(12, 0),  0.40),\n"
        "                    (dtime(13, 0),  0.32),\n"
        "                    (dtime(14, 0),  0.24),\n"
        "                ]",
        "(dtime(14, 0),  0.24),",
    ),
    _h(
        "execution_engine.py",
        "E3:per-expiry-chain-cache",
        "        # Get chain for the correct expiry\n"
        "        chain_expiry = self.market_engine.last_chain_expiry\n"
        "        if chain_expiry and chain_expiry.isoformat() != position.get(\"target_expiry\"):\n"
        "            chain = {}  # Wrong expiry chain — use empty dict (falls back to entry price)",
        "        # Get chain for the correct expiry\n"
        "        chain_expiry = self.market_engine.last_chain_expiry\n"
        "        # PATCH_V12: keep the last chain per expiry so a position is\n"
        "        # always marked on its OWN series. Without this, a Tuesday\n"
        "        # session whose active expiry flips mid-position (0DTE listed\n"
        "        # at midday) marks the morning's weekly spread on 0DTE quotes\n"
        "        # — or, with the guard below, goes blind and holds everything\n"
        "        # to the bell. (The strategy layer now refuses Tuesday\n"
        "        # pre-0DTE entries, so this is defence in depth — and it is\n"
        "        # what makes replayed marks honest whenever series coverage\n"
        "        # is partial.)\n"
        "        try:\n"
        '            _cache = getattr(self, "_chain_by_expiry", None)\n'
        "            if _cache is None:\n"
        "                _cache = {}\n"
        "                self._chain_by_expiry = _cache\n"
        "            if chain and chain_expiry is not None:\n"
        "                try:\n"
        "                    _cache[str(chain_expiry.isoformat())] = dict(chain)\n"
        "                    while len(_cache) > 3:\n"
        "                        _cache.pop(next(iter(_cache)))\n"
        "                except Exception:\n"
        "                    pass\n"
        '            _want = str(position.get("target_expiry") or "")[:10]\n'
        "            if _want and chain_expiry is not None and str(chain_expiry.isoformat())[:10] != _want:\n"
        "                _cached = _cache.get(_want)\n"
        "                if _cached:\n"
        "                    chain = _cached\n"
        "                    chain_expiry = None  # already the position's own chain\n"
        "        except Exception:\n"
        "            pass\n"
        "        if chain_expiry and chain_expiry.isoformat() != position.get(\"target_expiry\"):\n"
        "            chain = {}  # Wrong expiry chain — use empty dict (falls back to entry price)",
        '"_chain_by_expiry", None)',
    ),
    _h(
        "execution_engine.py",
        "E4a:debit-monitor-signals-param",
        "    def _monitor_debit_position(\n"
        "        self,\n"
        "        position:         dict,\n"
        "        open_legs:        List[dict],\n"
        "        chain:            dict,\n"
        "        current_time:     dtime,\n"
        "        spot:             float,\n"
        "        current_premium:  float,\n"
        "        liq_premium:      float,\n"
        "    ) -> Tuple[str, int, dict]:",
        "    # PATCH_V12: the debit ladder takes the live signals (optional,\n"
        "    # so every existing caller keeps working) for the trend-persist\n"
        "    # hold below.\n"
        "    def _monitor_debit_position(\n"
        "        self,\n"
        "        position:         dict,\n"
        "        open_legs:        List[dict],\n"
        "        chain:            dict,\n"
        "        current_time:     dtime,\n"
        "        spot:             float,\n"
        "        current_premium:  float,\n"
        "        liq_premium:      float,\n"
        "        signals: Optional[dict] = None,\n"
        "    ) -> Tuple[str, int, dict]:",
        "signals: Optional[dict] = None,",
    ),
    _h(
        "execution_engine.py",
        "E4b:debit-monitor-pass-signals",
        '        if entry_credit < 0 or str(position.get("strategy_type") or "").upper() == "BUY":\n'
        "            return self._monitor_debit_position(\n"
        "                position, open_legs, chain, current_time, spot,\n"
        "                current_premium, liq_premium,\n"
        "            )",
        '        if entry_credit < 0 or str(position.get("strategy_type") or "").upper() == "BUY":\n'
        "            return self._monitor_debit_position(\n"
        "                position, open_legs, chain, current_time, spot,\n"
        "                current_premium, liq_premium,\n"
        "                signals=signals,  # PATCH_V12: trend-persist hold\n"
        "            )",
        "signals=signals,  # PATCH_V12",
    ),
    _h(
        "execution_engine.py",
        "E4c:trend-persist-ratchet-cap",
        '        activated = bool(position.get("profit_lock_activated"))\n'
        '        locked    = position.get("profit_lock_stop_level")\n'
        "        if value_mid >= lock:\n"
        '            keep = float(getattr(cfg, "momentum_lock_keep_frac", 0.50))\n'
        "            new_level = max(\n"
        "                value_mid - max(value_mid - entry_value, 0.0) * keep,\n"
        "                entry_value + rt_cost,\n"
        "            )",
        '        activated = bool(position.get("profit_lock_activated"))\n'
        '        locked    = position.get("profit_lock_stop_level")\n'
        "        # PATCH_V12: trend-persist hold. A momentum ticket exists to\n"
        "        # ride a trend; banking it on a fixed give-back fraction\n"
        "        # while the thesis is still alive converts 1.7R+ winners\n"
        "        # into +15% scalps (measured 11-Sep: call spiked +38% by\n"
        "        # noon, stopped at +18% at 12:12 ahead of an afternoon\n"
        "        # rally; 15-Sep: put ran +31%, stopped at +15% at 13:19\n"
        "        # ahead of the waterfall). While the thesis lives — mature\n"
        "        # ADX above the death line, price still trend-side or\n"
        "        # merely napping (never opposed), spot still holding the\n"
        "        # breakout side of the opening-range mid — the ratchet locks\n"
        "        # only the free trade. The ride ends at the closing\n"
        "        # flatten, the breakeven stop, or the trend break, whichever\n"
        "        # comes first. Entry/exit asymmetry is deliberate:\n"
        "        # conviction (24) to enter, thesis-death (15) to abandon.\n"
        "        _persist = False\n"
        "        try:\n"
        "            _sig = signals or {}\n"
        '            _sname = str(position.get("strategy_name") or "")\n'
        '            if "PUT" in _sname:\n'
        "                _dir = -1\n"
        '            elif "CALL" in _sname:\n'
        "                _dir = 1\n"
        "            else:\n"
        '                _dir = int(raw.get("momentum_direction") or position.get("momentum_direction") or 0)\n'
        '            _px = str(_sig.get("price_regime") or "")\n'
        '            _adx = float(_sig.get("adx_15") or 0.0)\n'
        '            _mat = bool(_sig.get("adx_15_mature", False))\n'
        '            _orh = float(_sig.get("or_high") or 0.0)\n'
        '            _orl = float(_sig.get("or_low") or 0.0)\n'
        "            _ormid = (_orh + _orl) / 2.0 if (_orh > 0 and _orl > 0) else 0.0\n"
        "            _trend_side_ok = (\n"
        '                (_dir > 0 and _px in ("UPTREND", "STRONG_UPTREND", "RANGE"))\n'
        '                or (_dir < 0 and _px in ("DOWNTREND", "STRONG_DOWNTREND", "RANGE"))\n'
        "            )\n"
        "            _brk_ok = (\n"
        "                _ormid <= 0\n"
        "                or (_dir > 0 and spot >= _ormid)\n"
        "                or (_dir < 0 and spot <= _ormid)\n"
        "            )\n"
        '            _death = float(getattr(cfg, "momentum_trend_death_adx", 15.0))\n'
        "            _persist = bool(_dir != 0 and _trend_side_ok and _brk_ok and _mat and _adx >= _death)\n"
        "        except Exception:\n"
        "            _persist = False\n"
        "        if value_mid >= lock:\n"
        '            keep = float(getattr(cfg, "momentum_lock_keep_frac", 0.50))\n'
        "            new_level = max(\n"
        "                value_mid - max(value_mid - entry_value, 0.0) * keep,\n"
        "                entry_value + rt_cost,\n"
        "            )\n"
        "            if _persist:\n"
        "                # Ride: lock only the free trade, never bank into strength.\n"
        "                new_level = min(new_level, entry_value + rt_cost)",
        "_persist = bool(_dir != 0 and _trend_side_ok",
    ),
    _h(
        "execution_engine.py",
        "E4d:trend-persist-target-stand-down",
        "        if target > 0 and value >= target:",
        "        # PATCH_V12: no fixed targets into a living trend (see D2).\n"
        "        if target > 0 and value >= target and not _persist:",
        "value >= target and not _persist:",
    ),
    # =====================================================================
    # backtest_engine.py — honest exit pricing
    # =====================================================================
    _h(
        "backtest_engine.py",
        "B1a:chain-cache-reset",
        "        live: Optional[dict] = None\n"
        "        day_pnl = 0.0",
        "        live: Optional[dict] = None\n"
        "        day_pnl = 0.0\n"
        "        self._chain_by_expiry = {}  # PATCH_V12: reset per session (see _close)",
        "self._chain_by_expiry = {}  # PATCH_V12",
    ),
    _h(
        "backtest_engine.py",
        "B1b:chain-snapshot-per-cycle",
        "            signals = self._classify(signals)\n",
        "            signals = self._classify(signals)\n"
        "            # PATCH_V12: snapshot the active chain per expiry for\n"
        "            # honest exit pricing (see _close).\n"
        "            try:\n"
        '                _cb = getattr(self, "_chain_by_expiry", None)\n'
        "                if _cb is None:\n"
        "                    _cb = {}\n"
        '                    self._chain_by_expiry = _cb\n'
        '                _ax = self.me.state.get("actual_expiry") or signals.get("active_expiry")\n'
        "                if _ax and self.me.last_chain:\n"
        "                    _cb[str(_ax)[:10]] = dict(self.me.last_chain)\n"
        "            except Exception:\n"
        "                pass\n",
        "_cb[str(_ax)[:10]] = dict(self.me.last_chain)",
    ),
    _h(
        "backtest_engine.py",
        "B1c:exit-on-position-expiry",
        "    def _close(self, live: dict, signals: dict, reason: str, priority: int,\n"
        "               day: DaySlice) -> Trade:\n"
        "        chain = self.me.last_chain or {}",
        "    def _close(self, live: dict, signals: dict, reason: str, priority: int,\n"
        "               day: DaySlice) -> Trade:\n"
        "        # PATCH_V12: price the exit on the POSITION's expiry chain,\n"
        "        # not the active one. The runner snapshots every active chain\n"
        "        # per cycle (see run_day), so a weekly spread held across a\n"
        "        # Tuesday 0DTE listing exits on its own last-known quotes\n"
        "        # instead of the 0DTE lottery tickets (measured 2026-09-08:\n"
        "        # exit debit 0.93 on the wrong series turned ~Rs 750 of decay\n"
        "        # into Rs 8,993).\n"
        "        chain = self.me.last_chain or {}\n"
        "        try:\n"
        '            _pos_exp = str((live.get("params") or {}).get("target_expiry") or "")[:10]\n'
        '            _by_exp = getattr(self, "_chain_by_expiry", None) or {}\n'
        "            if _pos_exp and _pos_exp in _by_exp:\n"
        "                chain = _by_exp[_pos_exp]\n"
        "        except Exception:\n"
        "            pass",
        "chain = _by_exp[_pos_exp]",
    ),
    # =====================================================================
    # main.py — consult decide() on regime refusals (momentum parity)
    # =====================================================================
    _h(
        "main.py",
        "M1:decide-on-notrade",
        "                bool(signals.get(\"or_computed\", False)) and\n"
        '                signals.get("final_regime") not in ("NO_TRADE", "ABORT", None) and\n'
        "                not self._feed_stale",
        "                bool(signals.get(\"or_computed\", False)) and\n"
        "                # PATCH_V12: decide() also runs when the regime layer\n"
        "                # refused the sell side, so the long-premium momentum\n"
        "                # substitute is consulted exactly as in replay. The\n"
        "                # sell side cannot leak through: _check_hard_gates\n"
        "                # refuses every NO_TRADE regime before any structure\n"
        "                # is built, and ABORT still never reaches decide().\n"
        '                signals.get("final_regime") not in ("ABORT", None) and\n'
        "                not self._feed_stale",
        "not in (\"ABORT\", None) and",
    ),
]


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def main() -> int:
    root = Path.cwd()
    print("=" * 72)
    print("patch_v12.py — NIFTY engine profitability repair pass")
    print("=" * 72)

    # ── sanity: must run from the repo root ──────────────────────────────
    missing = [f for f in sorted(ALLOWED_FILES) if not (root / f).is_file()]
    if missing:
        print(f"ERROR: run from the repository root. Missing: {', '.join(missing)}")
        return 1

    applied: list = []
    skipped: list = []
    failed: list = []

    for h in HUNKS:
        path = root / h["file"]
        label = f"{h['file']}:{h['label']}"
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            _fail(f"{label}: cannot read ({exc})")
            failed.append(label)
            continue

        # Idempotency FIRST: if the verification string is already
        # present the hunk applied earlier. Several hunks preserve their
        # original text inside the replacement, so "old found" alone
        # cannot distinguish a fresh file from a patched one (re-running
        # must SKIP, never double-apply).
        if h["check"] in text:
            print(f"  [SKIP] {label} (already applied)")
            skipped.append(label)
            continue

        occurrences = text.count(h["old"])
        if occurrences == 1:
            # Backup once, before the first modification of each file.
            backup = path.with_name(path.name + ".v12bak")
            if not backup.exists():
                try:
                    shutil.copy2(path, backup)
                except Exception as exc:
                    _fail(f"{label}: cannot back up ({exc})")
                    failed.append(label)
                    continue
            text = text.replace(h["old"], h["new"], 1)
            if h["check"] not in text:
                _fail(f"{label}: applied but verification string missing")
                failed.append(label)
                continue
            try:
                path.write_text(text, encoding="utf-8")
            except Exception as exc:
                _fail(f"{label}: cannot write ({exc})")
                failed.append(label)
                continue
            print(f"  [OK]   {label}")
            applied.append(label)
        elif occurrences == 0:
            _fail(f"{label}: original text not found (0 occurrences) — file differs from expected")
            failed.append(label)
        else:
            _fail(f"{label}: original text matches {occurrences}x — refusing ambiguous hunk")
            failed.append(label)

    print("-" * 72)
    print(f"applied: {len(applied)}, skipped: {len(skipped)}, failed: {len(failed)}")

    if failed:
        print("PATCH INCOMPLETE — no further checks run. Restore from *.v12bak if needed.")
        return 1

    # ── byte-compile every touched file ──────────────────────────────────
    touched = sorted({h["file"] for h in HUNKS})
    print("byte-compiling touched files...")
    for f in touched:
        try:
            py_compile.compile(str(root / f), doraise=True)
            print(f"  [OK]   {f} compiles")
        except Exception as exc:
            _fail(f"{f} does not compile: {exc}")
            return 1

    print("=" * 72)
    print("PATCH_V12 APPLIED SUCCESSFULLY.")
    print("Validate with, e.g.:")
    print("  python3 backtest_engine.py --db data/per_day/nifty_algo_2026-09-10.db \\")
    print("      --from 2026-09-10 --to 2026-09-10 --trade-report off")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
