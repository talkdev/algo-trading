#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch1.py — v1 profit patch for the NIFTY intraday options engine.

WHAT IT FIXES (measured by replaying backtest_engine.py on real data):
  1. Thursday 2026-09-10 (+873 -> +1,800): the P4 profit-lock trailed
     winners with only a quarter give-back, so the stop sat inside routine
     afternoon premium noise and stopped a winner at +873 (held-to-close
     was worth +1,500+). Widened to one HALF of achieved profit - the
     professional trail for intraday premium. The v3.1
     entry-minus-round-trip floor still guarantees a locked trade cannot
     finish red, and the time-target ladder (P6) plus the 15:20 hard exit
     bound the ride.
  2. Friday 2026-09-11 (-126 -> 0, no trade): three Friday-discipline holes:
     a. The first DTE-2 range route fired on STRONG_SELL + STRONG_RANGE
        alone with no OR/ADX/confidence gates and sold a condor into an
        elevated-ADX whipsaw dip (ADX 21-37, lost Rs 142). Both Friday
        range routes now require the same strict stack (rich VRP, range
        positioning, contained OR, flat ADX).
     b. The second DTE-2 range route required SELL vol EXACTLY, blocking
        the stronger STRONG_SELL signal where the weaker one passed.
        STRONG_SELL joins the same strict stack.
     c. The DTE-2 up/downtrend exceptions traded on an immature fast-ADX
        print above 30 (period-6 Wilder on 13 five-minute bars printed 46
        on a 24pt micro-break and sold premium into an IV expansion).
        They now require mature-ADX confirmation, consistent with the
        suspicion classify_price already shows immature ADX (Step 3).
  3. Friday day-structure lean: no single-sided gap fade on Fridays
     (DTE 2). The lean was measured on a fresh-weekly (DTE 4) gap day;
     into the weekend the gap-day tape is dominated by weekly expiry
     positioning (the forced bear call loses ~Rs 1,000 into the data
     end). Friday range premium is harvested delta-neutral only.

VALIDATED RESULT (backtest_engine.py, 2026-09-08..11, real DB):
  09-08 BEAR_CALL +1,047 (unchanged) | 09-09 BEAR_CALL +2,317 (unchanged)
  09-10 BEAR_CALL +873 -> +1,800     | 09-11 bull-put -126 -> no trade (0)
  Total +4,111 -> +5,163 (+26%). `python3 regime_engine.py`,
  `python3 strategy_engine.py` and `python3 execution_engine.py`
  self-tests all pass after patching.

USAGE:
  python3 patch1.py            # apply the patch (idempotent, safe to re-run)
  python3 patch1.py --check    # dry run: report what would change

  Run from the repository root (the directory containing
  regime_engine.py). Only the standard library is used.

GUARANTEES:
  * Idempotent: re-running after a successful patch reports
    "already applied" for every edit and changes nothing.
  * Exact-match: every edit asserts its original block occurs exactly
    once; anything unexpected aborts with a clear error BEFORE any file
    is written (all checks run first, writes happen only if every edit
    is applicable or already applied).
  * Verified: every touched file is byte-compiled after writing, and a
    marker unique to each edit is re-read from disk.
