#!/usr/bin/env python3
"""
patch_cost_model.py — Production cost-model update: NIFTY lot
size 75->65, and full separation of NSE transaction charge / STT /
IPFT / SEBI fee / stamp duty / GST into distinct, named
constants per the supplied production verification spec.

IMPORTANT: the specific rates/dates below were supplied externally
by the user as verified values — this codebase has not
independently confirmed the underlying NSE circulars. Re-check
against a live circular / your broker's contract note before
relying on this in production, per the verification checklist
that was supplied alongside these values.

Brokerage is deliberately NOT changed — it remains the existing
Rs 20/order Upstox flat-fee assumption that predates this patch.
Per the supplied instruction ("do NOT invent or assume
brokerage"), that number needs to come from your actual account
tariff/contract note, not from this patch.

WHAT THIS CHANGES
-------------------
[config.py]
  - LOT_SIZE: 75 -> 65
  - New separated cost constants: COST_STT_OPTION_SELL_PCT,
    COST_STT_EXERCISE_PCT, COST_EXCHANGE_PCT, COST_NSE_IPFT_PCT,
    COST_SEBI_PCT, COST_STAMP_PCT, COST_GST_PCT,
    COST_BROKERAGE_PER_ORDER, COST_MODEL_VERIFIED_ON

[strategy_engine.py]
  - _calculate_transaction_costs(): rewritten to use the new
    named constants (adds NSE IPFT as its own line item; GST
    base unchanged — applies only to brokerage + exchange
    charge, never to STT/stamp/SEBI, which was already correct).
  - _close_one_side()'s banked-cost formula: same update, for
    consistency with the main cost function.

Usage:
    python patch_cost_model.py --dry-run
    python patch_cost_model.py
"""

import ast
import os
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

_lotsize_old = '''LOT_SIZE          = 75     # NSE NIFTY confirmed lot size
NIFTY_STRIKE_STEP = 100    # weekly options 100-pt steps'''

_lotsize_new = '''# Parameter: LOT_SIZE
#   Old value: 75   New value: 65   Unit: shares/contract
#   Effective: per transition period following NSE circular
#   NSE/FAOP/70616 dated 03-Oct-2025 (revised NIFTY lot 75->65).
#   Source: user-supplied verification, NOT independently
#   confirmed by this codebase. Broker-specific override: the
#   Upstox instrument master should be treated as the final
#   contract-level validation source before relying on this
#   constant in production — cross-check there before going live.
#   Verification date: 2026-08-31 (as supplied).
LOT_SIZE          = 65
NIFTY_STRIKE_STEP = 100    # weekly options 100-pt steps'''

_capitalrisk_old = '''MAX_RISK_PER_TRADE = int(
    MAX_RISK_PER_TRADE_PCT * TOTAL_CAPITAL
)
MAX_COMBINED_RISK  = int(
    MAX_COMBINED_RISK_PCT * TOTAL_CAPITAL
)
MAX_DAILY_LOSS     = int(
    MAX_DAILY_LOSS_PCT * TOTAL_CAPITAL
)
MAX_DRAWDOWN       = int(
    MAX_DRAWDOWN_PCT * TOTAL_CAPITAL
)

# ─────────────────────────────────────────────────────────────────────
# VIX BANDS
# ─────────────────────────────────────────────────────────────────────'''

