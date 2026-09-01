#!/usr/bin/env python3
"""
patch.py — Single-file patch for NIFTY Options Algo Engine.

Applies all verified fixes across:
  - config.py
  - regime_engine.py
  - strategy_engine.py
  - data_manager.py

Run from the directory containing all four files:
    python patch.py

A backup of each file is created before modification.
"""

import os
import re
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_SUFFIX = datetime.now().strftime(".bak_%Y%m%d_%H%M%S")

applied = []
skipped = []
errors  = []


def backup(path):
    bak = path + BACKUP_SUFFIX
    shutil.copy2(path, bak)
    print(f"  Backup: {os.path.basename(bak)}")


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def apply_fix(fix_id, description, path, find, replace):
    full_path = os.path.join(BASE_DIR, path)
    if not os.path.exists(full_path):
        errors.append(f"{fix_id}: file not found — {path}")
        print(f"  ERROR  [{fix_id}] file not found: {path}")
        return False

    content = read_file(full_path)

    if find not in content:
        skipped.append(f"{fix_id}: pattern not found — {description}")
        print(f"  SKIP   [{fix_id}] pattern not found: {description}")
        return False

    new_content = content.replace(find, replace, 1)

    if new_content == content:
        skipped.append(f"{fix_id}: no change made — {description}")
        print(f"  SKIP   [{fix_id}] no change made: {description}")
        return False

    write_file(full_path, new_content)
    applied.append(f"{fix_id}: {description}")
    print(f"  OK     [{fix_id}] {description}")
    return True


def create_backups():
    print("\n=== Creating backups ===")
    for fname in [
        "config.py", "regime_engine.py",
        "strategy_engine.py", "data_manager.py",
    ]:
        fpath = os.path.join(BASE_DIR, fname)
        if os.path.exists(fpath):
            backup(fpath)
        else:
            print(f"  WARNING: {fname} not found — skipping backup")


# ─────────────────────────────────────────────────────────────────────
# CONFIG.PY FIXES
# ─────────────────────────────────────────────────────────────────────