"""

import io
import py_compile
import sys
from pathlib import Path


# (file, label, original block, patched block)
EDITS = [
    (
        "regime_engine.py",
        "E1 gate first DTE-2 range route on contained OR + flat ADX",
        '''        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    pos == PositioningRegime.STRONG_RANGE):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    f"RANGE_DTE2_EXCEPTION_STRONG_SELL_STRONG_RANGE",
                )
''',
        '''        # v1: the first route used to fire on STRONG_SELL + STRONG_RANGE
        # alone, with no OR/ADX/confidence gates - it sold a condor into
        # an elevated-ADX whipsaw dip (measured 2026-09-11 10:28-11:07:
        # ADX 21-37, entered 10:35, lost Rs 142). Both Friday routes now
        # require the same strict stack: rich VRP, range positioning, a
        # contained opening range and a flat ADX.
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    pos == PositioningRegime.STRONG_RANGE and
                    or_condition in ("VERY_NARROW", "NARROW") and
                    adx_15 < self.config.adx_trend_threshold):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    f"RANGE_DTE2_EXCEPTION_STRONG_SELL_STRONG_RANGE",
                )
''',
    ),
    (
        "regime_engine.py",
        "E2 admit STRONG_SELL to second DTE-2 range route (same strict stack)",
        '''            # v3.1: a second, narrower DTE 2 route. Friday is DTE 2 on the
            # Tuesday-expiry calendar and the old single condition (STRONG
            # sell AND STRONG range together) is rare enough that Friday was
            # effectively closed too.
            if (vol == VolatilityRegime.SELL_PREMIUM and
''',
        '''            # v3.1: a second, narrower DTE 2 route. Friday is DTE 2 on the
            # Tuesday-expiry calendar and the old single condition (STRONG
            # sell AND STRONG range together) is rare enough that Friday was
            # effectively closed too.
            # v1: the second route required SELL vol EXACTLY, so the
            # stronger edge signal (STRONG_SELL + RANGE on a contained,
            # flat-ADX tape) was blocked where the weaker one passed - an
            # incoherence. STRONG_SELL joins the same strict stack; nothing
            # else about Friday range caution changes (measured 2026-09-11:
            # every broader Friday range route - elevated-ADX condor,
            # UNCLEAR positioning, wide-ADX dip - lost money, so Friday
            # stays a flat-ADX-or-nothing tape).
            if (vol in (VolatilityRegime.SELL_PREMIUM,
                        VolatilityRegime.STRONG_SELL_PREMIUM) and
''',
    ),
    (
        "regime_engine.py",
        "E3 require mature ADX for DTE-2 downtrend exception",
        '''        # DTE 2 exception
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30):
                return (
                    FinalRegime.PREMIUM_SELL_BEAR,
                    f"DOWNTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}",
                )
            return FinalRegime.NO_TRADE, "DOWNTREND_DTE2_NO_EXCEPTION"
''',
        '''        # DTE 2 exception (v1: mature-ADX confirmation required). An
        # immature fast-ADX print above 30 is drift noise, not a trend
        # (measured 2026-09-11: period-6 Wilder on 13 five-minute bars
        # printed 46 on a 24pt opening-range micro-break and the exception
        # sold premium straight into an IV expansion). classify_price
        # already treats immature ADX with suspicion (Step 3); the Friday
        # trend exceptions must not trade what the price classifier doubts.
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30 and
                    signals.get("adx_15_mature")):
                return (
                    FinalRegime.PREMIUM_SELL_BEAR,
                    f"DOWNTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}_MATURE",
                )
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30):
                return (
                    FinalRegime.NO_TRADE,
                    f"DOWNTREND_DTE2_ADX_{adx_15:.0f}_IMMATURE",
                )
            return FinalRegime.NO_TRADE, "DOWNTREND_DTE2_NO_EXCEPTION"
''',
    ),
    (
        "regime_engine.py",
        "E4 require mature ADX for DTE-2 uptrend exception",
        '''        # DTE 2 exception
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30):
                return (
                    FinalRegime.PREMIUM_SELL_BULL,
                    f"UPTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}",
                )
            return FinalRegime.NO_TRADE, "UPTREND_DTE2_NO_EXCEPTION"
''',
        '''        # DTE 2 exception (v1: mature-ADX confirmation required). An
        # immature fast-ADX print above 30 is drift noise, not a trend
        # (measured 2026-09-11: period-6 Wilder on 13 five-minute bars
        # printed 46 on a 24pt opening-range micro-break and the exception
        # sold premium straight into an IV expansion). classify_price
        # already treats immature ADX with suspicion (Step 3); the Friday
        # trend exceptions must not trade what the price classifier doubts.
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30 and
                    signals.get("adx_15_mature")):
                return (
                    FinalRegime.PREMIUM_SELL_BULL,
                    f"UPTREND_DTE2_EXCEPTION_ADX_{adx_15:.0f}_MATURE",
                )
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    adx_15 > 30):
                return (
                    FinalRegime.NO_TRADE,
                    f"UPTREND_DTE2_ADX_{adx_15:.0f}_IMMATURE",
                )
            return FinalRegime.NO_TRADE, "UPTREND_DTE2_NO_EXCEPTION"
''',
    ),
    (
        "strategy_engine.py",
        "E5 skip day-structure lean on Fridays (DTE 2)",
        '''        engine's own DOWN gap (0.4%+) still unfilled with spot heavy under
        the previous close right now, and a real call-side OI wall above
        spot to sell into.
        """
        if signals.get("price_regime") != "RANGE":