_capitalrisk_new = '''MAX_RISK_PER_TRADE = int(
    MAX_RISK_PER_TRADE_PCT * TOTAL_CAPITAL
)
MAX_COMBINED_RISK  = int(
    MAX_COMBINED_RISK_PCT * TOTAL_CAPITAL
)
MAX_DAILY_LOSS     = int(
    MAX_DAILY_LOSS_PCT * TOTAL_CAPITAL
)
MAX_DRAWDOWN       = int(
    MAX_DRAWDOWN_PCT * TOTAL_CAPITAL
)

# ─────────────────────────────────────────────────────────────────────
# TRANSACTION COST MODEL (production verification record)
# Kept as SEPARATE named categories per verification requirements —
# do not recombine into one generic transaction-cost percentage.
# All rates below were supplied externally as verified values; NOT
# independently confirmed by this codebase. Re-check against a live
# NSE circular / broker contract note before production use.
# ─────────────────────────────────────────────────────────────────────

# Parameter: COST_STT_OPTION_SELL_PCT
#   Old value: 0.001 (0.10%)   New value: 0.0015 (0.15%)
#   Unit: % of option premium   Side: Seller
#   Effective: 01-Apr-2026   Basis: Finance Act 2026 (as supplied)
COST_STT_OPTION_SELL_PCT = 0.0015

# Parameter: COST_STT_EXERCISE_PCT
#   0.15% of intrinsic value, charged on exercise of an ITM option.
#   NOTE: this engine always closes positions via market order
#   before/at expiry (_close_position / _expiry_day_close_all)
#   rather than letting them run into exercise — defined here for
#   completeness/architecture correctness; not currently applied
#   anywhere since the exercise code path doesn't exist here.
COST_STT_EXERCISE_PCT = 0.0015

# Parameter: COST_EXCHANGE_PCT
#   Old value: 0.0000325 (0.00325%)   New: 0.0003552 (0.03552%)
#   Unit: % of total turnover, both sides
#   Effective: 01-Mar-2026   Basis: Rs 3,552/crore/side
COST_EXCHANGE_PCT = 0.0003552

# Parameter: COST_NSE_IPFT_PCT
#   New value: 0.000000001 (Rs 0.01/crore/side) — economically
#   negligible, modeled separately per architecture requirement
#   (must not be silently folded into exchange charge).
#   Unit: % of total turnover, both sides   Effective: 01-Mar-2026
COST_NSE_IPFT_PCT = 0.000000001

# Parameter: COST_SEBI_PCT
#   Unchanged: 0.000001 (0.0001%)   Unit: % of total turnover
COST_SEBI_PCT = 0.000001

# Parameter: COST_STAMP_PCT
#   Old value: 0.00015 (0.015%)   New value: 0.00003 (0.003%)
#   Unit: % of buy-side value only   Side: Buyer
COST_STAMP_PCT = 0.00003

# Parameter: COST_GST_PCT
#   Unchanged: 0.18 (18%). Applies ONLY to brokerage + exchange
#   transaction charge (taxable service components) — never to
#   STT, stamp duty, SEBI fee, or IPFT. This was already correct
#   in the pre-existing cost function; kept unchanged here.
COST_GST_PCT = 0.18

# Parameter: COST_BROKERAGE_PER_ORDER
#   Kept at Rs 20/order — pre-existing Upstox flat-fee assumption,
#   NOT invented for this update. Per the supplied instruction to
#   never assume brokerage: CONFIRM this against your actual
#   Upstox account tariff / contract note before production use.
COST_BROKERAGE_PER_ORDER = 20.0

COST_MODEL_VERIFIED_ON = date(2026, 8, 31)

# ─────────────────────────────────────────────────────────────────────
# VIX BANDS
# ─────────────────────────────────────────────────────────────────────'''

config_patches = [
    ("Update LOT_SIZE 75 -> 65 (with audit record)",
     _lotsize_old, _lotsize_new),
    ("Add separated transaction cost model constants",
     _capitalrisk_old, _capitalrisk_new),
]


# ════════════════════════════════════════════════════════════════
# STRATEGY_ENGINE.PY
# ════════════════════════════════════════════════════════════════

_txcost_old = '''    def _calculate_transaction_costs(
        self, position: Position
    ) -> float:
        """
        Calculate NSE transaction costs.
        FIX QS7: brokerage = flat ₹20 per order.
        """
        if not position.legs:
            return 0.0

        buy_value  = 0.0
        sell_value = 0.0
        num_orders = 0

        for leg in position.legs:
            if leg.entry_price > 0 and leg.qty > 0:
                value      = (
                    leg.entry_price
                    * leg.qty
                    * config.LOT_SIZE
                )
                num_orders += 1
                if leg.action == "BUY":
                    buy_value  += value
                else:
                    sell_value += value

            if leg.exit_price > 0 and leg.qty > 0:
                value      = (
                    leg.exit_price
                    * leg.qty
                    * config.LOT_SIZE
                )
                num_orders += 1
                if leg.action == "SELL":
                    buy_value  += value
                else:
                    sell_value += value

        total_turnover = buy_value + sell_value
        if total_turnover <= 0:
            return 0.0

        # FIX QS7: flat ₹20 per order (Upstox flat fee)
        brokerage    = 20.0 * num_orders
        stt          = sell_value * 0.001
        exchange_fee = total_turnover * 0.0000325
        sebi         = total_turnover * 0.000001
        stamp        = buy_value    * 0.00015
        gst          = (brokerage + exchange_fee) * 0.18

        return round(
            brokerage + stt + exchange_fee
            + sebi + stamp + gst,
            2,
        )'''

