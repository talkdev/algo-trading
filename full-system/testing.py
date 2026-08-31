#!/usr/bin/env python3
"""
patch_margin.py — Heuristic margin/SPAN approximation for
paper-mode sizing realism, verified against the current state of
config.py / strategy_engine.py (post diversification/rolling
patch).

IMPORTANT — this is NOT a real SPAN calculation. Real exchange
margin varies daily with volatility scans and is broker-specific.
This is a documented, conservative approximation so paper-mode
position sizes are closer to what would actually be achievable
live, instead of assuming margin is unlimited. Live mode is
unaffected in terms of authority — it still separately validates
against the REAL broker margin API via check_margin() in
_pre_trade_checks() (unchanged); this heuristic just makes the
upstream lot-sizing decision more realistic before that gate.

WHAT THIS ADDS
---------------
[config.py]
  - MARGIN_UTILIZATION_PCT = 0.80
  - SPAN_NAKED_MARGIN_PCT = 0.11
  - SPAN_SPREAD_MARGIN_MULTIPLIER = 1.15

[strategy_engine.py]
  - Position.margin_estimate field
  - _estimate_margin_requirement(): per-strategy heuristic
  - _calculate_lot_size(): additional margin-budget cap
  - _enter_new_position(): stores total margin_estimate in meta
  - _create_position_record(): persists margin_estimate on Position

Usage:
    python patch_margin.py --dry-run
    python patch_margin.py
"""

import ast
import os
import re
import sys
import shutil
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRY_RUN = "--dry-run" in sys.argv
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

FILES = {
    "config": os.path.join(BASE_DIR, "config.py"),
    "strategy_engine": os.path.join(BASE_DIR, "strategy_engine.py"),
}


class PatchError(Exception):
    pass


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def backup(path):
    bak = f"{path}.bak_{TIMESTAMP}"
    shutil.copy2(path, bak)
    return bak


def apply_text_patch(content, old, new, label):
    count = content.count(old)
    if count == 0:
        if new in content:
            print(f"  [SKIP] {label}: already applied")
            return content, False
        raise PatchError(
            f"Anchor text not found for '{label}'. Aborting this "
            f"specific patch to avoid corrupting the file."
        )
    if count > 1:
        raise PatchError(
            f"Anchor text for '{label}' found {count} times "
            f"(expected exactly 1) — aborting."
        )
    content = content.replace(old, new, 1)
    print(f"  [OK]   {label}")
    return content, True


def verify_syntax(path, content):
    try:
        ast.parse(content, filename=path)
        return True, None
    except SyntaxError as e:
        return False, str(e)


def patch_file(key, patches):
    path = FILES[key]
    if not os.path.exists(path):
        raise PatchError(f"{path} not found")

    print(f"\nPatching {os.path.basename(path)} "
          f"{'(dry-run)' if DRY_RUN else ''} ...")
    content = read(path)
    changed_any = False

    for label, old, new in patches:
        content, changed = apply_text_patch(content, old, new, label)
        changed_any = changed_any or changed

    if not changed_any:
        print(f"  Nothing to do for {os.path.basename(path)}.")
        return

    ok, err = verify_syntax(path, content)
    if not ok:
        raise PatchError(
            f"Syntax error after patching "
            f"{os.path.basename(path)}: {err}"
        )
    print(f"  Syntax check: OK")

    if DRY_RUN:
        print(f"  [DRY-RUN] Would write {os.path.basename(path)} "
              f"(no changes written)")
        return

    bak = backup(path)
    write(path, content)
    print(f"  Backed up original -> {os.path.basename(bak)}")
    print(f"  Wrote patched file  -> {os.path.basename(path)}")


# ════════════════════════════════════════════════════════════════
# CONFIG.PY
# ════════════════════════════════════════════════════════════════

_config_margin_old = '''MAX_TRANCHES_PER_STRATEGY = 2

REENTRY_COOLDOWN_SEC       = 300
REENTRY_MAX_SPOT_MOVE_PCT  = 0.02
BUILD_FAILURE_COOLDOWN_SEC = 300'''

_config_margin_new = '''MAX_TRANCHES_PER_STRATEGY = 2

REENTRY_COOLDOWN_SEC       = 300
REENTRY_MAX_SPOT_MOVE_PCT  = 0.02
BUILD_FAILURE_COOLDOWN_SEC = 300

# ─────────────────────────────────────────────────────────────────────
# MARGIN/SPAN APPROXIMATION (heuristic, NOT the real exchange calc)
# PATCH: previously lot sizing never considered margin at all —
# only theoretical max-loss and capital %. For naked/undefined-risk
# strategies, real SPAN+exposure margin is typically far higher
# than max_risk. These are documented, conservative approximations
# for paper-mode sizing realism; live mode still separately
# validates against the real broker margin API via check_margin().
# ─────────────────────────────────────────────────────────────────────
MARGIN_UTILIZATION_PCT        = 0.80  # cap cumulative estimated margin at 80% of capital
SPAN_NAKED_MARGIN_PCT         = 0.11  # ~11% of notional for naked short options (approx)
SPAN_SPREAD_MARGIN_MULTIPLIER = 1.15  # defined-risk spreads: ~1.15x max loss'''