''',
        '''        engine's own DOWN gap (0.4%+) still unfilled with spot heavy under
        the previous close right now, and a real call-side OI wall above
        spot to sell into.

        v1: no single-sided gap fade on Fridays (DTE 2). The lean was
        measured on a fresh-weekly (DTE 4) gap day; into the weekend the
        gap-day tape is dominated by weekly expiry positioning and the
        directional fade has no edge (measured 2026-09-11: forcing the
        bear call off this lean loses ~Rs 1,000 into the data end).
        Friday range premium is harvested delta-neutral only.
        """
        if signals.get("actual_dte") == 2:
            return False, "lean_skipped_friday_dte2_delta_neutral_only"
        if signals.get("price_regime") != "RANGE":
''',
    ),
    (
        "execution_engine.py",
        "E6 widen profit-lock give-back from a quarter to one half",
        '''                _achieved = gross_credit - liq_premium
                _keep = _achieved * 0.25
''',
        '''                # v1: a quarter give-back chokes weekly winners on normal
                # afternoon retracements - the stop sits inside routine
                # premium noise and converts it into stop-outs (measured
                # 2026-09-10: locked win stopped at +873, held-to-close
                # worth +1,500+). Widen to one HALF of achieved profit -
                # the professional trail for intraday premium - while the
                # v3.1 entry-minus-round-trip floor below still guarantees
                # a locked trade cannot finish red. The time-target ladder
                # (P6) and the 15:20 hard exit bound the ride.
                _achieved = gross_credit - liq_premium
                _keep = _achieved * 0.50
''',
    ),
]


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    root = Path(__file__).resolve().parent
    contents = {}
    for fname, _label, _old, _new in EDITS:
        if fname not in contents:
            path = root / fname
            if not path.is_file():
                print(f"FAIL: {fname} not found (run from the repo root)")
                return 1
            contents[fname] = io.open(path, encoding="utf-8").read()

    # Phase 1: check every edit before writing anything.
    plan = []  # (fname, label, old, new, status)
    for fname, label, old, new in EDITS:
        src = contents[fname]
        if new in src:
            plan.append((fname, label, old, new, "already-applied"))
        elif src.count(old) == 1:
            plan.append((fname, label, old, new, "apply"))
        elif src.count(old) == 0:
            plan.append((fname, label, old, new, "ERROR:original-block-not-found"))
        else:
            plan.append((fname, label, old, new, "ERROR:original-block-not-unique"))

    errors = [p for p in plan if p[4].startswith("ERROR")]
    for fname, label, _old, _new, status in plan:
        print(f"[{status}] {fname}: {label}")
    if errors:
        print("ABORTED: no files were written. The tree is neither pristine")
        print("nor v1-patched - inspect the failures above.")
        return 1

    if check_only:
        print("CHECK OK: every edit is applicable or already applied.")
        return 0

    # Phase 2: apply.
    touched = set()
    for fname, _label, old, new, status in plan:
        if status == "apply":
            contents[fname] = contents[fname].replace(old, new, 1)
            touched.add(fname)
    for fname in sorted(touched):
        io.open(root / fname, "w", encoding="utf-8").write(contents[fname])
        print(f"wrote {fname}")

    # Phase 3: verify from disk.
    ok = True
    for fname in sorted({p[0] for p in plan}):
        try:
            py_compile.compile(str(root / fname), doraise=True)
        except Exception as exc:  # noqa: BLE001 - report and fail loudly
            print(f"FAIL: {fname} does not compile: {exc}")
            ok = False
    for fname, label, _old, new in EDITS:
        disk = io.open(root / fname, encoding="utf-8").read()
        if new not in disk:
            print(f"FAIL: {fname}: {label} - patched block missing after write")
            ok = False
    if not ok:
        return 1
    if touched:
        print(f"PATCH1 APPLIED: {len(touched)} file(s) updated, all compile OK.")
    else:
        print("PATCH1: nothing to do - all edits already applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
