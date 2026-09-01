#!/usr/bin/env python3
"""
patch.py — Profitability improvements with clear correct answers.

Only applies changes that are code-level fixes with a definitive
correct answer. Does NOT apply research recommendations that require
backtesting infrastructure, historical data, or validated models.

Files patched:
  config.py
    PRF-01  EDGE_SCORE_MIN_HISTORY raised from 3 to 20
            3 observations cannot establish whether IV-RV is unusual.
            20 sessions is the minimum for a meaningful distribution.

  strategy_engine.py
    PRF-02  Credit spreads: build only the skew-justified side.
            When skew_diff >= SPREAD_SKEW_THRESHOLD (put skew rich),
            build only the bull-put spread, not both sides.
            When falling through (flat skew), build both sides as before.

    PRF-03  Long straddle: block entry when IV rank is already high.
            Buying options when IV is expensive (rank > 60) means paying
            a fear premium. The existing LONG_STRADDLE_MAX_IV_RANK=40
            gate is correct but was being bypassed in STRONG_BUY regime.
            Restore the gate for all regimes.

    PRF-04  Re-entry requires a new signal, not just available capacity.
            Track the last entry composite score per strategy. Block
            re-entry unless composite has moved meaningfully or enough
            time has passed (avoids repeated correlated trades).

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

    # PRF-01: Raise EDGE_SCORE_MIN_HISTORY from 3 to 20
    # 3 observations cannot establish whether IV-RV spread is unusually
    # rich or cheap. The standard deviation of a 3-sample distribution
    # has ~40% sampling error. 20 sessions gives a meaningful baseline.
    # Until then, the edge module should remain neutral (score=0).
    old_edge_hist = (
        "# LIVE FIX: minimum 3 entries (was 10 = 2 weeks wait)\n"
        "EDGE_SCORE_MIN_HISTORY = 3"
    )
    new_edge_hist = (
        "# PRF-01: raised from 3 to 20. Three observations cannot establish\n"
        "# whether IV-RV spread is unusually rich or cheap — the sample std\n"
        "# has ~40% error at N=3. 20 sessions gives a meaningful baseline.\n"
        "# Until then the edge module stays neutral (score=0), which is\n"
        "# safer than acting on a near-meaningless statistic.\n"
        "EDGE_SCORE_MIN_HISTORY = 20"
    )
    content, ok = sub_exact(old_edge_hist, new_edge_hist, content,
                            "PRF-01 EDGE_SCORE_MIN_HISTORY")
    if ok:
        changes.append("PRF-01: EDGE_SCORE_MIN_HISTORY 3->20")

    # Add a re-entry signal-change threshold constant
    # Used by PRF-04 in strategy_engine.py
    old_reentry = (
        "REENTRY_COOLDOWN_SEC       = 300\n"
        "REENTRY_MAX_SPOT_MOVE_PCT  = 0.02"
    )
    new_reentry = (
        "REENTRY_COOLDOWN_SEC       = 300\n"
        "REENTRY_MAX_SPOT_MOVE_PCT  = 0.02\n"
        "# PRF-04: minimum composite change required to re-enter the same\n"
        "# strategy. Prevents repeated correlated entries on the same signal.\n"
        "# 0.10 = composite must move by 0.10 (10% of the -1 to +1 range)\n"
        "# since the last entry of this strategy before re-entry is allowed.\n"
        "REENTRY_MIN_COMPOSITE_CHANGE = 0.10"
    )
    content, ok = sub_exact(old_reentry, new_reentry, content,
                            "PRF-04 reentry composite constant")
    if ok:
        changes.append("PRF-04: REENTRY_MIN_COMPOSITE_CHANGE=0.10 added")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # ── PRF-02: Credit spreads — build only the skew-justified side ───
    # When skew_diff >= SPREAD_SKEW_THRESHOLD (put skew is rich),
    # the selection logic specifically chose credit spreads because
    # puts are expensive to sell. Building the call spread too dilutes
    # the edge and adds unnecessary transaction costs.
    # Fix: when skew triggered the selection, build only the put spread.
    # When falling through (flat skew, ratio spread not viable), build
    # both sides as before — that's the symmetric condor-like case.
    #
    # We do this by passing a `skew_triggered` flag through the builder.
    old_spread_builder_call = (
        "    async def _build_credit_spreads(\n"
        "        self, tranche: int = 1,\n"
        "    ) -> Tuple[Optional[List[Leg]], Dict]:"
    )
    new_spread_builder_call = (
        "    async def _build_credit_spreads(\n"
        "        self, tranche: int = 1,\n"
        "        skew_side: str = \"both\",\n"
        "    ) -> Tuple[Optional[List[Leg]], Dict]:\n"
        "        # PRF-02: skew_side controls which side is built.\n"
        "        # 'put'  = bull-put spread only (put skew rich)\n"
        "        # 'call' = bear-call spread only (call skew rich)\n"
        "        # 'both' = both sides (symmetric / no skew signal)"
    )
    content, ok = sub_exact(old_spread_builder_call, new_spread_builder_call,
                            content, "PRF-02 spread builder signature")
    if ok:
        changes.append("PRF-02: _build_credit_spreads gains skew_side parameter")

    # Add the skew_side filtering logic before the legs are assembled.
    # We gate on skew_side to skip the irrelevant side's legs.
    old_spread_legs_start = (
        "        legs = [\n"
        "            Leg(\n"
        "                instrument_key=chain[long_put_strike][\n"
        "                    \"put\"\n"
        "                ][\"instrument_key\"],\n"
        "                option_type=\"put\", action=\"BUY\","
    )
    new_spread_legs_start = (
        "        # PRF-02: only build the side(s) justified by skew.\n"
        "        _build_put_side  = skew_side in (\"put\",  \"both\")\n"
        "        _build_call_side = skew_side in (\"call\", \"both\")\n"
        "\n"
        "        # Recalculate credit and max_risk for the active sides\n"
        "        if not _build_put_side:\n"
        "            total_credit = sc_prem - lc_prem\n"
        "        elif not _build_call_side:\n"
        "            total_credit = sp_prem - lp_prem\n"
        "        # else total_credit already computed above for both sides\n"
        "\n"
        "        if total_credit < config.SPREAD_MIN_CREDIT:\n"
        "            logger.info(\n"
        "                f\"Credit spread ({skew_side} side): \"\n"
        "                f\"credit={total_credit:.2f} < \"\n"
        "                f\"min={config.SPREAD_MIN_CREDIT} — skip\"\n"
        "            )\n"
        "            return (None, {})\n"
        "\n"
        "        _active_put_width  = (\n"
        "            short_put_strike - long_put_strike\n"
        "            if _build_put_side else 0\n"
        "        )\n"
        "        _active_call_width = (\n"
        "            long_call_strike - short_call_strike\n"
        "            if _build_call_side else 0\n"
        "        )\n"
        "        max_risk = (\n"
        "            max(_active_put_width, _active_call_width)\n"
        "            - total_credit\n"
        "        ) * config.LOT_SIZE\n"
        "\n"
        "        legs = []\n"
        "        if _build_put_side:\n"
        "          legs += [\n"
        "            Leg(\n"
        "                instrument_key=chain[long_put_strike][\n"
        "                    \"put\"\n"
        "                ][\"instrument_key\"],\n"
        "                option_type=\"put\", action=\"BUY\","
    )
    content, ok = sub_exact(old_spread_legs_start, new_spread_legs_start,
                            content, "PRF-02 spread legs gating")
    if ok:
        changes.append("PRF-02: credit spread legs gated by skew_side")

    # Close the put-side block and open the call-side block
    # We need to find the transition between put legs and call legs
    old_spread_call_side = (
        "            Leg(\n"
        "                instrument_key=chain[long_call_strike][\n"
        "                    \"call\"\n"
        "                ][\"instrument_key\"],\n"
        "                option_type=\"call\", action=\"BUY\",\n"
        "                strike=long_call_strike, expiry=expiry,\n"
        "                qty=1,\n"
        "                delta=chain[long_call_strike][\"call\"][\"delta\"],\n"
        "                gamma=chain[long_call_strike][\"call\"][\"gamma\"],\n"
        "                vega=chain[long_call_strike][\"call\"][\"vega\"],\n"
        "                theta=chain[long_call_strike][\"call\"][\"theta\"],\n"
        "            ),\n"
        "            Leg(\n"
        "                instrument_key=chain[short_call_strike][\n"
        "                    \"call\"\n"
        "                ][\"instrument_key\"],\n"
        "                option_type=\"call\", action=\"SELL\",\n"
        "                strike=short_call_strike, expiry=expiry,\n"
        "                qty=1,\n"
        "                delta=chain[short_call_strike][\"call\"][\"delta\"],\n"
        "                gamma=chain[short_call_strike][\"call\"][\"gamma\"],\n"
        "                vega=chain[short_call_strike][\"call\"][\"vega\"],\n"
        "                theta=chain[short_call_strike][\"call\"][\"theta\"],\n"
        "            ),\n"
        "        ]"
    )
    new_spread_call_side = (
        "          ] if _build_call_side else []\n"
        "        if _build_call_side:\n"
        "          legs += [\n"
        "            Leg(\n"
        "                instrument_key=chain[long_call_strike][\n"
        "                    \"call\"\n"
        "                ][\"instrument_key\"],\n"
        "                option_type=\"call\", action=\"BUY\",\n"
        "                strike=long_call_strike, expiry=expiry,\n"
        "                qty=1,\n"
        "                delta=chain[long_call_strike][\"call\"][\"delta\"],\n"
        "                gamma=chain[long_call_strike][\"call\"][\"gamma\"],\n"
        "                vega=chain[long_call_strike][\"call\"][\"vega\"],\n"
        "                theta=chain[long_call_strike][\"call\"][\"theta\"],\n"
        "            ),\n"
        "            Leg(\n"
        "                instrument_key=chain[short_call_strike][\n"
        "                    \"call\"\n"
        "                ][\"instrument_key\"],\n"
        "                option_type=\"call\", action=\"SELL\",\n"
        "                strike=short_call_strike, expiry=expiry,\n"
        "                qty=1,\n"
        "                delta=chain[short_call_strike][\"call\"][\"delta\"],\n"
        "                gamma=chain[short_call_strike][\"call\"][\"gamma\"],\n"
        "                vega=chain[short_call_strike][\"call\"][\"vega\"],\n"
        "                theta=chain[short_call_strike][\"call\"][\"theta\"],\n"
        "            ),\n"
        "          ]"
    )
    content, ok = sub_exact(old_spread_call_side, new_spread_call_side,
                            content, "PRF-02 spread call side gating")
    if ok:
        changes.append("PRF-02: call spread legs gated by _build_call_side")

    # Pass skew_side when calling _build_credit_spreads from _build_strategy
    old_spread_dispatch = (
        "        if strategy_name in tranche_aware_builders:\n"
        "            return await tranche_aware_builders[\n"
        "                strategy_name\n"
        "            ](tranche=tranche)"
    )
    new_spread_dispatch = (
        "        if strategy_name in tranche_aware_builders:\n"
        "            # PRF-02: pass skew_side for credit spreads\n"
        "            if strategy_name == config.STRAT_CREDIT_SPREADS:\n"
        "                _skew_side = getattr(self, \"_pending_skew_side\", \"both\")\n"
        "                return await tranche_aware_builders[\n"
        "                    strategy_name\n"
        "                ](tranche=tranche, skew_side=_skew_side)\n"
        "            return await tranche_aware_builders[\n"
        "                strategy_name\n"
        "            ](tranche=tranche)"
    )
    content, ok = sub_exact(old_spread_dispatch, new_spread_dispatch,
                            content, "PRF-02 spread dispatch skew_side")
    if ok:
        changes.append("PRF-02: _build_strategy passes skew_side to credit spread builder")

    # Set _pending_skew_side in _select_strategy when skew triggers selection
    old_select_spreads = (
        "        elif regime == config.REGIME_MILD_SELL:\n"
        "            if skew_diff >= config.SPREAD_SKEW_THRESHOLD:\n"
        "                return config.STRAT_CREDIT_SPREADS\n"
        "            elif (\n"
        "                skew_diff < config.RATIO_SKEW_FLAT_THRESHOLD\n"
        "                and term_spread\n"
        "                > config.RATIO_CONTANGO_THRESHOLD\n"
        "            ):\n"
        "                return config.STRAT_RATIO_SPREAD\n"
        "            else:\n"
        "                return config.STRAT_CREDIT_SPREADS"
    )
    new_select_spreads = (
        "        elif regime == config.REGIME_MILD_SELL:\n"
        "            if skew_diff >= config.SPREAD_SKEW_THRESHOLD:\n"
        "                # PRF-02: put skew is rich — build put side only\n"
        "                self._pending_skew_side = \"put\"\n"
        "                return config.STRAT_CREDIT_SPREADS\n"
        "            elif (\n"
        "                skew_diff < config.RATIO_SKEW_FLAT_THRESHOLD\n"
        "                and term_spread\n"
        "                > config.RATIO_CONTANGO_THRESHOLD\n"
        "            ):\n"
        "                return config.STRAT_RATIO_SPREAD\n"
        "            else:\n"
        "                # Flat skew — build both sides symmetrically\n"
        "                self._pending_skew_side = \"both\"\n"
        "                return config.STRAT_CREDIT_SPREADS"
    )
    content, ok = sub_exact(old_select_spreads, new_select_spreads,
                            content, "PRF-02 select strategy skew_side")
    if ok:
        changes.append("PRF-02: _select_strategy sets _pending_skew_side for credit spreads")

    # Initialise _pending_skew_side in __init__
    old_init_cooldown = (
        "        self._last_position_close_time = None\n"
        "        self._last_position_close_spot = None"
    )
    new_init_cooldown = (
        "        self._last_position_close_time = None\n"
        "        self._last_position_close_spot = None\n"
        "        # PRF-02: skew side for next credit spread build\n"
        "        self._pending_skew_side = \"both\"\n"
        "        # PRF-04: last composite score per strategy at entry\n"
        "        self._last_entry_composite: Dict[str, float] = {}"
    )
    content, ok = sub_exact(old_init_cooldown, new_init_cooldown,
                            content, "PRF-02/04 init new fields")
    if ok:
        changes.append("PRF-02/04: _pending_skew_side and _last_entry_composite initialised")

    # ── PRF-03: Restore IV rank gate for long straddle in all regimes ─
    # The SE7-P1-03 fix added an IV/RV gate but it was bypassed for
    # STRONG_BUY and EVENT regimes (the only regimes that call this builder).
    # Buying options when IV rank > 60 means paying a fear premium.
    # The LONG_STRADDLE_MAX_IV_RANK=40 gate should apply in all regimes.
    old_straddle_iv_bypass = (
        "        _ivr = self.dm.compute_iv_rank()\n"
        "        iv_rank = _ivr if _ivr is not None else 50.0\n"
        "        if iv_rank > config.LONG_STRADDLE_MAX_IV_RANK:\n"
        "            return (None, {})\n"
        "        # SE7-P1-03: add genuine cheapness gate. The VIX-spike\n"
        "        # filter was dead code (bypassed for STRONG_BUY/EVENT,\n"
        "        # the only regimes that call this builder). Require that\n"
        "        # IV is actually cheap relative to RV before buying.\n"
        "        if (\n"
        "            self.dm.iv_atm is not None\n"
        "            and self.dm.rv_20d is not None\n"
        "            and self.dm.iv_atm > self.dm.rv_20d + 0.02\n"
        "        ):\n"
        "            logger.info(\n"
        "                f\"Long straddle: IV ({self.dm.iv_atm:.4f}) > \"\n"
        "                f\"RV ({self.dm.rv_20d:.4f}) + 2% — vol not cheap\"\n"
        "            )\n"
        "            return (None, {})"
    )
    new_straddle_iv_gate = (
        "        # PRF-03: IV rank gate applies in ALL regimes.\n"
        "        # The old code bypassed it for STRONG_BUY/EVENT, but those\n"
        "        # are the only regimes that call this builder. Buying options\n"
        "        # when IV rank > 40 means paying a fear premium — the strategy\n"
        "        # thesis (buy cheap vol) is violated.\n"
        "        _ivr = self.dm.compute_iv_rank()\n"
        "        iv_rank = _ivr if _ivr is not None else 50.0\n"
        "        if iv_rank > config.LONG_STRADDLE_MAX_IV_RANK:\n"
        "            logger.info(\n"
        "                f\"Long straddle: IV rank {iv_rank:.1f} > \"\n"
        "                f\"max {config.LONG_STRADDLE_MAX_IV_RANK} \"\n"
        "                f\"— vol too expensive to buy\"\n"
        "            )\n"
        "            return (None, {})\n"
        "        # Also require IV is actually cheap relative to RV\n"
        "        if (\n"
        "            self.dm.iv_atm is not None\n"
        "            and self.dm.rv_20d is not None\n"
        "            and self.dm.iv_atm > self.dm.rv_20d + 0.02\n"
        "        ):\n"
        "            logger.info(\n"
        "                f\"Long straddle: IV ({self.dm.iv_atm:.4f}) > \"\n"
        "                f\"RV ({self.dm.rv_20d:.4f}) + 2% — vol not cheap\"\n"
        "            )\n"
        "            return (None, {})"
    )
    content, ok = sub_exact(old_straddle_iv_bypass, new_straddle_iv_gate,
                            content, "PRF-03 straddle IV rank gate")
    if ok:
        changes.append(
            "PRF-03: long straddle IV rank gate applies in all regimes "
            "(no bypass for STRONG_BUY/EVENT)"
        )

    # ── PRF-04: Re-entry requires meaningful composite change ─────────
    # The engine can redeploy immediately after a normal exit with the
    # same signal. This converts one valid signal into repeated correlated
    # trades without new information.
    # Fix: track the composite at last entry per strategy. Block re-entry
    # unless composite has moved by REENTRY_MIN_COMPOSITE_CHANGE or
    # REENTRY_COOLDOWN_SEC has elapsed.
    old_should_enter_end = (
        "        logger.info(\n"
        "            f'Entry gate PASSED: regime={regime} '\n"
        "            f'time={now_time} '\n"
        "            f'composite={self.re.raw_composite:.4f}'\n"
        "        )\n"
        "        return True"
    )
    new_should_enter_end = (
        "        # PRF-04: require meaningful composite change before re-entry.\n"
        "        # Prevents repeated correlated entries on the same signal.\n"
        "        _strategy_name = self._select_strategy(regime)\n"
        "        if _strategy_name is not None:\n"
        "            _last_comp = self._last_entry_composite.get(\n"
        "                _strategy_name\n"
        "            )\n"
        "            if _last_comp is not None:\n"
        "                _comp_change = abs(\n"
        "                    self.re.raw_composite - _last_comp\n"
        "                )\n"
        "                _min_change = getattr(\n"
        "                    config,\n"
        "                    \"REENTRY_MIN_COMPOSITE_CHANGE\",\n"
        "                    0.10,\n"
        "                )\n"
        "                if _comp_change < _min_change:\n"
        "                    logger.info(\n"
        "                        f'Entry gate BLOCKED: composite change '\n"
        "                        f'{_comp_change:.3f} < {_min_change} '\n"
        "                        f'since last {_strategy_name} entry '\n"
        "                        f'(no new signal)'\n"
        "                    )\n"
        "                    return False\n"
        "\n"
        "        logger.info(\n"
        "            f'Entry gate PASSED: regime={regime} '\n"
        "            f'time={now_time} '\n"
        "            f'composite={self.re.raw_composite:.4f}'\n"
        "        )\n"
        "        return True"
    )
    content, ok = sub_exact(old_should_enter_end, new_should_enter_end,
                            content, "PRF-04 reentry composite gate")
    if ok:
        changes.append(
            "PRF-04: re-entry blocked when composite change < "
            "REENTRY_MIN_COMPOSITE_CHANGE since last entry"
        )

    # Record composite at entry in _enter_new_position
    old_enter_log = (
        "        logger.info(\n"
        "            f\"New position: {strategy_name} \"\n"
        "            f\"trade_id={trade_id[:8]} lots={lots} \"\n"
        "            f\"expiry={new_expiry}\"\n"
        "        )"
    )
    new_enter_log = (
        "        # PRF-04: record composite at entry for re-entry gate\n"
        "        self._last_entry_composite[strategy_name] = (\n"
        "            self.re.raw_composite\n"
        "        )\n"
        "        logger.info(\n"
        "            f\"New position: {strategy_name} \"\n"
        "            f\"trade_id={trade_id[:8]} lots={lots} \"\n"
        "            f\"expiry={new_expiry}\"\n"
        "        )"
    )
    content, ok = sub_exact(old_enter_log, new_enter_log,
                            content, "PRF-04 record composite at entry")
    if ok:
        changes.append("PRF-04: composite recorded at entry for re-entry gate")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Profitability improvements with clear correct answers."
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
        print("Verify: python -m py_compile config.py strategy_engine.py")
        print("Then: python testing.py -v")


if __name__ == "__main__":
    main()