def fix_config():
    print("\n=== config.py fixes ===")

    apply_fix(
        "C-01",
        "EDGE_RICH: 5.0 → 2.0 (regime_engine uses relative VRP)",
        "config.py",
        "EDGE_RICH  = 5.0   # reference: IV-RV > 5 -> rich",
        "EDGE_RICH  = 2.0   # PATCHED C-01: lowered; regime_engine uses relative VRP >= 15%",
    )

    apply_fix(
        "C-02",
        "STRADDLE_STOP_MULT: 2.0 → 1.2",
        "config.py",
        "STRADDLE_STOP_MULT     = 2.0   # stop = 2x credit",
        "STRADDLE_STOP_MULT     = 1.2   # PATCHED C-02: 2.0→1.2; break-even WR 80%→70.6%",
    )

    apply_fix(
        "C-03",
        "MAX_RISK_PER_TRADE_PCT: 0.02 → 0.04",
        "config.py",
        "MAX_RISK_PER_TRADE_PCT   = 0.02",
        "MAX_RISK_PER_TRADE_PCT   = 0.04  # PATCHED C-03: 0.02→0.04",
    )

    apply_fix(
        "C-05",
        "STRONG_SELL_ENTER: 0.45 → 0.30",
        "config.py",
        "STRONG_SELL_ENTER =  0.45   # enter STRONG_SELL above this",
        "STRONG_SELL_ENTER =  0.30   # PATCHED C-05: 0.45→0.30 for VIX=11-14 environment",
    )

    apply_fix(
        "C-06",
        "STRONG_SELL_EXIT: 0.40 → 0.22 (hysteresis band 0.05→0.08)",
        "config.py",
        "STRONG_SELL_EXIT  =  0.40   # exit  STRONG_SELL below this",
        "STRONG_SELL_EXIT  =  0.22   # PATCHED C-06: 0.40→0.22 (band now 0.08)",
    )

    apply_fix(
        "C-07",
        "MILD_SELL_ENTER: 0.15 → 0.10",
        "config.py",
        "MILD_SELL_ENTER   =  0.15   # enter MILD_SELL above this",
        "MILD_SELL_ENTER   =  0.10   # PATCHED C-07: 0.15→0.10",
    )

    apply_fix(
        "C-08",
        "MILD_SELL_EXIT: 0.10 → 0.02 (hysteresis band 0.05→0.08)",
        "config.py",
        "MILD_SELL_EXIT    =  0.10   # exit  MILD_SELL below this",
        "MILD_SELL_EXIT    =  0.02   # PATCHED C-08: 0.10→0.02 (band now 0.08)",
    )

    apply_fix(
        "C-09",
        "STRONG_SELL_THRESHOLD: 0.45 → 0.30",
        "config.py",
        "STRONG_SELL_THRESHOLD =  0.45   # reference: x > 0.45",
        "STRONG_SELL_THRESHOLD =  0.30   # PATCHED C-09: 0.45→0.30",
    )

    apply_fix(
        "C-10",
        "MILD_SELL_THRESHOLD: 0.15 → 0.10",
        "config.py",
        "MILD_SELL_THRESHOLD   =  0.15   # reference: x >= 0.15",
        "MILD_SELL_THRESHOLD   =  0.10   # PATCHED C-10: 0.15→0.10",
    )

    apply_fix(
        "C-11",
        "ADAPTIVE_PERSISTENCE_FAST_THRESHOLD: 0.60 → 0.35",
        "config.py",
        "ADAPTIVE_PERSISTENCE_FAST_THRESHOLD = 0.60  # composite > this -> 2 readings",
        "ADAPTIVE_PERSISTENCE_FAST_THRESHOLD = 0.35  # PATCHED C-11: 0.60→0.35",
    )

    apply_fix(
        "C-13",
        "ADX_TREND_THRESHOLD: 20 → 18",
        "config.py",
        "ADX_TREND_THRESHOLD = 20   # AUDIT #2.2: now read by regime_engine.py via ADX_TREND",
        "ADX_TREND_THRESHOLD = 18   # PATCHED C-13: 20→18 (30-min bar calibration)",
    )

    apply_fix(
        "C-14",
        "ADX_RANGE_THRESHOLD: 15 → 13",
        "config.py",
        "ADX_RANGE_THRESHOLD = 15",
        "ADX_RANGE_THRESHOLD = 13   # PATCHED C-14: 15→13",
    )

    apply_fix(
        "C-15",
        "MIN_VIX_SELL: 11.0 → 9.5",
        "config.py",
        "MIN_VIX_SELL     = 11.0",
        "MIN_VIX_SELL     = 9.5   # PATCHED C-15: 11.0→9.5",
    )

    apply_fix(
        "C-16",
        "CONDOR_MIN_CREDIT_PCT_OF_WIDTH: 0.18 → 0.12",
        "config.py",
        "CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.18",
        "CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.12  # PATCHED C-16: 0.18→0.12",
    )

    # C-17 and C-18: MIN_COMPOSITE thresholds live inside getattr() calls
    # in strategy_engine.py, not in config.py as string literals.
    # Patch them directly in strategy_engine.py instead.
    apply_fix(
        "C-17",
        "MIN_COMPOSITE_FOR_STRONG_SELL: 0.52 → 0.30 (in strategy_engine.py getattr)",
        "strategy_engine.py",
        '"MIN_COMPOSITE_FOR_STRONG_SELL", 0.52',
        '"MIN_COMPOSITE_FOR_STRONG_SELL", 0.30  # PATCHED C-17',
    )

    apply_fix(
        "C-18",
        "MIN_COMPOSITE_FOR_MILD_SELL: 0.22 → 0.10 (in strategy_engine.py getattr)",
        "strategy_engine.py",
        '"MIN_COMPOSITE_FOR_MILD_SELL", 0.22',
        '"MIN_COMPOSITE_FOR_MILD_SELL", 0.10  # PATCHED C-18',
    )

    apply_fix(
        "C-19",
        "CONDOR_TARGET_PCT: 0.50 → 0.60",
        "config.py",
        "CONDOR_TARGET_PCT         = 0.50",
        "CONDOR_TARGET_PCT         = 0.60  # PATCHED C-19: 0.50→0.60",
    )

    apply_fix(
        "C-20",
        "SPREAD_TARGET_PCT: 0.50 → 0.60",
        "config.py",
        "SPREAD_TARGET_PCT     = 0.50",
        "SPREAD_TARGET_PCT     = 0.60  # PATCHED C-20: 0.50→0.60",
    )

    apply_fix(
        "C-21",
        "STRADDLE_TARGET_PCT: 0.50 → 0.60",
        "config.py",
        "STRADDLE_TARGET_PCT    = 0.50",
        "STRADDLE_TARGET_PCT    = 0.60  # PATCHED C-21: 0.50→0.60",
    )