_txcost_new = '''    def _calculate_transaction_costs(
        self, position: Position
    ) -> float:
        """
        Calculate NSE transaction costs.
        PATCH: rates updated per production verification record
        (see config.py's COST_* constants). Kept as separate named
        categories (brokerage, exchange charge, NSE IPFT, SEBI
        fee, STT, stamp duty, GST) rather than one generic
        percentage, per the verification requirements. Rates were
        supplied externally, NOT independently confirmed by this
        codebase — re-check against a live NSE circular / broker
        contract note before production use.
        """
        if not position.legs:
            return 0.0

        buy_value  = 0.0
        sell_value = 0.0
        num_orders = 0

        for leg in position.legs:
            if leg.entry_price > 0 and leg.qty > 0:
                value      = (
                    leg.entry_price
                    * leg.qty
                    * config.LOT_SIZE
                )
                num_orders += 1
                if leg.action == "BUY":
                    buy_value  += value
                else:
                    sell_value += value

            if leg.exit_price > 0 and leg.qty > 0:
                value      = (
                    leg.exit_price
                    * leg.qty
                    * config.LOT_SIZE
                )
                num_orders += 1
                if leg.action == "SELL":
                    buy_value  += value
                else:
                    sell_value += value

        total_turnover = buy_value + sell_value
        if total_turnover <= 0:
            return 0.0

        brokerage    = (
            config.COST_BROKERAGE_PER_ORDER * num_orders
        )
        stt          = sell_value * config.COST_STT_OPTION_SELL_PCT
        exchange_fee = total_turnover * config.COST_EXCHANGE_PCT
        ipft         = total_turnover * config.COST_NSE_IPFT_PCT
        sebi         = total_turnover * config.COST_SEBI_PCT
        stamp        = buy_value * config.COST_STAMP_PCT
        # GST applies ONLY to brokerage + exchange charge — not to
        # STT, stamp duty, SEBI fee, or IPFT (unchanged from the
        # original, already-correct logic).
        gst          = (
            brokerage + exchange_fee
        ) * config.COST_GST_PCT

        return round(
            brokerage + stt + exchange_fee + ipft
            + sebi + stamp + gst,
            2,
        )'''

_bankedcost_old = '''                _leg_value = (
                    exit_price * qty_closed * config.LOT_SIZE
                )
                _leg_brokerage = 20.0
                _leg_exchange = _leg_value * 0.0000325
                _leg_sebi = _leg_value * 0.000001
                if leg.action == "SELL":
                    _leg_stt = 0.0
                    _leg_stamp = _leg_value * 0.00015
                else:
                    _leg_stt = _leg_value * 0.001
                    _leg_stamp = 0.0
                _leg_gst = (
                    _leg_brokerage + _leg_exchange
                ) * 0.18
                leg_cost = (
                    _leg_brokerage + _leg_exchange
                    + _leg_sebi + _leg_stt + _leg_stamp
                    + _leg_gst
                )'''

_bankedcost_new = '''                # PATCH: rates updated per production
                # verification record (config.py's COST_*
                # constants), matching _calculate_transaction_costs().
                _leg_value = (
                    exit_price * qty_closed * config.LOT_SIZE
                )
                _leg_brokerage = config.COST_BROKERAGE_PER_ORDER
                _leg_exchange = (
                    _leg_value * config.COST_EXCHANGE_PCT
                )
                _leg_ipft = (
                    _leg_value * config.COST_NSE_IPFT_PCT
                )
                _leg_sebi = _leg_value * config.COST_SEBI_PCT
                if leg.action == "SELL":
                    _leg_stt = 0.0
                    _leg_stamp = (
                        _leg_value * config.COST_STAMP_PCT
                    )
                else:
                    _leg_stt = (
                        _leg_value
                        * config.COST_STT_OPTION_SELL_PCT
                    )
                    _leg_stamp = 0.0
                _leg_gst = (
                    _leg_brokerage + _leg_exchange
                ) * config.COST_GST_PCT
                leg_cost = (
                    _leg_brokerage + _leg_exchange + _leg_ipft
                    + _leg_sebi + _leg_stt + _leg_stamp
                    + _leg_gst
                )'''

strategy_engine_patches = [
    ("Update _calculate_transaction_costs with new cost model",
     _txcost_old, _txcost_new),
    ("Update _close_one_side banked-cost formula",
     _bankedcost_old, _bankedcost_new),
]


def main():
    print("=" * 70)
    print("COST MODEL / LOT SIZE UPDATE — PRODUCTION VERIFICATION"
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
        "\nBEFORE marking production-ready, per the supplied "
        "checklist, still required:\n"
        "  1. Compare against an actual recent broker contract note\n"
        "  2. Verify one SELL, one BUY, one Iron Condor, one\n"
        "     4-leg round trip — reconcile every rupee\n"
        "  3. Confirm LOT_SIZE=65 against the live Upstox\n"
        "     instrument master (not just this constant)\n"
        "  4. Confirm COST_BROKERAGE_PER_ORDER=20 against your\n"
        "     actual account tariff — this was NOT changed by\n"
        "     this patch and predates it\n"
        "  5. Re-run backtests with this cost model and do NOT\n"
        "     compare directly against pre-patch backtest numbers\n"
        "     without documenting the cost-model change"
    )


if __name__ == "__main__":
    main()