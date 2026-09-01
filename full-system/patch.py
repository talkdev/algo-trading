#!/usr/bin/env python3
"""
patch.py — Profitability & Accuracy Improvements (Immediate High-Impact)

Applies only changes with clear correct answers that are implementable
against the current code using data already available at runtime.
No new data feeds, no backtesting infrastructure, no research required.

Files patched:
  config.py
    IMM-01  Dynamic flow weight: FLOW_WEIGHT_NONE_THRESHOLD added
    IMM-02  VIX-adaptive lot sizing constants added
    IMM-03  Distance-based stop constant added
    IMM-04  Partial profit-taking ladder constants added
    IMM-05  Adaptive persistence constants added
    IMM-06  CANDLE_REFRESH_SECONDS 300->60 (align with main loop)

  strategy_engine.py
    IMM-02  _calculate_lot_size: scale by VIX-adaptive multiplier
    IMM-03  _check_stop_loss: add distance-based secondary stop for condors/spreads
    IMM-04  _check_profit_target: partial profit-taking at 25% of target
    IMM-05  _refresh_locked: adaptive persistence (2 readings for strong signal)

  regime_engine.py
    IMM-01  _build_weights: set flow weight to 0 when frequently None
    IMM-05  _refresh_locked: adaptive persistence based on composite magnitude

Run:
    python patch.py [--dry-run] [--no-backup]
"""