# ─────────────────────────────────────────────────────────────────────
# REGIME_ENGINE.PY FIXES
# ─────────────────────────────────────────────────────────────────────

def fix_regime_engine():
    print("\n=== regime_engine.py fixes ===")

    # R-01: _build_weights() wrong signature call
    apply_fix(
        "R-01",
        "_build_weights() called with wrong argument — remove argument",
        "regime_engine.py",
        (
            "            _log_weights = _build_weights(\n"
            "                getattr(self, \"_last_flow_none_frac\", 0.0)\n"
            "            )"
        ),
        "            _log_weights = _build_weights()  # PATCHED R-01: removed invalid argument",
    )

    # R-02: _composite_history never populated
    apply_fix(
        "R-02",
        "_composite_history.append() after raw_composite is set",
        "regime_engine.py",
        (
            "            self.raw_composite = float(\n"
            "                max(-1.0, min(1.0, composite))\n"
            "            )"
        ),
        (
            "            self.raw_composite = float(\n"
            "                max(-1.0, min(1.0, composite))\n"
            "            )\n"
            "            # PATCHED R-02: populate composite history for FILTER-11c\n"
            "            self._composite_history.append(self.raw_composite)"
        ),
    )

    # R-03: SKEW_MIN_DAYS 20 → 10
    apply_fix(
        "R-03",
        "SKEW_MIN_DAYS: 20 → 10",
        "regime_engine.py",
        "SKEW_MIN_DAYS    = 20     # minimum history before z is trusted",
        "SKEW_MIN_DAYS    = 10     # PATCHED R-03: 20→10",
    )

    # R-04: 25-delta tolerance 0.05 → 0.08
    apply_fix(
        "R-04",
        "25-delta skew tolerance: 0.05 → 0.08",
        "regime_engine.py",
        (
            "            and abs(best_c[0] - 0.25) < 0.05\n"
            "            and abs(best_p[0] + 0.25) < 0.05"
        ),
        (
            "            and abs(best_c[0] - 0.25) < 0.08  # PATCHED R-04: 0.05→0.08\n"
            "            and abs(best_p[0] + 0.25) < 0.08  # PATCHED R-04: 0.05→0.08"
        ),
    )

    # R-05: Persistence sign-of-average replaces sign-stability
    apply_fix(
        "R-05",
        "Persistence: sign-of-average replaces sign-stability for float scores",
        "regime_engine.py",
        (
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
            "                )"
        ),
        (
            "        # PATCHED R-05: sign-of-average replaces sign-stability.\n"
            "        # Sign-stability breaks for float scores (vol=0.5 then 0.0 then 0.5\n"
            "        # never confirms because set([+1,0,+1])={0,1}).\n"
            "        # Average-sign handles mixed sub-signals correctly.\n"
            "        if len(buf) >= _required:\n"
            "            _lastN = buf[-_required:]\n"
            "            _avg = sum(_lastN) / len(_lastN)\n"
            "            if abs(_avg) >= 0.10:\n"
            "                self._conf[name] = _avg\n"
            "                logger.info(\n"
            "                    f\"Persistence confirmed: \"\n"
            "                    f\"{name}={_avg:.3f} \"\n"
            "                    f\"(avg-sign over {_required} readings, \"\n"
            "                    f\"composite={_composite_mag:.3f})\"\n"
            "                )\n"
            "            else:\n"
            "                logger.info(\n"
            "                    f\"Persistence unconfirmed: {name} \"\n"
            "                    f\"avg={_avg:.3f} < 0.10 \"\n"
            "                    f\"holding={self._conf[name]:.3f}\"\n"
            "                )"
        ),
    )

    # R-06: Edge module — relative VRP as primary signal
    apply_fix(
        "R-06",
        "Edge module: relative VRP >= 15% as primary signal",
        "regime_engine.py",
        (
            "        if edge > EDGE_RICH:\n"
            "            raw = 1\n"
            "            tag = \"RICH (seller edge)\"\n"
            "        elif edge < EDGE_CHEAP:\n"
            "            raw = -1\n"
            "            tag = \"CHEAP (buyer edge)\"\n"
            "        else:\n"
            "            raw = 0\n"
            "            tag = \"FAIR\""
        ),
        (
            "        # PATCHED R-06: relative VRP as primary signal.\n"
            "        # Absolute spread (EDGE_RICH) fires only at VIX>14.\n"
            "        # Relative VRP fires at VIX=11 (22%>15%) through VIX=22.\n"
            "        _vrp_rel = (iv_atm - rv_pct) / rv_pct if rv_pct > 0 else 0.0\n"
            "        _vrp_rich  =  0.15\n"
            "        _vrp_cheap = -0.05\n"
            "        if _vrp_rel >= _vrp_rich or edge > EDGE_RICH:\n"
            "            raw = 1\n"
            "            tag = f\"RICH (VRP_rel={_vrp_rel:.2%})\"\n"
            "        elif _vrp_rel <= _vrp_cheap or edge < EDGE_CHEAP:\n"
            "            raw = -1\n"
            "            tag = f\"CHEAP (VRP_rel={_vrp_rel:.2%})\"\n"
            "        else:\n"
            "            raw = 0\n"
            "            tag = f\"FAIR (VRP_rel={_vrp_rel:.2%})\""
        ),
    )

    # R-07: _build_weights() — implement flow weight redistribution
    apply_fix(
        "R-07",
        "_build_weights(): implement flow weight redistribution (IMM-01)",
        "regime_engine.py",
        (
            "def _build_weights():\n"
            "    return {\n"
            "        \"vol\":   config.WEIGHT_VOL,\n"
            "        \"edge\":  config.WEIGHT_EDGE,\n"
            "        \"trend\": config.WEIGHT_TREND,\n"
            "        \"flow\":  config.WEIGHT_FLOW,\n"
            "    }"
        ),
        (
            "def _build_weights(flow_none_frac=0.0):\n"
            "    \"\"\"PATCHED R-07: redistribute flow weight when flow is frequently None.\"\"\"\n"
            "    _threshold = getattr(config, \"FLOW_WEIGHT_NONE_THRESHOLD\", 0.50)\n"
            "    wv = config.WEIGHT_VOL\n"
            "    we = config.WEIGHT_EDGE\n"
            "    wt = config.WEIGHT_TREND\n"
            "    wf = config.WEIGHT_FLOW\n"
            "    if flow_none_frac > _threshold and wf > 0:\n"
            "        _other = wv + we + wt\n"
            "        if _other > 0:\n"
            "            _scale = (wv + we + wt + wf) / _other\n"
            "            wv = round(wv * _scale, 6)\n"
            "            we = round(we * _scale, 6)\n"
            "            wt = round(wt * _scale, 6)\n"
            "            wf = 0.0\n"
            "    return {\"vol\": wv, \"edge\": we, \"trend\": wt, \"flow\": wf}"
        ),
    )

    # R-08: Wire flow_none_frac into composite calculation
    # The pattern must match exactly what is in the file after R-01 applied.
    # R-01 changed the _log_weights call at the END of the method.
    # R-08 targets the _live_weights call inside the composite block.
    # We search for the comment that precedes _live_weights.
    apply_fix(
        "R-08",
        "Wire flow_none_frac into _build_weights() in composite calculation",
        "regime_engine.py",
        (
            "                    # AUDIT #2.2: rebuild weights from config\n"
            "                    # each cycle so config.WEIGHT_* tuning is live.\n"
            "                    _live_weights = _build_weights()"
        ),
        (
            "                    # PATCHED R-08: rebuild weights with flow_none_frac.\n"
            "                    _lookback_fw = getattr(\n"
            "                        config, \"FLOW_WEIGHT_NONE_LOOKBACK\", 10\n"
            "                    )\n"
            "                    _hist_fw = [\n"
            "                        h.get(\"raw_flow\")\n"
            "                        for h in list(self.score_history)[-_lookback_fw:]\n"
            "                    ]\n"
            "                    _hist_fw.append(self._raw.get(\"flow\"))\n"
            "                    _n_fw = len(_hist_fw)\n"
            "                    _none_fw = sum(1 for v in _hist_fw if v is None)\n"
            "                    _flow_none_frac = _none_fw / _n_fw if _n_fw > 0 else 0.0\n"
            "                    self._last_flow_none_frac = _flow_none_frac\n"
            "                    _live_weights = _build_weights(_flow_none_frac)"
        ),
    )

    # R-09: Macro pre-window — fire at T-1 market close
    apply_fix(
        "R-09",
        "Macro pre-window: fire at T-1 15:30 IST (not T 03:15 IST)",
        "regime_engine.py",
        (
            "                # Pre-window starts EVENT_PRE_HOURS before market open\n"
            "                pre_window_start = (\n"
            "                    event_market_open\n"
            "                    - __import__(\"datetime\").timedelta(\n"
            "                        hours=EVENT_PRE_HOURS\n"
            "                    )\n"
            "                )"
        ),
        (
            "                # PATCHED R-09: pre-window = T-1 market close (15:30).\n"
            "                # Old anchor (event_open - 6h = 03:15 IST) was always\n"
            "                # outside market hours so EVENT_HEDGE never fired before\n"
            "                # the event. New anchor fires during T-1 trading session.\n"
            "                _prev_close = self._IST.localize(\n"
            "                    __import__(\"datetime\").datetime.strptime(\n"
            "                        event_date_str, \"%Y-%m-%d\"\n"
            "                    ).replace(\n"
            "                        hour=15, minute=30,\n"
            "                        second=0, microsecond=0,\n"
            "                    )\n"
            "                ) - __import__(\"datetime\").timedelta(days=1)\n"
            "                pre_window_start = _prev_close"
        ),
    )

    # R-10: Term spread — near vs far weekly IV
    apply_fix(
        "R-10",
        "Term spread: use near vs far weekly IV when no 30-45 DTE expiry",
        "regime_engine.py",
        (
            "        elif _fwd_is_vix_proxy:\n"
            "            # RE-01: VIX proxy — not a real term spread, return None\n"
            "            term_score = None\n"
            "            term_txt   = \"T_spread n/a (forward_iv is VIX proxy)\"\n"
            "            notes.append(\"forward IV is VIX/100 proxy — not a term spread\")"
        ),
        (
            "        elif _fwd_is_vix_proxy:\n"
            "            # PATCHED R-10: use near vs far weekly IV when no 30-45 DTE expiry.\n"
            "            # Near = active expiry (DTE=4-8), Far = next weekly (DTE=10-16).\n"
            "            _far_iv_val = None\n"
            "            try:\n"
            "                import datetime as _dt_r10\n"
            "                _today_r10 = _dt_r10.date.today()\n"
            "                for _exp_r10 in sorted(self.dm.get_available_expiries()):\n"
            "                    _expd_r10 = _dt_r10.datetime.strptime(\n"
            "                        _exp_r10, \"%Y-%m-%d\"\n"
            "                    ).date()\n"
            "                    _dte_r10 = (_expd_r10 - _today_r10).days\n"
            "                    if 10 <= _dte_r10 <= 16:\n"
            "                        _fc_r10 = self.dm.get_chain_for_expiry(_exp_r10)\n"
            "                        if _fc_r10 and spot:\n"
            "                            _atm_r10 = min(\n"
            "                                _fc_r10.keys(),\n"
            "                                key=lambda k: abs(k - spot),\n"
            "                            )\n"
            "                            _c_iv = _fc_r10[_atm_r10].get(\"call\", {}).get(\"iv\", 0)\n"
            "                            _p_iv = _fc_r10[_atm_r10].get(\"put\",  {}).get(\"iv\", 0)\n"
            "                            if _c_iv > 0 and _p_iv > 0:\n"
            "                                _far_iv_val = (_c_iv + _p_iv) / 2.0\n"
            "                        break\n"
            "            except Exception:\n"
            "                _far_iv_val = None\n"
            "            if _far_iv_val is not None and _near_iv is not None and _near_iv > 0:\n"
            "                _near_pct_r10 = _near_iv * 100.0\n"
            "                _far_pct_r10  = _far_iv_val * 100.0\n"
            "                _ts_r10 = _far_pct_r10 - _near_pct_r10\n"
            "                if _ts_r10 > TERM_THRESHOLD:\n"
            "                    term_score = 1\n"
            "                elif _ts_r10 < -TERM_THRESHOLD:\n"
            "                    term_score = -1\n"
            "                else:\n"
            "                    term_score = 0\n"
            "                term_txt = (\n"
            "                    f\"T_spread(weekly) {_ts_r10:+.2f}%% \"\n"
            "                    f\"near={_near_pct_r10:.1f}%% far={_far_pct_r10:.1f}%%\"\n"
            "                )\n"
            "            else:\n"
            "                term_score = None\n"
            "                term_txt = \"T_spread n/a (VIX proxy, no far weekly IV)\"\n"
            "                notes.append(\"forward IV is VIX/100 proxy — not a term spread\")"
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# STRATEGY_ENGINE.PY FIXES
# ─────────────────────────────────────────────────────────────────────

def fix_strategy_engine():
    print("\n=== strategy_engine.py fixes ===")

    # S-01: _build_credit_spreads — assign side flags before first use
    apply_fix(
        "S-01",
        "_build_credit_spreads: assign _build_put_side/_build_call_side before first use",
        "strategy_engine.py",
        (
            "        # FIX-02: max_risk based on active sides only\n"
            "        _active_w = max(\n"
            "            put_width  if _build_put_side  else 0,\n"
            "            call_width if _build_call_side else 0,\n"
            "        )"
        ),
        (
            "        # PATCHED S-01: assign side flags BEFORE first use.\n"
            "        # Previously assigned after the credit gate that used them.\n"
            "        _build_put_side  = skew_side in (\"put\",  \"both\")\n"
            "        _build_call_side = skew_side in (\"call\", \"both\")\n"
            "\n"
            "        # FIX-02: max_risk based on active sides only\n"
            "        _active_w = max(\n"
            "            put_width  if _build_put_side  else 0,\n"
            "            call_width if _build_call_side else 0,\n"
            "        )"
        ),
    )

    # S-02: remove duplicate late assignment
    apply_fix(
        "S-02",
        "_build_credit_spreads: remove duplicate late assignment of side flags",
        "strategy_engine.py",
        (
            "        # PRF-02: only build the side(s) justified by skew.\n"
            "        _build_put_side  = skew_side in (\"put\",  \"both\")\n"
            "        _build_call_side = skew_side in (\"call\", \"both\")"
        ),
        (
            "        # PATCHED S-02: side flags already assigned above (S-01)."
        ),
    )

    # S-03: _vix_mult undefined in _calculate_lot_size
    apply_fix(
        "S-03",
        "_calculate_lot_size: define _vix_mult before use",
        "strategy_engine.py",
        (
            "        # IMM-02: apply VIX-adaptive multiplier to final lot count\n"
            "        if _vix_mult != 1.0 and lots > 0:\n"
            "            lots = max(1, int(lots * _vix_mult))\n"
            "        # Store for entry attempt logging\n"
            "        self._last_vix_mult = _vix_mult"
        ),
        (
            "        # PATCHED S-03: compute _vix_mult before use.\n"
            "        _vix_adaptive = getattr(config, \"VIX_ADAPTIVE_SIZING\", False)\n"
            "        _vix_ref_sz = getattr(config, \"VIX_ADAPTIVE_REFERENCE\", 16.0)\n"
            "        _vix_min_m = getattr(config, \"VIX_ADAPTIVE_MIN_MULT\", 0.5)\n"
            "        _vix_max_m = getattr(config, \"VIX_ADAPTIVE_MAX_MULT\", 2.0)\n"
            "        _cur_vix = (\n"
            "            self.dm.vix\n"
            "            if self.dm.vix and self.dm.vix > 0\n"
            "            else _vix_ref_sz\n"
            "        )\n"
            "        if _vix_adaptive and _cur_vix > 0:\n"
            "            _vix_mult = max(\n"
            "                _vix_min_m,\n"
            "                min(_vix_max_m, _vix_ref_sz / _cur_vix),\n"
            "            )\n"
            "        else:\n"
            "            _vix_mult = 1.0\n"
            "        # IMM-02: apply VIX-adaptive multiplier to final lot count\n"
            "        if _vix_mult != 1.0 and lots > 0:\n"
            "            lots = max(1, int(lots * _vix_mult))\n"
            "        # Store for entry attempt logging\n"
            "        self._last_vix_mult = _vix_mult"
        ),
    )

    # S-04: _defined_risk_budget undefined
    apply_fix(
        "S-04",
        "_calculate_lot_size: define _defined_risk_budget before use",
        "strategy_engine.py",
        (
            "        # FIX-09: use defined-risk budget if set\n"
            "        risk_per_trade = (\n"
            "            _defined_risk_budget\n"
            "            if _defined_risk_budget is not None\n"
            "            else config.MAX_RISK_PER_TRADE\n"
            "        )"
        ),
        (
            "        # PATCHED S-04: define _defined_risk_budget before use.\n"
            "        _defined_risk_budget = None\n"
            "        # FIX-09: use defined-risk budget if set\n"
            "        risk_per_trade = (\n"
            "            _defined_risk_budget\n"
            "            if _defined_risk_budget is not None\n"
            "            else config.MAX_RISK_PER_TRADE\n"
            "        )"
        ),
    )

    # S-05: condor stop 2.0 → 1.0
    apply_fix(
        "S-05",
        "_build_iron_condor: stop_loss credit * 2.0 → credit * 1.0",
        "strategy_engine.py",
        (
            "            # PRF-S05: raised from 1.25x to 2.0x credit.\n"
            "            # 1.25x stop = 50pt on a 40pt credit condor. NIFTY moves\n"
            "            # 50-80pt intraday routinely, causing many false stop-outs.\n"
            "            # 2.0x = 80pt stop, survives normal intraday noise.\n"
            "            \"stop_loss\":     net_credit * 2.0,"
        ),
        (
            "            # PATCHED S-05: 2.0x→1.0x stop.\n"
            "            # At 2.0x stop + 60% target, break-even WR=77% (unachievable).\n"
            "            # At 1.0x stop + 60% target, break-even WR=62.5% (achievable).\n"
            "            \"stop_loss\":     net_credit * 1.0,"
        ),
    )

    # S-06: credit spread stop 2.0 → 1.0
    apply_fix(
        "S-06",
        "_build_credit_spreads: stop_loss credit * 2.0 → credit * 1.0",
        "strategy_engine.py",
        (
            "            # PRF-S05: raised from 1.25x to 2.0x credit (same as condor).\n"
            "            \"stop_loss\":     total_credit * 2.0,"
        ),
        (
            "            # PATCHED S-06: 2.0x→1.0x (same reasoning as S-05).\n"
            "            \"stop_loss\":     total_credit * 1.0,"
        ),
    )

    # S-07: ratio spread stop 2.0 → 1.0
    # The ratio spread meta block ends just before _build_butterfly.
    # We target the specific stop_loss line inside the ratio spread meta dict.
    apply_fix(
        "S-07",
        "_build_ratio_spread: stop_loss credit * 2.0 → credit * 1.0",
        "strategy_engine.py",
        (
            "            \"stop_loss\":     total_credit * 2.0,\n"
            "            \"exit_dte\":      config.RATIO_EXIT_DTE,"
        ),
        (
            "            \"stop_loss\":     total_credit * 1.0,  # PATCHED S-07: 2.0x→1.0x\n"
            "            \"exit_dte\":      config.RATIO_EXIT_DTE,"
        ),
    )

    # S-08: STRONG_SELL always routes to straddle
    apply_fix(
        "S-08",
        "_select_strategy: STRONG_SELL always routes to straddle",
        "strategy_engine.py",
        (
            "        if regime == config.REGIME_STRONG_SELL:\n"
            "            # SE-B1: route to straddle by default.\n"
            "            # The condor is arithmetically unbuildable at 1.5sigma\n"
            "            # across the realistic VIX range (credit ~6-9% of width\n"
            "            # vs 18% required floor). The straddle has 2 legs (1.7pts\n"
            "            # cost vs 3.2), no wing debit, and maximum theta.\n"
            "            # Route to condor only when ADX confirms a genuine trend\n"
            "            # (tail risk warrants paying for wings).\n"
            "            if adx > config.ADX_TREND_THRESHOLD:\n"
            "                return config.STRAT_IRON_CONDOR\n"
            "            else:\n"
            "                return config.STRAT_SHORT_STRADDLE"
        ),
        (
            "        if regime == config.REGIME_STRONG_SELL:\n"
            "            # PATCHED S-08: always route to straddle.\n"
            "            # STRONG_SELL requires trend=+1 (ADX<ADX_RANGE_THRESHOLD),\n"
            "            # so ADX>ADX_TREND_THRESHOLD is impossible in this regime.\n"
            "            # The condor branch was permanently dead code.\n"
            "            return config.STRAT_SHORT_STRADDLE"
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# DATA_MANAGER.PY FIXES
# ─────────────────────────────────────────────────────────────────────

def fix_data_manager():
    print("\n=== data_manager.py fixes ===")

    # D-01: add meta_json column to open_positions CREATE TABLE
    apply_fix(
        "D-01",
        "init_sqlite: add meta_json column to open_positions CREATE TABLE",
        "data_manager.py",
        (
            "                    paper_trade        INTEGER DEFAULT 1,\n"
            "                    status             TEXT    DEFAULT 'OPEN',\n"
            "                    created_at         TEXT    DEFAULT CURRENT_TIMESTAMP\n"
            "                )"
        ),
        (
            "                    paper_trade        INTEGER DEFAULT 1,\n"
            "                    status             TEXT    DEFAULT 'OPEN',\n"
            "                    meta_json          TEXT    DEFAULT '{}',\n"
            "                    created_at         TEXT    DEFAULT CURRENT_TIMESTAMP\n"
            "                )"
        ),
    )

    # D-04: ALTER TABLE migration for existing databases
    # Insert before the closed_trades CREATE TABLE statement.
    apply_fix(
        "D-04",
        "init_sqlite: ALTER TABLE migration for existing databases",
        "data_manager.py",
        (
            "            cursor.execute(\"\"\"\n"
            "                CREATE TABLE IF NOT EXISTS closed_trades ("
        ),
        (
            "            # PATCHED D-04: safe migration for existing databases\n"
            "            try:\n"
            "                cursor.execute(\n"
            "                    \"ALTER TABLE open_positions \"\n"
            "                    \"ADD COLUMN meta_json TEXT DEFAULT '{}'\"\n"
            "                )\n"
            "            except sqlite3.OperationalError:\n"
            "                pass\n"
            "\n"
            "            cursor.execute(\"\"\"\n"
            "                CREATE TABLE IF NOT EXISTS closed_trades ("
        ),
    )

    # D-02: save_position INSERT — add meta_json column name
    # We target the column list in the INSERT statement.
    apply_fix(
        "D-02",
        "save_position: add meta_json to INSERT column list",
        "data_manager.py",
        (
            "                    paper_trade, status\n"
            "                ) VALUES (\n"
            "                    ?,?,?,?,?,?,?,?,?,?,\n"
            "                    ?,?,?,?,?,?,?,?,?,?,\n"
            "                    ?,?,?,?\n"
            "                )"
        ),
        (
            "                    paper_trade, status, meta_json\n"
            "                ) VALUES (\n"
            "                    ?,?,?,?,?,?,?,?,?,?,\n"
            "                    ?,?,?,?,?,?,?,?,?,?,\n"
            "                    ?,?,?,?,?\n"
            "                )"
        ),
    )

    # D-03: save_position VALUES tuple — add meta_json value
    apply_fix(
        "D-03",
        "save_position: add meta_json value to VALUES tuple",
        "data_manager.py",
        (
            "                1 if position_dict.get(\"paper_trade\")\n"
            "                else 0,\n"
            "                \"OPEN\",\n"
            "            ))"
        ),
        (
            "                1 if position_dict.get(\"paper_trade\")\n"
            "                else 0,\n"
            "                \"OPEN\",\n"
            "                position_dict.get(\"meta_json\", \"{}\"),\n"
            "            ))"
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# SYNTAX VERIFICATION
# ─────────────────────────────────────────────────────────────────────

def verify_syntax():
    print("\n=== Syntax verification ===")
    all_ok = True
    for fname in [
        "config.py", "regime_engine.py",
        "strategy_engine.py", "data_manager.py",
    ]:
        fpath = os.path.join(BASE_DIR, fname)
        if not os.path.exists(fpath):
            print(f"  SKIP   {fname} (not found)")
            continue
        try:
            source = read_file(fpath)
            compile(source, fname, "exec")
            print(f"  OK     {fname} — syntax valid")
        except SyntaxError as e:
            print(f"  ERROR  {fname} — SyntaxError: {e}")
            all_ok = False
    return all_ok


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("NIFTY Options Algo Engine — Patch Application")
    print("=" * 65)

    missing = []
    for fname in [
        "config.py", "regime_engine.py",
        "strategy_engine.py", "data_manager.py",
    ]:
        if not os.path.exists(os.path.join(BASE_DIR, fname)):
            missing.append(fname)
    if missing:
        print(f"\nERROR: Missing files: {missing}")
        print("Run patch.py from the directory containing all engine files.")
        sys.exit(1)

    create_backups()
    fix_config()
    fix_regime_engine()
    fix_strategy_engine()
    fix_data_manager()

    syntax_ok = verify_syntax()

    print("\n" + "=" * 65)
    print("PATCH SUMMARY")
    print("=" * 65)
    print(f"\nApplied  ({len(applied)}):")
    for a in applied:
        print(f"  OK  {a}")
    if skipped:
        print(f"\nSkipped  ({len(skipped)}):")
        for s in skipped:
            print(f"  --  {s}")
    if errors:
        print(f"\nErrors   ({len(errors)}):")
        for e in errors:
            print(f"  !!  {e}")

    print()
    if not syntax_ok:
        print("WARNING: Syntax errors detected after patching.")
        print("Restore from .bak files and report the failing fix ID.")
        sys.exit(1)
    elif errors:
        print("Patch complete with errors. Review error list above.")
        sys.exit(1)
    else:
        print("All patches applied. Syntax verified clean.")
        print(f"Backups saved with suffix: {BACKUP_SUFFIX}")


if __name__ == "__main__":
    main()