config_patches = [
    ("Add margin/SPAN approximation constants",
     _config_margin_old, _config_margin_new),
]


# ════════════════════════════════════════════════════════════════
# STRATEGY_ENGINE.PY
# ════════════════════════════════════════════════════════════════

_dataclass_old = '''    banked_pnl:           float = 0.0   # PATCH: partial-close pnl
    banked_costs:         float = 0.0   # PATCH: partial-close costs'''

_dataclass_new = '''    banked_pnl:           float = 0.0   # PATCH: partial-close pnl
    banked_costs:         float = 0.0   # PATCH: partial-close costs
    margin_estimate:      float = 0.0   # PATCH: heuristic SPAN/margin estimate'''

_create_record_old = '''            max_risk=meta.get("max_risk", 0.0),
            paper_trade=config.PAPER_TRADING_MODE,
            trend_direction=meta.get(
                "trend_direction", 0.0
            ),
            meta=copy.deepcopy(meta),
        )'''

_create_record_new = '''            max_risk=meta.get("max_risk", 0.0),
            paper_trade=config.PAPER_TRADING_MODE,
            trend_direction=meta.get(
                "trend_direction", 0.0
            ),
            margin_estimate=meta.get("margin_estimate", 0.0),
            meta=copy.deepcopy(meta),
        )'''

_enter_margin_old = '''        for leg in legs:
            leg.qty = leg.qty * lots

        trade_id = str(uuid.uuid4())'''

_enter_margin_new = '''        for leg in legs:
            leg.qty = leg.qty * lots

        # PATCH: store total estimated margin for this position
        # (per-lot heuristic estimate x final lot count), so
        # future sizing calls can track cumulative margin usage
        # across all open positions.
        meta["margin_estimate"] = (
            meta.get("margin_estimate_per_lot", 0.0) * lots
        )

        trade_id = str(uuid.uuid4())'''

_estimate_margin_insert_old = '''    def _calculate_lot_size(
        self, strategy_name: str, meta: Dict
    ) -> int:
        max_loss_per_lot = meta.get("max_risk", 0)
        if max_loss_per_lot <= 0:
            return 0

        risk_per_trade = config.MAX_RISK_PER_TRADE'''

_estimate_margin_insert_new = '''    def _estimate_margin_requirement(
        self, strategy_name: str, max_risk_per_lot: float
    ) -> float:
        """
        PATCH: heuristic SPAN+exposure margin approximation for
        paper-mode sizing realism. This is NOT the real exchange
        calculation (real SPAN varies daily with volatility scans
        and is broker/exchange-specific) — it's a conservative,
        documented approximation so paper-mode position sizes are
        closer to what would actually be achievable live, rather
        than assuming margin is unlimited (the previous behavior).
        Live mode still separately validates against the REAL
        broker margin API via check_margin() in
        _pre_trade_checks().
        """
        spot = self.dm.spot or 24000.0
        notional_per_lot = spot * config.LOT_SIZE

        if strategy_name == config.STRAT_SHORT_STRADDLE:
            # Naked short options on both sides — margin is
            # dominated by SPAN+exposure on notional, not by the
            # (large) theoretical max loss.
            return notional_per_lot * config.SPAN_NAKED_MARGIN_PCT

        elif strategy_name in (
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
        ):
            # Defined-risk spread in the same expiry — exchanges
            # typically apply spread margining close to (a modest
            # multiple of) max loss, not full naked margin.
            return (
                max_risk_per_lot
                * config.SPAN_SPREAD_MARGIN_MULTIPLIER
            )

        elif strategy_name == config.STRAT_RATIO_SPREAD:
            # Mostly hedged (2 long vs 1 short per side) but not a
            # clean defined-risk spread — blend spread-margining
            # with a partial naked-margin allowance.
            return max(
                max_risk_per_lot
                * config.SPAN_SPREAD_MARGIN_MULTIPLIER,
                notional_per_lot
                * config.SPAN_NAKED_MARGIN_PCT * 0.5,
            )

        elif strategy_name == config.STRAT_BACKSPREAD:
            # More longs than shorts by construction — treat as
            # spread-like.
            return (
                max_risk_per_lot
                * config.SPAN_SPREAD_MARGIN_MULTIPLIER
            )

        else:
            # Long-only debit strategies (long straddle, strangle,
            # butterfly, defensive hedge): margin required is just
            # the premium paid, already captured by
            # max_risk_per_lot.
            return max_risk_per_lot

    def _calculate_lot_size(
        self, strategy_name: str, meta: Dict
    ) -> int:
        max_loss_per_lot = meta.get("max_risk", 0)
        if max_loss_per_lot <= 0:
            return 0

        risk_per_trade = config.MAX_RISK_PER_TRADE'''