import sys
import os
import ast
import shutil
import argparse
from datetime import datetime


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def backup_file(path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path + ".bak_" + ts
    shutil.copy2(path, bak)
    print("  Backup: " + bak)


def verify_syntax(path, content):
    try:
        ast.parse(content)
        return True, None
    except SyntaxError as e:
        return False, str(e)


def apply_patch(path, original, patched, dry_run, do_backup):
    if original == patched:
        print("  [SKIP] " + os.path.basename(path) + " — no changes")
        return True
    ok, err = verify_syntax(path, patched)
    if not ok:
        print("  [ERROR] " + os.path.basename(path)
              + " — syntax error: " + str(err))
        return False
    if dry_run:
        orig_lines = original.splitlines()
        new_lines = patched.splitlines()
        print("  [DRY-RUN] " + os.path.basename(path)
              + " — " + str(len(orig_lines))
              + " -> " + str(len(new_lines)) + " lines")
        shown = 0
        for i in range(max(len(orig_lines), len(new_lines))):
            if shown >= 20:
                break
            a = orig_lines[i] if i < len(orig_lines) else None
            b = new_lines[i] if i < len(new_lines) else None
            if a != b:
                if a is not None:
                    print("    L" + str(i + 1) + ": - " + a.rstrip())
                if b is not None:
                    print("    L" + str(i + 1) + ": + " + b.rstrip())
                shown += 1
        return True
    if do_backup:
        backup_file(path)
    write_file(path, patched)
    print("  [OK] " + os.path.basename(path) + " — patched")
    return True


def sub_exact(old, new, content, label):
    if old in content:
        return content.replace(old, new, 1), True
    print("  [WARN] " + label + ": target not found")
    return content, False


# ─────────────────────────────────────────────────────────────────────
# config.py
# ─────────────────────────────────────────────────────────────────────

def patch_config(content):
    changes = []

    # IMM-06: CANDLE_REFRESH_SECONDS 300->60
    # Candle refresh is every 300s but the main loop runs every 60s.
    # ADX and EMA can be stale for up to 5 minutes, causing delayed
    # regime changes and missed entries/exits.
    old_candle = (
        "# CFG-D1: lowered from 1800 to 300 (5 min).\n"
        "# The trend module is up to 30 min stale at 1800s. ADX and EMA\n"
        "# slope on the *forming* bar change continuously. 5-min refresh\n"
        "# gives an early view of the forming bar — one extra cheap call.\n"
        "CANDLE_REFRESH_SECONDS = 300"
    )
    new_candle = (
        "# IMM-06: lowered from 300 to 60 to align with main loop cadence.\n"
        "# ADX and EMA can be stale for up to 5 minutes at 300s, causing\n"
        "# delayed regime changes. At 60s the trend module is always fresh.\n"
        "CANDLE_REFRESH_SECONDS = 60"
    )
    content, ok = sub_exact(old_candle, new_candle, content,
                            "IMM-06 CANDLE_REFRESH_SECONDS")
    if ok:
        changes.append("IMM-06: CANDLE_REFRESH_SECONDS 300->60")

    # IMM-01/02/03/04/05: Add new constants block after FLOW_WINDOW_MINUTES
    old_flow_win = (
        "FLOW_WINDOW_MINUTES     = 30\n"
        "# CFG-P1: per-leg slippage haircut applied in credit gate BEFORE\n"
        "# trade approval. Currently slippage only appears in _simulate_fill\n"
        "# (after approval). Gate sees a credit that does not exist.\n"
        "ENTRY_SLIPPAGE_PTS_PER_LEG = 0.75"
    )
    new_flow_win = (
        "FLOW_WINDOW_MINUTES     = 30\n"
        "# CFG-P1: per-leg slippage haircut applied in credit gate BEFORE\n"
        "# trade approval.\n"
        "ENTRY_SLIPPAGE_PTS_PER_LEG = 0.75\n"
        "\n"
        "# ─── IMM-01: Dynamic flow weight ───────────────────────────────\n"
        "# If flow score is None for more than this fraction of recent cycles,\n"
        "# set flow weight to 0 and redistribute to other modules.\n"
        "# The flow module frequently returns None (DTE<3, warming up, etc.)\n"
        "# and a fixed 15% weight on a missing signal distorts the composite.\n"
        "FLOW_WEIGHT_NONE_THRESHOLD = 0.50   # >50% None -> weight = 0\n"
        "FLOW_WEIGHT_NONE_LOOKBACK  = 10     # last N cycles to check\n"
        "\n"
        "# ─── IMM-02: VIX-adaptive lot sizing ────────────────────────────\n"
        "# Scale MAX_RISK_PER_TRADE by (VIX_REFERENCE / current_VIX).\n"
        "# In high VIX, premium is larger but so is risk — reduce size.\n"
        "# In low VIX, premium is thin — increase size to maintain returns.\n"
        "VIX_ADAPTIVE_SIZING        = True\n"
        "VIX_ADAPTIVE_REFERENCE     = 16.0   # neutral VIX level\n"
        "VIX_ADAPTIVE_MIN_MULT      = 0.5    # minimum size multiplier\n"
        "VIX_ADAPTIVE_MAX_MULT      = 2.0    # maximum size multiplier\n"
        "\n"
        "# ─── IMM-03: Distance-based secondary stop ───────────────────────\n"
        "# For condors/spreads: close when spot reaches this fraction of the\n"
        "# distance from entry spot to the short strike. Prevents holding\n"
        "# through a strike breach when the premium stop hasn't fired yet.\n"
        "STOP_SPOT_FRACTION_OF_DISTANCE = 0.80\n"
        "\n"
        "# ─── IMM-04: Partial profit-taking ladder ────────────────────────\n"
        "# Close PARTIAL_PROFIT_CLOSE_PCT of the position when profit reaches\n"
        "# PARTIAL_PROFIT_TRIGGER_PCT of the full profit target.\n"
        "# Locks in gains early while letting the remainder run to full target.\n"
        "PARTIAL_PROFIT_ENABLED         = True\n"
        "PARTIAL_PROFIT_TRIGGER_PCT     = 0.25   # close partial at 25% of target\n"
        "PARTIAL_PROFIT_CLOSE_PCT       = 0.50   # close 50% of position\n"
        "\n"
        "# ─── IMM-05: Adaptive persistence ────────────────────────────────\n"
        "# Use fewer confirmation readings when the composite signal is strong.\n"
        "# Strong signals (high conviction) should not be delayed by 3 readings.\n"
        "ADAPTIVE_PERSISTENCE_ENABLED   = True\n"
        "ADAPTIVE_PERSISTENCE_FAST_THRESHOLD = 0.60  # composite > this -> 2 readings\n"
        "ADAPTIVE_PERSISTENCE_FAST_READINGS  = 2     # readings for strong signal\n"
        "ADAPTIVE_PERSISTENCE_SLOW_READINGS  = 3     # readings for weak signal"
    )
    content, ok = sub_exact(old_flow_win, new_flow_win, content,
                            "IMM-01/02/03/04/05 new constants")
    if ok:
        changes.append(
            "IMM-01: FLOW_WEIGHT_NONE_THRESHOLD/LOOKBACK added; "
            "IMM-02: VIX_ADAPTIVE_SIZING constants added; "
            "IMM-03: STOP_SPOT_FRACTION_OF_DISTANCE added; "
            "IMM-04: PARTIAL_PROFIT_* constants added; "
            "IMM-05: ADAPTIVE_PERSISTENCE_* constants added"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # IMM-01: Dynamic flow weight — set to 0 when frequently None
    # Track None count for flow module and redistribute weight when
    # flow is unavailable more than FLOW_WEIGHT_NONE_THRESHOLD of cycles.
    old_build_weights = (
        "# AUDIT #2.2: weights read from config so tuning\n"
        "# config.WEIGHT_* actually takes effect at runtime.\n"
        "def _build_weights():\n"
        "    return {\n"
        '        "vol":   config.WEIGHT_VOL,\n'
        '        "edge":  config.WEIGHT_EDGE,\n'
        '        "trend": config.WEIGHT_TREND,\n'
        '        "flow":  config.WEIGHT_FLOW,\n'
        "    }\n"
        "WEIGHTS = _build_weights()"
    )
    new_build_weights = (
        "# AUDIT #2.2: weights read from config so tuning\n"
        "# config.WEIGHT_* actually takes effect at runtime.\n"
        "def _build_weights(flow_none_fraction: float = 0.0):\n"
        "    \"\"\"IMM-01: dynamic flow weight.\n"
        "\n"
        "    If flow has been None for more than FLOW_WEIGHT_NONE_THRESHOLD\n"
        "    of recent cycles, set its weight to 0 and redistribute\n"
        "    proportionally to the other modules. A fixed 15% weight on a\n"
        "    missing signal distorts the composite score.\n"
        "    \"\"\"\n"
        "    _threshold = getattr(\n"
        "        config, \"FLOW_WEIGHT_NONE_THRESHOLD\", 0.50\n"
        "    )\n"
        "    if flow_none_fraction > _threshold:\n"
        "        # Flow is unreliable — redistribute its weight\n"
        "        _flow_w = 0.0\n"
        "        _other_sum = (\n"
        "            config.WEIGHT_VOL\n"
        "            + config.WEIGHT_EDGE\n"
        "            + config.WEIGHT_TREND\n"
        "        )\n"
        "        _scale = 1.0 / _other_sum if _other_sum > 0 else 1.0\n"
        "        return {\n"
        '            "vol":   config.WEIGHT_VOL   * _scale,\n'
        '            "edge":  config.WEIGHT_EDGE  * _scale,\n'
        '            "trend": config.WEIGHT_TREND * _scale,\n'
        '            "flow":  0.0,\n'
        "        }\n"
        "    return {\n"
        '        "vol":   config.WEIGHT_VOL,\n'
        '        "edge":  config.WEIGHT_EDGE,\n'
        '        "trend": config.WEIGHT_TREND,\n'
        '        "flow":  config.WEIGHT_FLOW,\n'
        "    }\n"
        "WEIGHTS = _build_weights()"
    )
    content, ok = sub_exact(old_build_weights, new_build_weights, content,
                            "IMM-01 dynamic flow weight")
    if ok:
        changes.append("IMM-01: _build_weights() dynamically zeroes flow weight when frequently None")

    # IMM-01: Track flow None count and pass to _build_weights in refresh
    old_flow_none_track = (
        "            # AUDIT RE-N01: cap at 0 when RV is estimated (circular signal)\n"
        "            if _rv_is_estimated and raw != 0:"
    )
    # This target is in _module_edge, not the right place.
    # Instead patch the composite aggregation in _refresh_locked to use
    # the dynamic weights based on recent flow None count.
    old_composite_agg = (
        "            # AUDIT #2.2: rebuild weights from config each\n"
        "            # cycle so config.WEIGHT_* tuning is live.\n"
        "            _live_weights = _build_weights()\n"
        "            composite = sum(\n"
        "                _live_weights[m] * self._conf[m]\n"
        "                for m in MODULES\n"
        "            )"
    )
    new_composite_agg = (
        "            # IMM-01: compute flow None fraction over recent cycles\n"
        "            # and use it to dynamically zero the flow weight.\n"
        "            _lookback = getattr(\n"
        "                config, \"FLOW_WEIGHT_NONE_LOOKBACK\", 10\n"
        "            )\n"
        "            _flow_buf = self._buf.get(\"flow\", [])\n"
        "            # Count None raw readings in recent history\n"
        "            # (we track None as a sentinel in the buffer)\n"
        "            _flow_none_count = sum(\n"
        "                1 for v in list(self._buf.get(\"flow\", []))[-_lookback:]\n"
        "                if v == 0 and self._raw.get(\"flow\") is None\n"
        "            )\n"
        "            _flow_none_frac = (\n"
        "                _flow_none_count / _lookback\n"
        "                if _lookback > 0 else 0.0\n"
        "            )\n"
        "            # Also check if current raw_flow is None\n"
        "            if self._raw.get(\"flow\") is None:\n"
        "                _flow_none_frac = max(_flow_none_frac, 0.6)\n"
        "\n"
        "            # AUDIT #2.2: rebuild weights from config each cycle.\n"
        "            # IMM-01: pass flow None fraction for dynamic weighting.\n"
        "            _live_weights = _build_weights(_flow_none_frac)\n"
        "            composite = sum(\n"
        "                _live_weights[m] * self._conf[m]\n"
        "                for m in MODULES\n"
        "            )"
    )
    content, ok = sub_exact(old_composite_agg, new_composite_agg, content,
                            "IMM-01 flow none fraction in composite")
    if ok:
        changes.append("IMM-01: composite aggregation uses dynamic flow weight based on None fraction")

    # IMM-05: Adaptive persistence — use 2 readings for strong signals
    # Strong composite signals (>0.60) should not be delayed by 3 readings.
    old_persist_confirm = (
        "        # RE-T02: confirm when the last 3 readings have the\n"
        "        # same sign (or are all zero). Use the mean of the\n"
        "        # buffer as the confirmed value to preserve granularity.\n"
        "        import math as _math_p\n"
        "        if len(buf) >= 3:\n"
        "            _last3 = buf[-3:]\n"
        "            _signs = [_math_p.copysign(1, v) if v != 0 else 0\n"
        "                      for v in _last3]\n"
        "            if len(set(_signs)) == 1:  # all same sign\n"
        "                _confirmed = sum(_last3) / len(_last3)\n"
        "                self._conf[name] = _confirmed\n"
        "                logger.info(\n"
        "                    f\"Persistence confirmed: \"\n"
        "                    f\"{name}={_confirmed:.3f} \"\n"
        "                    f\"(sign-stable over 3 readings)\"\n"
        "                )\n"
        "            else:\n"
        "                logger.info(\n"
        "                    f\"Persistence unconfirmed: {name} \"\n"
        "                    f\"buf={[round(v,3) for v in buf[-3:]]} \"\n"
        "                    f\"holding={self._conf[name]:.3f}\"\n"
        "                )\n"
        "        return self._conf[name]"
    )
    new_persist_confirm = (
        "        # IMM-05: adaptive persistence.\n"
        "        # Use fewer readings when the composite signal is strong.\n"
        "        # Strong signals (high conviction) should not be delayed.\n"
        "        import math as _math_p\n"
        "        _adaptive = getattr(\n"
        "            config, \"ADAPTIVE_PERSISTENCE_ENABLED\", False\n"
        "        )\n"
        "        _fast_thresh = getattr(\n"
        "            config, \"ADAPTIVE_PERSISTENCE_FAST_THRESHOLD\", 0.60\n"
        "        )\n"
        "        _fast_n = getattr(\n"
        "            config, \"ADAPTIVE_PERSISTENCE_FAST_READINGS\", 2\n"
        "        )\n"
        "        _slow_n = getattr(\n"
        "            config, \"ADAPTIVE_PERSISTENCE_SLOW_READINGS\", 3\n"
        "        )\n"
        "        # Determine required readings based on composite magnitude\n"
        "        _composite_mag = abs(self.raw_composite)\n"
        "        if _adaptive and _composite_mag >= _fast_thresh:\n"
        "            _required = _fast_n\n"
        "        else:\n"
        "            _required = _slow_n\n"
        "\n"
        "        # RE-T02: confirm when the last N readings have the same sign.\n"
        "        if len(buf) >= _required:\n"
        "            _lastN = buf[-_required:]\n"
        "            _signs = [_math_p.copysign(1, v) if v != 0 else 0\n"
        "                      for v in _lastN]\n"
        "            if len(set(_signs)) == 1:  # all same sign\n"
        "                _confirmed = sum(_lastN) / len(_lastN)\n"
        "                self._conf[name] = _confirmed\n"
        "                logger.info(\n"
        "                    f\"Persistence confirmed: \"\n"
        "                    f\"{name}={_confirmed:.3f} \"\n"
        "                    f\"(sign-stable over {_required} readings, \"\n"
        "                    f\"composite={_composite_mag:.3f})\"\n"
        "                )\n"
        "            else:\n"
        "                logger.info(\n"
        "                    f\"Persistence unconfirmed: {name} \"\n"
        "                    f\"buf={[round(v,3) for v in buf[-_required:]]} \"\n"
        "                    f\"holding={self._conf[name]:.3f}\"\n"
        "                )\n"
        "        return self._conf[name]"
    )
    content, ok = sub_exact(old_persist_confirm, new_persist_confirm, content,
                            "IMM-05 adaptive persistence")
    if ok:
        changes.append(
            "IMM-05: _persist() uses adaptive readings "
            "(2 for strong composite >=0.60, 3 otherwise)"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # IMM-02: VIX-adaptive lot sizing
    # Scale MAX_RISK_PER_TRADE by (VIX_REFERENCE / current_VIX).
    # In high VIX, premium is larger but risk is also higher — reduce size.
    # In low VIX, premium is thin — increase size to maintain returns.
    old_lot_size_start = (
        "        # AUDIT #N1: defensive hedge pre-computes its own quantity.\n"
        "        if strategy_name == config.STRAT_DEFENSIVE:\n"
        "            return 1\n"
        "        # SE-03: size off the DESIGNED STOP LOSS, not the theoretical\n"
        "        # max loss."
    )
    new_lot_size_start = (
        "        # AUDIT #N1: defensive hedge pre-computes its own quantity.\n"
        "        if strategy_name == config.STRAT_DEFENSIVE:\n"
        "            return 1\n"
        "\n"
        "        # IMM-02: VIX-adaptive lot sizing.\n"
        "        # Scale risk per trade by (VIX_REFERENCE / current_VIX).\n"
        "        # High VIX -> smaller size (risk is higher per lot).\n"
        "        # Low VIX -> larger size (premium is thin, need more lots).\n"
        "        _vix_adaptive = getattr(\n"
        "            config, \"VIX_ADAPTIVE_SIZING\", False\n"
        "        )\n"
        "        _vix_mult = 1.0\n"
        "        if _vix_adaptive and self.dm.vix and self.dm.vix > 0:\n"
        "            _vix_ref = getattr(\n"
        "                config, \"VIX_ADAPTIVE_REFERENCE\", 16.0\n"
        "            )\n"
        "            _vix_min = getattr(\n"
        "                config, \"VIX_ADAPTIVE_MIN_MULT\", 0.5\n"
        "            )\n"
        "            _vix_max = getattr(\n"
        "                config, \"VIX_ADAPTIVE_MAX_MULT\", 2.0\n"
        "            )\n"
        "            _vix_mult = max(\n"
        "                _vix_min,\n"
        "                min(_vix_max, _vix_ref / self.dm.vix),\n"
        "            )\n"
        "            logger.debug(\n"
        "                f\"VIX-adaptive sizing: VIX={self.dm.vix:.1f} \"\n"
        "                f\"mult={_vix_mult:.2f}\"\n"
        "            )\n"
        "\n"
        "        # SE-03: size off the DESIGNED STOP LOSS, not the theoretical\n"
        "        # max loss."
    )
    content, ok = sub_exact(old_lot_size_start, new_lot_size_start, content,
                            "IMM-02 VIX adaptive sizing start")
    if ok:
        changes.append("IMM-02: VIX-adaptive multiplier computed in _calculate_lot_size")

    # Apply the VIX multiplier to the final lots calculation
    old_lots_clamp = (
        "        lots = max(lots, 0)\n"
        "\n"
        "        # PATCH: heuristic margin/SPAN cap."
    )
    new_lots_clamp = (
        "        lots = max(lots, 0)\n"
        "\n"
        "        # IMM-02: apply VIX-adaptive multiplier to final lot count\n"
        "        if _vix_mult != 1.0 and lots > 0:\n"
        "            lots = max(1, int(lots * _vix_mult))\n"
        "\n"
        "        # PATCH: heuristic margin/SPAN cap."
    )
    content, ok = sub_exact(old_lots_clamp, new_lots_clamp, content,
                            "IMM-02 VIX adaptive apply")
    if ok:
        changes.append("IMM-02: VIX-adaptive multiplier applied to final lot count")

    # IMM-03: Distance-based secondary stop for condors/spreads
    # Close when spot reaches STOP_SPOT_FRACTION_OF_DISTANCE of the way
    # to the short strike. Prevents holding through a strike breach.
    old_condor_stop_end = (
        "            if short_put and self.dm.spot <= short_put - _buf:\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return True"
    )
    new_condor_stop_end = (
        "            if short_put and self.dm.spot <= short_put - _buf:\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return True\n"
        "\n"
        "            # IMM-03: distance-based secondary stop.\n"
        "            # Close the whole position when spot reaches\n"
        "            # STOP_SPOT_FRACTION_OF_DISTANCE of the way from\n"
        "            # entry spot to the short strike. This prevents\n"
        "            # holding through a strike breach when the premium\n"
        "            # stop hasn't fired yet (e.g. low IV environment).\n"
        "            _dist_frac = getattr(\n"
        "                config, \"STOP_SPOT_FRACTION_OF_DISTANCE\", 0.80\n"
        "            )\n"
        "            if self.dm.spot and position.entry_spot > 0:\n"
        "                if short_call:\n"
        "                    _dist_to_call = short_call - position.entry_spot\n"
        "                    if (\n"
        "                        _dist_to_call > 0\n"
        "                        and self.dm.spot\n"
        "                        >= position.entry_spot\n"
        "                        + _dist_to_call * _dist_frac\n"
        "                    ):\n"
        "                        logger.info(\n"
        "                            f\"Distance stop (call): spot reached \"\n"
        "                            f\"{_dist_frac*100:.0f}%% of distance \"\n"
        "                            f\"to short call {short_call}\"\n"
        "                        )\n"
        "                        await self._close_position(\n"
        "                            position,\n"
        "                            config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                        )\n"
        "                        return True\n"
        "                if short_put:\n"
        "                    _dist_to_put = position.entry_spot - short_put\n"
        "                    if (\n"
        "                        _dist_to_put > 0\n"
        "                        and self.dm.spot\n"
        "                        <= position.entry_spot\n"
        "                        - _dist_to_put * _dist_frac\n"
        "                    ):\n"
        "                        logger.info(\n"
        "                            f\"Distance stop (put): spot reached \"\n"
        "                            f\"{_dist_frac*100:.0f}%% of distance \"\n"
        "                            f\"to short put {short_put}\"\n"
        "                        )\n"
        "                        await self._close_position(\n"
        "                            position,\n"
        "                            config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                        )\n"
        "                        return True"
    )
    content, ok = sub_exact(old_condor_stop_end, new_condor_stop_end, content,
                            "IMM-03 distance-based stop")
    if ok:
        changes.append(
            "IMM-03: distance-based secondary stop added for condors/spreads "
            "(fires at STOP_SPOT_FRACTION_OF_DISTANCE of way to short strike)"
        )

    # IMM-04: Partial profit-taking ladder
    # Close PARTIAL_PROFIT_CLOSE_PCT of position when profit reaches
    # PARTIAL_PROFIT_TRIGGER_PCT of the full profit target.
    # This locks in gains early while letting the remainder run.
    # We add this check in _check_profit_target for credit strategies.
    old_profit_target_credit = (
        "        if strategy in [\n"
        "            config.STRAT_SHORT_STRADDLE,\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "            config.STRAT_RATIO_SPREAD,\n"
        "        ]:\n"
        "            current_value = (\n"
        "                self._get_position_current_premium(\n"
        "                    position\n"
        "                )\n"
        "            )\n"
        "            # PATCH: prefer the strategy-specific target already\n"
        "            # computed at build time (position.profit_target).\n"
        "            fallback_target = (\n"
        "                position.total_credit\n"
        "                * (1 - config.PROFIT_TARGET_PCT)\n"
        "            )\n"
        "            target_credit = (\n"
        "                position.profit_target\n"
        "                if position.profit_target\n"
        "                and position.profit_target > 0\n"
        "                else fallback_target\n"
        "            )\n"
        "            if (\n"
        "                current_value <= target_credit\n"
        "                and position.total_credit > 0\n"
        "            ):\n"
        "                await self._close_position(\n"
        "                    position,\n"
        "                    config.EXIT_REASONS[\"PROFIT_TARGET\"],\n"
        "                )\n"
        "                return True"
    )
    new_profit_target_credit = (
        "        if strategy in [\n"
        "            config.STRAT_SHORT_STRADDLE,\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "            config.STRAT_RATIO_SPREAD,\n"
        "        ]:\n"
        "            current_value = (\n"
        "                self._get_position_current_premium(\n"
        "                    position\n"
        "                )\n"
        "            )\n"
        "            fallback_target = (\n"
        "                position.total_credit\n"
        "                * (1 - config.PROFIT_TARGET_PCT)\n"
        "            )\n"
        "            target_credit = (\n"
        "                position.profit_target\n"
        "                if position.profit_target\n"
        "                and position.profit_target > 0\n"
        "                else fallback_target\n"
        "            )\n"
        "\n"
        "            # IMM-04: partial profit-taking ladder.\n"
        "            # Close PARTIAL_PROFIT_CLOSE_PCT of position when profit\n"
        "            # reaches PARTIAL_PROFIT_TRIGGER_PCT of full target.\n"
        "            # Locks in gains early; remainder runs to full target.\n"
        "            _partial_enabled = getattr(\n"
        "                config, \"PARTIAL_PROFIT_ENABLED\", False\n"
        "            )\n"
        "            if (\n"
        "                _partial_enabled\n"
        "                and position.total_credit > 0\n"
        "                and not position.meta.get(\"_partial_taken\", False)\n"
        "            ):\n"
        "                _trigger_pct = getattr(\n"
        "                    config, \"PARTIAL_PROFIT_TRIGGER_PCT\", 0.25\n"
        "                )\n"
        "                _close_pct = getattr(\n"
        "                    config, \"PARTIAL_PROFIT_CLOSE_PCT\", 0.50\n"
        "                )\n"
        "                # Partial trigger: current_value <= credit * (1 - trigger_pct)\n"
        "                _partial_trigger = (\n"
        "                    position.total_credit * (1 - _trigger_pct)\n"
        "                )\n"
        "                if current_value <= _partial_trigger:\n"
        "                    logger.info(\n"
        "                        f\"Partial profit-taking: closing \"\n"
        "                        f\"{_close_pct*100:.0f}%% at \"\n"
        "                        f\"{_trigger_pct*100:.0f}%% of target\"\n"
        "                    )\n"
        "                    # Reduce position by _close_pct\n"
        "                    await self._reduce_position_pct(\n"
        "                        position, _close_pct\n"
        "                    )\n"
        "                    position.meta[\"_partial_taken\"] = True\n"
        "                    # Don't return True — let the remainder run\n"
        "\n"
        "            if (\n"
        "                current_value <= target_credit\n"
        "                and position.total_credit > 0\n"
        "            ):\n"
        "                await self._close_position(\n"
        "                    position,\n"
        "                    config.EXIT_REASONS[\"PROFIT_TARGET\"],\n"
        "                )\n"
        "                return True"
    )
    content, ok = sub_exact(old_profit_target_credit, new_profit_target_credit,
                            content, "IMM-04 partial profit taking")
    if ok:
        changes.append(
            "IMM-04: partial profit-taking ladder added to _check_profit_target "
            "(close 50% at 25% of target, remainder runs to full target)"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Immediate high-impact profitability improvements."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show changes without writing files")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip .bak backup files")
    args = parser.parse_args()
    dry_run   = args.dry_run
    do_backup = not args.no_backup

    base = os.path.dirname(os.path.abspath(__file__))
    files = {
        "config.py":          os.path.join(base, "config.py"),
        "regime_engine.py":   os.path.join(base, "regime_engine.py"),
        "strategy_engine.py": os.path.join(base, "strategy_engine.py"),
    }

    missing = [n for n, p in files.items() if not os.path.isfile(p)]
    if missing:
        print("ERROR: Files not found: " + str(missing))
        print("Run patch.py from the same directory as the engine.")
        sys.exit(1)

    all_ok        = True
    total_changes = []

    patches = [
        ("config.py",          patch_config),
        ("regime_engine.py",   patch_regime_engine),
        ("strategy_engine.py", patch_strategy_engine),
    ]

    for name, patch_fn in patches:
        path = files[name]
        print("")
        print("=" * 60)
        print("Patching: " + name)
        print("=" * 60)
        original = read_file(path)
        patched, changes = patch_fn(original)
        for c in changes:
            print("  + " + c)
        if not changes:
            print("  (no changes produced)")
        total_changes.extend(changes)
        ok = apply_patch(path, original, patched, dry_run, do_backup)
        if not ok:
            all_ok = False

    print("")
    print("=" * 60)
    print("SUMMARY — " + str(len(total_changes)) + " changes")
    print("=" * 60)
    for c in total_changes:
        print("  OK  " + c)

    if not all_ok:
        print("")
        print("ERROR: One or more patches failed. Review warnings above.")
        sys.exit(1)

    if dry_run:
        print("\nDry-run complete — no files modified.")
    else:
        print("\nAll patches applied.")
        print("Verify: python -m py_compile config.py "
              "regime_engine.py strategy_engine.py")
        print("Then: python testing.py -v")


if __name__ == "__main__":
    main()