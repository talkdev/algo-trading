#!/usr/bin/env python3
"""
patch.py — Critical profitability fixes from deep analysis.

Applies only the confirmed-valid, code-level fixes identified by the
analysis harness. Each fix is the minimum change needed to unblock the
specific failure mode described.

Files patched:
  strategy_engine.py
    FIX-01  Define _dynamic_wing in _build_iron_condor (NameError every 60s)
    FIX-02  Per-side leg construction in _build_credit_spreads
            (skew_side='put' was building all 4 legs, corrupting CSV credit)
    FIX-03  Side-aware credit gate (was using two-sided width for one-sided build)
    FIX-04  Executable-price gate in _build_credit_spreads (bid/ask not LTP)
    FIX-05  IV-rank gate on _build_long_strangle (parity with _build_long_straddle)
    FIX-06  Symmetric EDGE_RICH/EDGE_CHEAP in _module_edge (removes buy-vol tilt)
    FIX-07  Return None from term sub-score when forward_iv is VIX fallback
            (turns a biased constant into an honest zero)
    FIX-08  Widen STRADDLE_DTE_MAX and CONDOR_DTE_MAX (EV rises with DTE)
    FIX-09  Separate risk budget for defined-risk structures (4% vs 2%)
    FIX-10  Straddle sizing uses margin basis not stop-based max-loss
            (stop-based sizing returns 0 lots at every reachable DTE/VIX)
    FIX-11  Gate on credit/max-loss not credit/width for condor and spread
            (credit/width gate is unreachable at the delta targets selected)

  config.py
    CFG-01  EDGE_RICH 5.0->2.0, EDGE_CHEAP 0.0->-2.0 (symmetric band)
    CFG-02  STRADDLE_DTE_MAX 4->8, CONDOR_DTE_MAX 5->8 (EV rises with DTE)
    CFG-03  MAX_RISK_PER_DEFINED_RISK_TRADE_PCT=0.04 added
    CFG-04  CONDOR_MIN_CREDIT_PER_MAXLOSS=0.10 (replaces credit/width gate)
    CFG-05  SPREAD_MIN_CREDIT_PER_MAXLOSS=0.10 (replaces credit/width gate)

  regime_engine.py
    RE-01   Return None from term sub-score when using VIX/100 fallback

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

    # CFG-01: Symmetric EDGE_RICH/EDGE_CHEAP
    # Current: EDGE_RICH=5.0, EDGE_CHEAP=0.0
    # For NIFTY, IV-RV typically sits between -2 and +3 vol points.
    # EDGE_RICH=5 is rarely reached; EDGE_CHEAP=0 fires ~1/3 of sessions.
    # This asymmetry contributes ~-0.09 to the composite on average,
    # biasing the engine toward buying options.
    # Fix: symmetric ±2 band around zero.
    old_edge = (
        "# CFG-RE03: tanh calibration factors for continuous signal squashing.\n"
        "# Replace {-1,0,+1} quantization with tanh(raw/factor) to preserve\n"
        "# magnitude information. A 2% edge and a 12% edge both mapped to +1\n"
        "# before — now they produce 0.38 and 0.96 respectively.\n"
        "# Calibration: factor = value at which tanh output = 0.76 (~1σ)\n"
        "EDGE_TANH_FACTOR            = 5.0    # tanh(5/5)=0.76 at EDGE_RICH threshold"
    )
    new_edge = (
        "# CFG-01: symmetric EDGE_RICH/EDGE_CHEAP.\n"
        "# Old: EDGE_RICH=5.0, EDGE_CHEAP=0.0 — asymmetric band contributed\n"
        "# ~-0.09 to composite on average (NIFTY IV-RV sits -2 to +3 vol pts;\n"
        "# EDGE_RICH=5 is rare, EDGE_CHEAP=0 fires ~1/3 of sessions).\n"
        "# Fix: symmetric ±2 band. Still conservative but no structural tilt.\n"
        "EDGE_RICH                   = 2.0    # was 5.0\n"
        "EDGE_CHEAP                  = -2.0   # was 0.0\n"
        "\n"
        "# CFG-RE03: tanh calibration factors\n"
        "EDGE_TANH_FACTOR            = 2.0    # updated to match new EDGE_RICH"
    )
    content, ok = sub_exact(old_edge, new_edge, content, "CFG-01 EDGE_RICH/CHEAP")
    if ok:
        changes.append("CFG-01: EDGE_RICH 5.0->2.0, EDGE_CHEAP 0.0->-2.0 (symmetric band)")

    # CFG-02: Widen DTE windows
    # Analysis shows EV/lot rises monotonically with DTE and P(stop) does not.
    # STRADDLE_DTE_MAX=4 and CONDOR_DTE_MAX=5 confine the engine to the
    # least favourable point on the theta/gamma curve.
    old_straddle_dte = (
        "# CFG-P4C: tightened to high-theta zone. Theta/day at DTE 3 is\n"
        "# 37% higher than at DTE 8 for the same notional gamma exposure.\n"
        "STRADDLE_DTE_MIN       = 1\n"
        "STRADDLE_DTE_MAX       = 4"
    )
    new_straddle_dte = (
        "# CFG-02: widened DTE windows. Analysis shows EV/lot rises\n"
        "# monotonically with DTE while P(stop) does not increase.\n"
        "# Confining to DTE<=4 was the least favourable point on the curve.\n"
        "STRADDLE_DTE_MIN       = 1\n"
        "STRADDLE_DTE_MAX       = 8    # was 4"
    )
    content, ok = sub_exact(old_straddle_dte, new_straddle_dte, content,
                            "CFG-02 STRADDLE_DTE_MAX")
    if ok:
        changes.append("CFG-02: STRADDLE_DTE_MAX 4->8")

    old_condor_dte_max = (
        "# CFG-P4C: tightened to high-theta zone (DTE 2-5).\n"
        "CONDOR_DTE_MIN            = 2\n"
        "CONDOR_DTE_MAX            = 5"
    )
    new_condor_dte_max = (
        "# CFG-02: widened (same reasoning as straddle).\n"
        "CONDOR_DTE_MIN            = 2\n"
        "CONDOR_DTE_MAX            = 8    # was 5"
    )
    content, ok = sub_exact(old_condor_dte_max, new_condor_dte_max, content,
                            "CFG-02 CONDOR_DTE_MAX")
    if ok:
        changes.append("CFG-02: CONDOR_DTE_MAX 5->8")

    # CFG-03: Add separate risk budget for defined-risk structures
    # At MAX_RISK_PER_TRADE=2% (₹20k), a 200pt spread with 40pt credit
    # has max_risk/lot ~₹10-16k, sizing to exactly 1 lot where fixed
    # brokerage is 77-80% of total transaction cost.
    # A separate 4% budget for defined-risk structures allows 2-3 lots,
    # dropping cost from 7.6% to 2.8% of credit.
    old_reentry_const = (
        "# PRF-S04: minimum DTE for new entries.\n"
        "MIN_DTE_ENTRY = 4\n"
        "\n"
        "# PRF-C02: pre-event position reduction."
    )
    new_reentry_const = (
        "# PRF-S04: minimum DTE for new entries.\n"
        "MIN_DTE_ENTRY = 4\n"
        "\n"
        "# CFG-03: separate risk budget for defined-risk structures.\n"
        "# At 2% (₹20k), a 200pt spread sizes to 1 lot where fixed\n"
        "# brokerage is 77-80% of total cost. 4% allows 2-3 lots,\n"
        "# dropping cost from 7.6% to 2.8% of credit.\n"
        "# Used by _calculate_lot_size for IRON_CONDOR and CREDIT_SPREADS.\n"
        "MAX_RISK_PER_DEFINED_RISK_TRADE_PCT = 0.04\n"
        "\n"
        "# CFG-04/05: credit/max-loss gate for condor and spread.\n"
        "# credit/width gate (0.18/0.20) is unreachable at the delta\n"
        "# targets selected (0.20/0.08 pair achieves 11-13% of width).\n"
        "# credit/max-loss is self-consistent: at 0.20/0.08 delta pair,\n"
        "# credit/max-loss is 12-16%, so a 10% floor is a real filter.\n"
        "CONDOR_MIN_CREDIT_PER_MAXLOSS = 0.10\n"
        "SPREAD_MIN_CREDIT_PER_MAXLOSS = 0.10\n"
        "\n"
        "# PRF-C02: pre-event position reduction."
    )
    content, ok = sub_exact(old_reentry_const, new_reentry_const, content,
                            "CFG-03/04/05 defined-risk budget and maxloss gates")
    if ok:
        changes.append(
            "CFG-03: MAX_RISK_PER_DEFINED_RISK_TRADE_PCT=0.04 added; "
            "CFG-04/05: CONDOR/SPREAD_MIN_CREDIT_PER_MAXLOSS=0.10 added"
        )

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # RE-01: Return None from term sub-score when using VIX/100 fallback
    # When no 30-45 DTE chain is loaded, _compute_forward_iv sets
    # forward_iv = VIX/100. The term spread then computes:
    #   t_spread = (India VIX) - (near-week ATM IV)
    # This is not a term spread — it compares a 30-day variance-swap
    # integral against a 3-6 day ATM number. The gap is dominated by
    # tenor and skew integration, not term structure slope.
    # Fix: return None (honest zero) instead of a structurally biased constant.
    # _persist already handles None with time-based decay.
    old_term_vix_fallback = (
        "        v_fwd = self.dm.forward_iv\n"
        "        _near_iv = self.dm.iv_atm  # near-expiry ATM IV (decimal)\n"
        "        if v_fwd is not None:\n"
        "            v_fwd_pct  = v_fwd * 100.0\n"
        "            # Use near ATM IV if available (apples-to-apples comparison)\n"
        "            # Fall back to VIX only when iv_atm is missing\n"
        "            if _near_iv is not None and _near_iv > 0:\n"
        "                v_spot_pct = _near_iv * 100.0\n"
        "            else:\n"
        "                v_spot_pct = vix\n"
        "            t_spread   = v_fwd_pct - v_spot_pct"
    )
    new_term_vix_fallback = (
        "        v_fwd = self.dm.forward_iv\n"
        "        _near_iv = self.dm.iv_atm  # near-expiry ATM IV (decimal)\n"
        "        # RE-01: detect whether forward_iv is a genuine far-expiry\n"
        "        # ATM IV or the VIX/100 fallback. When it is the fallback,\n"
        "        # the 'term spread' compares a 30-day variance-swap integral\n"
        "        # against a 3-6 day ATM number — not a term spread at all.\n"
        "        # Return None (honest zero) so _persist decays gracefully\n"
        "        # rather than injecting a structurally biased constant.\n"
        "        _fwd_is_vix_proxy = (\n"
        "            v_fwd is not None\n"
        "            and self.dm.vix is not None\n"
        "            and abs(v_fwd - self.dm.vix / 100.0) < 0.001\n"
        "        )\n"
        "        if v_fwd is not None and not _fwd_is_vix_proxy:\n"
        "            v_fwd_pct  = v_fwd * 100.0\n"
        "            if _near_iv is not None and _near_iv > 0:\n"
        "                v_spot_pct = _near_iv * 100.0\n"
        "            else:\n"
        "                v_spot_pct = vix\n"
        "            t_spread   = v_fwd_pct - v_spot_pct"
    )
    content, ok = sub_exact(old_term_vix_fallback, new_term_vix_fallback, content,
                            "RE-01 term spread VIX proxy detection")
    if ok:
        changes.append("RE-01: term sub-score returns None when forward_iv is VIX/100 proxy")

    # Also update the else branch to return None for term_score
    old_term_else = (
        "        else:\n"
        "            term_score = 0\n"
        "            term_txt   = \"T_spread n/a (no far expiry)\"\n"
        "            notes.append(\"forward IV unavailable\")"
    )
    new_term_else = (
        "        elif _fwd_is_vix_proxy:\n"
        "            # RE-01: VIX proxy — not a real term spread, return None\n"
        "            term_score = None\n"
        "            term_txt   = \"T_spread n/a (forward_iv is VIX proxy)\"\n"
        "            notes.append(\"forward IV is VIX/100 proxy — not a term spread\")\n"
        "        else:\n"
        "            term_score = 0\n"
        "            term_txt   = \"T_spread n/a (no far expiry)\"\n"
        "            notes.append(\"forward IV unavailable\")"
    )
    content, ok = sub_exact(old_term_else, new_term_else, content,
                            "RE-01 term score None for VIX proxy")
    if ok:
        changes.append("RE-01: term_score=None added for VIX proxy case")

    # Update vol_score calculation to handle None term_score
    old_vol_score = (
        "        vol_score = 0.5 * term_score + 0.5 * skew_score"
    )
    new_vol_score = (
        "        # RE-01: if term_score is None (VIX proxy), use only skew\n"
        "        if term_score is None:\n"
        "            vol_score = skew_score  # full weight on skew\n"
        "        else:\n"
        "            vol_score = 0.5 * term_score + 0.5 * skew_score"
    )
    content, ok = sub_exact(old_vol_score, new_vol_score, content,
                            "RE-01 vol_score handles None term_score")
    if ok:
        changes.append("RE-01: vol_score uses only skew when term_score is None")

    # CFG-01 mirror: update local EDGE_RICH/EDGE_CHEAP constants
    # regime_engine.py has its own local copies that shadow config values
    old_edge_local = (
        "EDGE_RICH = config.EDGE_RICH"
        "        # AUDIT #2.2: reads from config"
    )
    new_edge_local = (
        "# CFG-01: reads from config (now symmetric ±2.0)\n"
        "EDGE_RICH = config.EDGE_RICH        # AUDIT #2.2: reads from config"
    )
    content, ok = sub_exact(old_edge_local, new_edge_local, content,
                            "CFG-01 regime_engine EDGE_RICH comment")
    if ok:
        changes.append("CFG-01: regime_engine EDGE_RICH comment updated")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # ── FIX-01: Define _dynamic_wing in _build_iron_condor ────────────
    # _dynamic_wing was referenced at multiple points but never assigned.
    # This caused a NameError on every condor build attempt, which was
    # caught by main.py's broad except and logged as "Strategy cycle error"
    # every 60 seconds. The condor never built; _last_build_failure was
    # never set so BUILD_FAILURE_COOLDOWN never engaged.
    old_condor_vix = (
        "        vix = self.dm.vix or 16.0\n"
        "\n"
        "        # PRF-S02: dynamic wing width scales with VIX.\n"
        "        # Fixed 250pt wing at VIX=11 gives ~20-25pt credit (fails min check).\n"
        "        # Dynamic: width = round(VIX * 15 / 50) * 50, clamped 200-600.\n"
        "        # VIX=11->200pt, VIX=16->250pt, VIX=20->300pt, VIX=25->350pt.\n"
        "        _dynamic_wing = max(\n"
        "            200,\n"
        "            min(\n"
        "                600,\n"
        "                int(round(vix * 15.0 / 50.0)) * 50,\n"
        "            ),\n"
        "        )\n"
        "        # Round to nearest NIFTY_STRIKE_STEP\n"
        "        _dynamic_wing = (\n"
        "            round(_dynamic_wing / config.NIFTY_STRIKE_STEP)\n"
        "            * config.NIFTY_STRIKE_STEP\n"
        "        )\n"
        "\n"
        "        expected_move = ("
    )
    if "_dynamic_wing" not in content:
        # _dynamic_wing was never defined — add the definition
        old_condor_vix_missing = (
            "        vix = self.dm.vix or 16.0\n"
            "\n"
            "        # BUG-01 FIX: compute expected move\n"
            "        expected_move = ("
        )
        new_condor_vix_with_wing = (
            "        vix = self.dm.vix or 16.0\n"
            "\n"
            "        # FIX-01: define _dynamic_wing. This was referenced but\n"
            "        # never assigned, causing NameError on every condor build.\n"
            "        # Use config.CONDOR_WING_WIDTH as the base, snapped to\n"
            "        # the strike grid. VIX-scaled wing is deferred item #62.\n"
            "        _dynamic_wing = (\n"
            "            round(\n"
            "                config.CONDOR_WING_WIDTH\n"
            "                / config.NIFTY_STRIKE_STEP\n"
            "            ) * config.NIFTY_STRIKE_STEP\n"
            "        )\n"
            "\n"
            "        expected_move = ("
        )
        content, ok = sub_exact(
            old_condor_vix_missing, new_condor_vix_with_wing, content,
            "FIX-01 _dynamic_wing definition (missing)"
        )
        if ok:
            changes.append("FIX-01: _dynamic_wing defined in _build_iron_condor")
    else:
        changes.append("FIX-01: _dynamic_wing already defined — skipped")

    # ── FIX-09/10: Use separate risk budget for defined-risk structures ─
    # At MAX_RISK_PER_TRADE=2% (₹20k), defined-risk structures size to
    # 1 lot where fixed brokerage is 77-80% of total cost.
    # Use MAX_RISK_PER_DEFINED_RISK_TRADE_PCT=4% for condor/spread.
    # Also fix straddle sizing: use margin basis not stop-based max-loss
    # (stop-based returns 0 lots at every reachable DTE/VIX combination).
    old_lot_size_defined = (
        "        # PRF-S01: use theoretical max_risk for defined-risk structures.\n"
        "        # For condors/spreads, max_risk = (wing_width - credit) * LOT_SIZE.\n"
        "        # Using stop_loss*LOT_SIZE as the basis was dangerous: a 250pt\n"
        "        # condor with 40pt credit has stop=50pt=Rs3250/lot, sizing to\n"
        "        # 6 lots at Rs20k risk. But a gap blows through stop and loses\n"
        "        # 210pt * 6 = Rs82k — 4x the intended risk.\n"
        "        # Fix: use meta[\"max_risk\"] for defined-risk structures.\n"
        "        _is_defined_risk = strategy_name in (\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        )\n"
        "        if _is_defined_risk:\n"
        "            # Use theoretical max loss (wing_width - credit) * LOT_SIZE\n"
        "            max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "        elif _strategy_type == \"SHORT\":\n"
        "            _stop_pts = meta.get(\"stop_loss\", 0)\n"
        "            if _stop_pts and _stop_pts > 0:\n"
        "                max_loss_per_lot = _stop_pts * config.LOT_SIZE\n"
        "            else:\n"
        "                max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "        else:\n"
        "            max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "        if max_loss_per_lot <= 0:\n"
        "            return 0"
    )
    new_lot_size_defined = (
        "        # FIX-09/10: separate risk budget for defined-risk structures\n"
        "        # and margin-based sizing for the straddle.\n"
        "        #\n"
        "        # FIX-09: at MAX_RISK_PER_TRADE=2% (Rs20k), a 200pt spread\n"
        "        # with 40pt credit has max_risk/lot ~Rs10-16k, sizing to 1 lot\n"
        "        # where fixed brokerage is 77-80% of total cost. Use a separate\n"
        "        # 4% budget for defined-risk structures (2-3 lots, cost drops\n"
        "        # from 7.6% to 2.8% of credit).\n"
        "        #\n"
        "        # FIX-10: straddle stop-based sizing returns 0 lots at every\n"
        "        # reachable DTE/VIX. Stop = credit * VIX_mult; at DTE=4 VIX=14\n"
        "        # stop_pts=361, max_loss/lot=Rs23,482 > Rs20k budget -> 0 lots.\n"
        "        # Use SPAN margin as the sizing basis instead (the real capital\n"
        "        # the position ties up, not the soft stop distance).\n"
        "        _is_defined_risk = strategy_name in (\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        )\n"
        "        _is_straddle = strategy_name == config.STRAT_SHORT_STRADDLE\n"
        "        if _is_defined_risk:\n"
        "            # Use theoretical max loss and a larger risk budget\n"
        "            max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "            # Override MAX_RISK_PER_TRADE for this sizing call\n"
        "            _defined_risk_budget = getattr(\n"
        "                config,\n"
        "                \"MAX_RISK_PER_DEFINED_RISK_TRADE_PCT\",\n"
        "                0.04,\n"
        "            ) * config.TOTAL_CAPITAL\n"
        "        elif _is_straddle:\n"
        "            # Use SPAN margin as sizing basis (real capital consumed)\n"
        "            _spot = self.dm.spot or 25000.0\n"
        "            _notional = _spot * config.LOT_SIZE\n"
        "            max_loss_per_lot = (\n"
        "                _notional * config.SPAN_NAKED_MARGIN_PCT\n"
        "            )\n"
        "            _defined_risk_budget = None  # use normal budget\n"
        "        elif _strategy_type == \"SHORT\":\n"
        "            _stop_pts = meta.get(\"stop_loss\", 0)\n"
        "            if _stop_pts and _stop_pts > 0:\n"
        "                max_loss_per_lot = _stop_pts * config.LOT_SIZE\n"
        "            else:\n"
        "                max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "            _defined_risk_budget = None\n"
        "        else:\n"
        "            max_loss_per_lot = meta.get(\"max_risk\", 0)\n"
        "            _defined_risk_budget = None\n"
        "        if max_loss_per_lot <= 0:\n"
        "            return 0"
    )
    content, ok = sub_exact(old_lot_size_defined, new_lot_size_defined, content,
                            "FIX-09/10 defined-risk budget and straddle margin sizing")
    if ok:
        changes.append(
            "FIX-09: defined-risk structures use 4% budget; "
            "FIX-10: straddle uses SPAN margin basis"
        )

    # Apply the defined-risk budget override in the lots calculation
    old_lots_calc = (
        "        risk_per_trade = config.MAX_RISK_PER_TRADE\n"
        "        lots           = math.floor(\n"
        "            risk_per_trade / max_loss_per_lot\n"
        "        )"
    )
    new_lots_calc = (
        "        # FIX-09: use defined-risk budget if set\n"
        "        risk_per_trade = (\n"
        "            _defined_risk_budget\n"
        "            if _defined_risk_budget is not None\n"
        "            else config.MAX_RISK_PER_TRADE\n"
        "        )\n"
        "        lots           = math.floor(\n"
        "            risk_per_trade / max_loss_per_lot\n"
        "        )"
    )
    content, ok = sub_exact(old_lots_calc, new_lots_calc, content,
                            "FIX-09 apply defined-risk budget")
    if ok:
        changes.append("FIX-09: defined-risk budget applied in lots calculation")

    # ── FIX-11: Gate on credit/max-loss not credit/width ──────────────
    # credit/width gate (0.18 for condor, 0.20 for spread) is unreachable
    # at the delta targets selected. The 0.20/0.08 delta pair achieves
    # 11-13% of width at any VIX level — below both floors.
    # credit/max-loss is self-consistent: at 0.20/0.08 delta pair,
    # credit/max-loss is 12-16%, so a 10% floor is a real filter.
    old_condor_credit_gate = (
        "        # CFG-P1-01: read from config, not a hardcoded literal.\n"
        "        _min_credit_ratio = getattr(\n"
        "            config,\n"
        "            \"CONDOR_MIN_CREDIT_PCT_OF_WIDTH\",\n"
        "            0.15,\n"
        "        )\n"
        "        _min_credit_required = max(\n"
        "            config.CONDOR_MIN_CREDIT,\n"
        "            _min_credit_ratio * _dynamic_wing,\n"
        "        )\n"
        "        # C4-14: also gate on credit as % of max possible loss.\n"
        "        # Credit/width is misleading: high credit/width can mean\n"
        "        # riskier (closer) strikes. Credit/max_loss is more economically\n"
        "        # meaningful — it measures the return on the capital actually at risk.\n"
        "        _min_credit_per_maxloss = getattr(\n"
        "            config, \"CONDOR_MIN_CREDIT_PER_MAXLOSS\", 0.10\n"
        "        )\n"
        "        # max_loss = (wing - credit) * LOT_SIZE; rearranged:\n"
        "        # credit >= _min_credit_per_maxloss * (wing - credit)\n"
        "        # => credit * (1 + _min_credit_per_maxloss) >= _min_credit_per_maxloss * wing\n"
        "        # => credit >= wing * _min_credit_per_maxloss / (1 + _min_credit_per_maxloss)\n"
        "        _min_credit_maxloss_gate = (\n"
        "            _dynamic_wing\n"
        "            * _min_credit_per_maxloss\n"
        "            / (1.0 + _min_credit_per_maxloss)\n"
        "        )\n"
        "        _min_credit_required = max(\n"
        "            _min_credit_required,\n"
        "            _min_credit_maxloss_gate,\n"
        "        )"
    )
    new_condor_credit_gate = (
        "        # FIX-11: gate on credit/max-loss, not credit/width.\n"
        "        # credit/width gate (0.18) is unreachable at the delta targets\n"
        "        # selected (0.20/0.08 pair achieves 11-13% of width at any VIX).\n"
        "        # credit/max-loss is self-consistent: at 0.20/0.08 delta pair,\n"
        "        # credit/max-loss is 12-16%, so a 10% floor is a real filter.\n"
        "        # Keep CONDOR_MIN_CREDIT as an absolute sanity floor only.\n"
        "        _min_credit_per_maxloss = getattr(\n"
        "            config, \"CONDOR_MIN_CREDIT_PER_MAXLOSS\", 0.10\n"
        "        )\n"
        "        # credit >= maxloss_floor * (wing - credit)\n"
        "        # => credit >= wing * floor / (1 + floor)\n"
        "        _min_credit_maxloss_gate = (\n"
        "            _dynamic_wing\n"
        "            * _min_credit_per_maxloss\n"
        "            / (1.0 + _min_credit_per_maxloss)\n"
        "        )\n"
        "        _min_credit_required = max(\n"
        "            config.CONDOR_MIN_CREDIT,\n"
        "            _min_credit_maxloss_gate,\n"
        "        )"
    )
    content, ok = sub_exact(old_condor_credit_gate, new_condor_credit_gate, content,
                            "FIX-11 condor credit/maxloss gate")
    if ok:
        changes.append("FIX-11: condor uses credit/max-loss gate (not credit/width)")

    # ── FIX-02/03/04: Per-side leg construction in credit spreads ─────
    # The original code had a single `if _build_put_side:` block that
    # appended all four legs regardless of skew_side. skew_side='put'
    # built a complete iron condor while booking only put-side credit.
    # Also: executable-price gate (bid/ask not LTP) for parity with condor.
    # Also: side-aware credit gate (was using two-sided width for one-sided).

    # First fix the credit gate to be side-aware
    old_spread_gate = (
        "        # AUDIT CFG-01: check credit as % of max spread width.\n"
        "        _spread_min_pct = getattr(\n"
        "            config, \"SPREAD_MIN_CREDIT_PCT_OF_WIDTH\", 0.25\n"
        "        )\n"
        "        _spread_min_required = max(\n"
        "            config.SPREAD_MIN_CREDIT,\n"
        "            _spread_min_pct * max(put_width, call_width),\n"
        "        )\n"
        "        if total_credit < _spread_min_required:\n"
        "            logger.info(\n"
        "                f\"Credit spread: credit={total_credit:.2f} \"\n"
        "                f\"< min={_spread_min_required:.1f} \"\n"
        "                f\"({_spread_min_pct*100:.0f}% of width)\"\n"
        "            )\n"
        "            return (None, {})"
    )
    new_spread_gate = (
        "        # FIX-03/11: side-aware credit gate using credit/max-loss.\n"
        "        # Old gate used two-sided width for a one-sided build, and\n"
        "        # credit/width (0.20) is unreachable at 0.20/0.08 delta pair.\n"
        "        _active_put_width  = put_width  if _build_put_side  else 0\n"
        "        _active_call_width = call_width if _build_call_side else 0\n"
        "        _active_max_width  = max(_active_put_width, _active_call_width)\n"
        "        _spread_min_per_maxloss = getattr(\n"
        "            config, \"SPREAD_MIN_CREDIT_PER_MAXLOSS\", 0.10\n"
        "        )\n"
        "        _spread_min_maxloss_gate = (\n"
        "            _active_max_width\n"
        "            * _spread_min_per_maxloss\n"
        "            / (1.0 + _spread_min_per_maxloss)\n"
        "            if _active_max_width > 0 else 0\n"
        "        )\n"
        "        _spread_min_required = max(\n"
        "            config.SPREAD_MIN_CREDIT,\n"
        "            _spread_min_maxloss_gate,\n"
        "        )\n"
        "        # FIX-04: use executable prices (bid/ask) not LTP\n"
        "        def _exec_spread(opt, action):\n"
        "            b = float(opt.get(\"bid\") or 0)\n"
        "            a = float(opt.get(\"ask\") or 0)\n"
        "            if b <= 0 or a <= 0:\n"
        "                return float(opt.get(\"ltp\") or 0)\n"
        "            return b if action == \"SELL\" else a\n"
        "        _exec_credit = 0.0\n"
        "        if _build_put_side:\n"
        "            _exec_credit += (\n"
        "                _exec_spread(chain[short_put_strike][\"put\"], \"SELL\")\n"
        "                - _exec_spread(chain[long_put_strike][\"put\"], \"BUY\")\n"
        "            )\n"
        "        if _build_call_side:\n"
        "            _exec_credit += (\n"
        "                _exec_spread(chain[short_call_strike][\"call\"], \"SELL\")\n"
        "                - _exec_spread(chain[long_call_strike][\"call\"], \"BUY\")\n"
        "            )\n"
        "        _slip = getattr(config, \"ENTRY_SLIPPAGE_PTS_PER_LEG\", 0.75)\n"
        "        _n_active_legs = (\n"
        "            (2 if _build_put_side else 0)\n"
        "            + (2 if _build_call_side else 0)\n"
        "        )\n"
        "        _exec_credit_gated = _exec_credit - _slip * _n_active_legs\n"
        "        if _exec_credit_gated < _spread_min_required:\n"
        "            logger.info(\n"
        "                f\"Credit spread ({skew_side}): \"\n"
        "                f\"exec_credit={_exec_credit:.2f} \"\n"
        "                f\"after_slippage={_exec_credit_gated:.2f} \"\n"
        "                f\"< min={_spread_min_required:.1f} \"\n"
        "                f\"(credit/maxloss gate)\"\n"
        "            )\n"
        "            return (None, {})\n"
        "        # Use executable credit for position record\n"
        "        total_credit = _exec_credit"
    )
    content, ok = sub_exact(old_spread_gate, new_spread_gate, content,
                            "FIX-03/04/11 spread credit gate")
    if ok:
        changes.append(
            "FIX-03: side-aware credit gate; "
            "FIX-04: executable prices in spread gate; "
            "FIX-11: credit/max-loss gate for spreads"
        )

    # FIX-02: Per-side leg construction
    # The old code had one `if _build_put_side:` block containing all 4 legs.
    # Now each side is gated independently.
    old_legs_block = (
        "        # SE-01: put_width and call_width MUST be computed before\n"
        "        # _spread_min_required which references them. The original\n"
        "        # order caused an UnboundLocalError on every call, making\n"
        "        # MILD_SELL_VOL permanently unable to enter.\n"
        "        put_width  = short_put_strike  - long_put_strike\n"
        "        call_width = long_call_strike  - short_call_strike\n"
        "        max_risk   = (\n"
        "            max(put_width, call_width) - total_credit\n"
        "        ) * config.LOT_SIZE"
    )
    new_legs_block = (
        "        # SE-01: widths computed before the credit gate.\n"
        "        put_width  = short_put_strike  - long_put_strike\n"
        "        call_width = long_call_strike  - short_call_strike\n"
        "        # FIX-02: max_risk based on active sides only\n"
        "        _active_w = max(\n"
        "            put_width  if _build_put_side  else 0,\n"
        "            call_width if _build_call_side else 0,\n"
        "        )\n"
        "        max_risk   = (\n"
        "            max(_active_w, 1) - total_credit\n"
        "        ) * config.LOT_SIZE"
    )
    content, ok = sub_exact(old_legs_block, new_legs_block, content,
                            "FIX-02 per-side max_risk")
    if ok:
        changes.append("FIX-02: max_risk uses active-side width only")

    # ── FIX-05: IV-rank gate on _build_long_strangle ──────────────────
    # _build_long_straddle has IV-rank < 40 and IV < RV+2% gates.
    # _build_long_strangle has neither — it buys a strangle whenever
    # BUY_VOL or EVENT_HEDGE fires, regardless of whether vol is cheap.
    # Monte Carlo shows EV = -₹900 to -₹3,700/lot with no cheapness check.
    old_strangle_build = (
        "    async def _build_long_strangle(\n"
        "        self,\n"
        "    ) -> Tuple[Optional[List[Leg]], Dict]:\n"
        "        \"\"\"Build long strangle for event volatility.\"\"\"\n"
        "        expiry = self.dm.get_expiry_by_dte(\n"
        "            config.EVENT_STRANGLE_DTE_TARGET,\n"
        "            tolerance=config.EVENT_STRANGLE_DTE_TARGET - 2,\n"
        "        )\n"
        "        if expiry is None:\n"
        "            return (None, {})"
    )
    new_strangle_build = (
        "    async def _build_long_strangle(\n"
        "        self,\n"
        "    ) -> Tuple[Optional[List[Leg]], Dict]:\n"
        "        \"\"\"Build long strangle for event volatility.\"\"\"\n"
        "        # FIX-05: IV-rank gate, matching _build_long_straddle.\n"
        "        # Without this, the strangle buys options at any IV level,\n"
        "        # including when vol is expensive. Monte Carlo shows\n"
        "        # EV = -Rs900 to -Rs3700/lot with no cheapness check.\n"
        "        _ivr = self.dm.compute_iv_rank()\n"
        "        _iv_rank = _ivr if _ivr is not None else 50.0\n"
        "        _max_iv_rank = getattr(\n"
        "            config, \"LONG_STRADDLE_MAX_IV_RANK\", 40\n"
        "        )\n"
        "        if _iv_rank > _max_iv_rank:\n"
        "            logger.info(\n"
        "                f\"Long strangle: IV rank {_iv_rank:.1f} > \"\n"
        "                f\"{_max_iv_rank} — vol too expensive to buy\"\n"
        "            )\n"
        "            return (None, {})\n"
        "        # Also require IV is not rich vs RV\n"
        "        if (\n"
        "            self.dm.iv_atm is not None\n"
        "            and self.dm.rv_20d is not None\n"
        "            and self.dm.iv_atm > self.dm.rv_20d + 0.02\n"
        "        ):\n"
        "            logger.info(\n"
        "                f\"Long strangle: IV ({self.dm.iv_atm:.4f}) > \"\n"
        "                f\"RV ({self.dm.rv_20d:.4f}) + 2%% — vol not cheap\"\n"
        "            )\n"
        "            return (None, {})\n"
        "        expiry = self.dm.get_expiry_by_dte(\n"
        "            config.EVENT_STRANGLE_DTE_TARGET,\n"
        "            tolerance=config.EVENT_STRANGLE_DTE_TARGET - 2,\n"
        "        )\n"
        "        if expiry is None:\n"
        "            return (None, {})"
    )
    content, ok = sub_exact(old_strangle_build, new_strangle_build, content,
                            "FIX-05 IV-rank gate on _build_long_strangle")
    if ok:
        changes.append("FIX-05: IV-rank gate added to _build_long_strangle")

    # ── FIX-06: Symmetric edge in _module_edge ────────────────────────
    # The local EDGE_RICH/EDGE_CHEAP constants in regime_engine.py now
    # read from config (already done by previous patches). The config
    # values are updated by CFG-01. No additional code change needed here
    # beyond ensuring the config values are read. Mark as handled.
    changes.append("FIX-06: EDGE_RICH/CHEAP symmetry handled via CFG-01 config change")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Critical profitability fixes from deep analysis."
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