_lotsize_tail_old = '''        position_cap = math.floor(
            (
                config.POSITION_SIZE_PCT
                * config.TOTAL_CAPITAL
            ) / max_loss_per_lot
        )
        lots = min(lots, max(position_cap, 0))
        lots = max(lots, 0)

        logger.info(
            f"Lot size: {lots} for {strategy_name} "
            f"risk={risk_per_trade} "
            f"max_loss={max_loss_per_lot:.0f}"
        )
        return lots'''

_lotsize_tail_new = '''        position_cap = math.floor(
            (
                config.POSITION_SIZE_PCT
                * config.TOTAL_CAPITAL
            ) / max_loss_per_lot
        )
        lots = min(lots, max(position_cap, 0))
        lots = max(lots, 0)

        # PATCH: heuristic margin/SPAN cap. Previously sizing was
        # based purely on theoretical max-loss and capital %,
        # never on what margin the position would actually tie up
        # — which for naked/undefined-risk strategies (short
        # straddle, ratio spread) is typically far higher than
        # max_risk. This is a documented approximation, not the
        # real exchange calculation (live mode still separately
        # validates against the real broker margin API in
        # _pre_trade_checks()).
        margin_per_lot = self._estimate_margin_requirement(
            strategy_name, max_loss_per_lot
        )
        meta["margin_estimate_per_lot"] = margin_per_lot
        if margin_per_lot > 0:
            deployed_margin = sum(
                getattr(p, "margin_estimate", 0.0)
                for p in self.open_positions
            )
            margin_budget = (
                config.MARGIN_UTILIZATION_PCT
                * config.TOTAL_CAPITAL
            )
            available_margin = margin_budget - deployed_margin
            if available_margin <= 0:
                logger.info(
                    f"Lot size: 0 for {strategy_name} — "
                    f"margin budget exhausted "
                    f"(deployed={deployed_margin:.0f}/"
                    f"{margin_budget:.0f})"
                )
                return 0
            lots_by_margin = math.floor(
                available_margin / margin_per_lot
            )
            lots = min(lots, lots_by_margin)
            lots = max(lots, 0)

        logger.info(
            f"Lot size: {lots} for {strategy_name} "
            f"risk={risk_per_trade} "
            f"max_loss={max_loss_per_lot:.0f} "
            f"margin_per_lot={margin_per_lot:.0f}"
        )
        return lots'''

strategy_engine_patches = [
    ("Add margin_estimate field to Position",
     _dataclass_old, _dataclass_new),
    ("Persist margin_estimate in _create_position_record",
     _create_record_old, _create_record_new),
    ("Store total margin_estimate in _enter_new_position",
     _enter_margin_old, _enter_margin_new),
    ("Insert _estimate_margin_requirement()",
     _estimate_margin_insert_old, _estimate_margin_insert_new),
    ("Add margin-budget cap to _calculate_lot_size",
     _lotsize_tail_old, _lotsize_tail_new),
]


def main():
    print("=" * 70)
    print("MARGIN/SPAN APPROXIMATION — FINAL FIX SCRIPT"
          + (" [DRY RUN]" if DRY_RUN else ""))
    print("=" * 70)

    errors = []

    try:
        patch_file("config", config_patches)
    except PatchError as e:
        print(f"  [ABORTED] config.py: {e}")
        errors.append(("config", str(e)))

    try:
        patch_file("strategy_engine", strategy_engine_patches)
    except PatchError as e:
        print(f"  [ABORTED] strategy_engine.py: {e}")
        errors.append(("strategy_engine", str(e)))

    print("\n" + "=" * 70)
    if errors:
        print(f"COMPLETED WITH {len(errors)} FAILURE(S):")
        for key, err in errors:
            print(f"  - {key}.py: {err}")
        sys.exit(1)

    print("ALL PATCHES " + ("VALIDATED (dry-run)" if DRY_RUN
                             else "APPLIED SUCCESSFULLY"))
    print("=" * 70)
    print(
        "\nWhat to watch for next:\n"
        "  - Lot-size log lines now show 'margin_per_lot=...' —\n"
        "    for STRAT_SHORT_STRADDLE this should look noticeably\n"
        "    larger relative to max_loss than for\n"
        "    IRON_CONDOR/CREDIT_SPREADS.\n"
        "  - If a position ever gets sized to fewer lots than\n"
        "    before, or blocked with 'margin budget exhausted',\n"
        "    that's this heuristic actively constraining sizing —\n"
        "    expected behavior, not a bug.\n"
        "  - This is a heuristic, not the real SPAN formula — if\n"
        "    you ever get real margin figures from Upstox (e.g.\n"
        "    via the live check_margin() call, or their margin\n"
        "    calculator), it's worth comparing against these\n"
        "    estimates and adjusting SPAN_NAKED_MARGIN_PCT /\n"
        "    SPAN_SPREAD_MARGIN_MULTIPLIER accordingly."
    )


if __name__ == "__main__":
    main()