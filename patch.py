#!/usr/bin/env python3
"""v11: raise the per-trade risk budget from 1.2% to 2.0% of capital.

    python3 patch_v11.py                 # applies to the directory it lives in
    python3 patch_v11.py /path/to/repo   # or to an explicit checkout

READ THIS FIRST: this patch buys CAGR with LEVERAGE, not with edge. Nothing
about signal quality, strike selection or exits changes. It is the same five
setups on the same four sessions at a bigger ticket. If you are looking for
more alpha, this is not it - see "What was NOT found" below.

Requires patch_v10.py to be applied first (the numbers below were measured
with the v10 exit ladder in place); this patch refuses to run otherwise.

Self-contained, all-or-nothing, hash-gated, compile-checked, rolls back on
failure, and re-running is a no-op. Exit codes: 0 applied or already up to
date, 2 wrong directory, 3 unrecognised content / patch_v10 missing,
4 compile failure (reverted).

File patched: core.py (two numeric defaults in load_config). No schema, no
strategy logic, no exit logic.

------------------------------------------------------------------------------
MEASURED EFFECT - 2026-09-08..11 replayed in ONE process, v10 exit ladder
------------------------------------------------------------------------------
  per-trade / daily      4-day net    x base   worst single-trade   worst observed
    risk budget                                     structural loss  unrealised P&L
  ------------------------------------------------------------------------------
  1.2% /  4.0% (v10)     +12,494.66    1.00x     32,925 (3.29%)      -3,795
  2.0% /  8.0% (THIS)    +18,596.09    1.49x     41,321 (4.13%)      -4,744
  3.0% / 10.0%           +21,774.72    1.74x     54,628 (5.46%)      -4,744

  per day, 2.0% / 8.0%:   09-08 +2,544.06   09-09 +3,908.73
                          09-10 +3,048.53   09-11 +9,094.77 (2 trades)

Three things in that table matter more than the headline:

1. PROFIT SCALES SUB-LINEARLY, RISK SCALES LINEARLY. 1.67x the budget buys
   1.49x the profit, because LOT_CAPS_BY_DAY binds before the budget does -
   Friday caps at 5 lots, so 2026-09-11's long call is the SAME 5 lots at
   2.0% and at 3.0%. The tail does not have that ceiling.

2. THE DAILY STOP HAS TO MOVE WITH IT. The clamp in load_config requires
   per-trade < daily/3, so 2.0% needs a daily stop above 6.0%. It is set to
   8.0%: three consecutive structural losses at 2.0% still fit inside it,
   which is the arithmetic the clamp exists to protect. Raising the daily
   circuit breaker is the real cost of this patch - it is the last line of
   defence on a day the models are wrong.

3. REALISED RISK IS FAR BELOW STRUCTURAL RISK. Across all four sessions the
   worst mark-to-market excursion actually reached was -4,744 against a
   41,321 structural maximum. That gap is why the size increase looks safe
   here - and it is measured on FOUR sessions with a 100% win rate. The gap
   is exactly what a single gap-open through a short strike would close.

------------------------------------------------------------------------------
REVERTING / GOING FURTHER - no code change needed either way
------------------------------------------------------------------------------
env.txt overrides both defaults, so once this patch is applied:

  MAX_RISK_PER_TRADE_PCT=0.012   # back to the v10 baseline
  MAX_DAILY_LOSS_PCT=0.04

  MAX_RISK_PER_TRADE_PCT=0.030   # the more aggressive row above
  MAX_DAILY_LOSS_PCT=0.10

------------------------------------------------------------------------------
WHAT WAS NOT FOUND - so you do not have to look again
------------------------------------------------------------------------------
* EXITS ARE ALREADY AT THE ACHIEVABLE MAXIMUM on three of the four days.
  Rebuilding each position's liquidation mark from option_chain_snapshot:
  2026-09-09 realised +12.54 pts where the best mark available at ANY earlier
  cycle was +8.26; 2026-09-10 realised +9.87 against +7.35. There is no exit
  rule that extracts more from those two structures. 2026-09-08 was the one
  exception and patch_v10 already took it (+1,042 -> +1,664).
* MORE ENTRIES IS NOT AVAILABLE WITHOUT BREAKING A GATE THAT PAYS. The funnel
  over these four sessions is 1,686 cycles -> 5 entries, blocked mainly by
  PAST_14:30 (644), DTE (328), OR_NOT_ESTABLISHED (253) and the 0DTE entry
  window (171). The engine's own phantom_trades table replays what blocked
  setups would have done: on 2026-09-08 all ten blocked iron condors were
  simulated LOSSES totalling -81,304. Loosening those gates manufactures
  trade count at a measured negative price.
* The two knobs that actually raise this book's CAGR are therefore ticket
  size (this patch) and the daily lot caps in strategy_engine.LOT_CAPS_BY_DAY
  - both leverage, neither alpha.

------------------------------------------------------------------------------
HONEST CAVEAT
------------------------------------------------------------------------------
Annualising a four-session, five-trade, 100%-win-rate sample produces
triple-digit percentages that mean nothing as a forecast. The reproducible
claims here are narrow: the change is deterministic, it lifts every one of
the four days, it never reduces a day, and the risk it adds is quantified
above. Re-validate on sessions you have not looked at before trusting any of
it with real money.
"""

import hashlib
import os
import py_compile
import sys
import tempfile

TARGETS = (
    "core.py",
)

CREATED = ()

# patch_v10 must already be applied: the numbers this patch was measured
# against include the v10 exit ladder. Checked in main(), not just documented.
PREREQ = {
    "execution_engine.py": "e9bb4a84d677b59af792746a63632642",
}

MD5 = {
    "core.py": {
        "pre": "0bd8fd489fb67de64cb14896166acc79",
        "v11": "696900b544b4ffdf9b90a13d7a40911c",
    },
}

CHANGES = (
    "core.py: MAX_RISK_PER_TRADE_PCT default 0.012 -> 0.020 (1.2% -> 2.0% "
    "of capital per trade)",
    "core.py: MAX_DAILY_LOSS_PCT default 0.04 -> 0.08 (4% -> 8% daily "
    "circuit breaker), required by the per-trade < daily/3 clamp and by "
    "letting the book survive two losers without halting",
    "nothing else: no exit rule, entry gate, strategy, schema or lot cap "
    "touched",
    "measured on 2026-09-08..11 in one session with patch_v10 applied: "
    "+12,494.66 -> +18,596.09 (1.49x); every day up, none down",
    "cost: worst single-trade structural loss 32,925 (3.29%) -> 41,321 "
    "(4.13%) of capital",
    "revert without a code change: MAX_RISK_PER_TRADE_PCT=0.012 and "
    "MAX_DAILY_LOSS_PCT=0.04 in env.txt",
)

# (file, [anchor candidates], replacement) - anchored whole-block edits.
PLAN_V11 = [
    (
        "core.py",
        [
            '    # structure at a single lot, below the size where fixed brokerage (per\n    # order, not per lot) can be amortised against the slow DTE-3/4 theta —\n    # so directionally-correct near-weekly trades still lost to costs. A\n    # defined-risk (capped-loss) intraday structure can carry a larger per-\n    # trade budget than naked selling; 1.2% per trade / 4.0% daily lets it\n    # size to a cost-viable 2+ lots while keeping the daily stop intact.\n    max_daily_loss_pct = _get_float(env, "MAX_DAILY_LOSS_PCT", 0.04)\n    max_risk_per_trade_pct = _get_float(env, "MAX_RISK_PER_TRADE_PCT", 0.012)\n\n    # Clamp: max risk per trade must be < max daily loss / 3\n    safe_max = round(max_daily_loss_pct / 3.0 - 0.001, 4)\n    if max_risk_per_trade_pct >= max_daily_loss_pct / 3.0:\n        print(\n            f"[WARNING] MAX_RISK_PER_TRADE_PCT clamped to {safe_max} "\n',
        ],
        '    # structure at a single lot, below the size where fixed brokerage (per\n    # order, not per lot) can be amortised against the slow DTE-3/4 theta —\n    # so directionally-correct near-weekly trades still lost to costs. A\n    # defined-risk (capped-loss) intraday structure can carry a larger per-\n    # trade budget than naked selling; 1.2% per trade / 4.0% daily lets it\n    # size to a cost-viable 2+ lots while keeping the daily stop intact.\n    #\n    # ── v11 (patch_v11): 2.0% per trade / 8.0% daily ─────────────────────\n    # THIS IS LEVERAGE, NOT ALPHA. Nothing about the edge changes - the same\n    # five setups on the same four sessions, at a larger ticket. It is here\n    # because the book was committing only 1.2-3.3% of capital per trade\n    # against a 1.0 lakh account while its realised risk ran far below its\n    # structural risk, and the operator asked for CAGR specifically.\n    #\n    # Measured on 2026-09-08..11 replayed in one process (v10 exit ladder\n    # applied), against the 1.2% / 4.0% baseline:\n    #\n    #   per-trade / daily    4-day net    worst single-trade    worst observed\n    #                                          structural loss   unrealised P&L\n    #   1.2% /  4.0%          +12,494.66     32,925 (3.29%)        -3,795\n    #   2.0% /  8.0%  <- here +18,596.09     41,321 (4.13%)        -4,744\n    #   3.0% / 10.0%          +21,774.72     54,628 (5.46%)        -4,744\n    #\n    # Profit scales SUB-linearly (1.67x the budget buys 1.49x the profit)\n    # because LOT_CAPS_BY_DAY binds first - Friday caps at 5 lots - while the\n    # tail scales linearly. The daily stop is raised to 8.0% alongside it\n    # because the clamp below requires per-trade < daily/3, and because a\n    # 2.0% per-trade budget inside a 4.0% daily stop would halt the book\n    # after two losers. Three consecutive structural losses at 2.0% still fit\n    # inside the 8.0% stop, which is the arithmetic the clamp protects.\n    #\n    # Revert with MAX_RISK_PER_TRADE_PCT=0.012 and MAX_DAILY_LOSS_PCT=0.04 in\n    # env.txt - env.txt overrides these defaults, no code change needed. For\n    # the more aggressive row use 0.030 / 0.10 the same way.\n    max_daily_loss_pct = _get_float(env, "MAX_DAILY_LOSS_PCT", 0.08)\n    max_risk_per_trade_pct = _get_float(env, "MAX_RISK_PER_TRADE_PCT", 0.020)\n\n    # Clamp: max risk per trade must be < max daily loss / 3\n    safe_max = round(max_daily_loss_pct / 3.0 - 0.001, 4)\n    if max_risk_per_trade_pct >= max_daily_loss_pct / 3.0:\n        print(\n            f"[WARNING] MAX_RISK_PER_TRADE_PCT clamped to {safe_max} "\n',
    ),

]


def root(argv):
    """The checkout to patch: argv[1], or the directory this file lives in."""
    for a in argv[1:]:
        if not a.startswith("-"):
            return os.path.abspath(a)
    return os.path.dirname(os.path.abspath(__file__))


def apply_hunks(text, hunks):
    """Return (new_text, status, applied, skipped) or (None, reason, 0, 0).

    A hunk whose replacement is already present is skipped, which is what
    makes re-running safe. An anchor that is absent or ambiguous aborts the
    whole file rather than guessing.
    """
    original = text
    applied = skipped = 0
    for _rel, olds, new in hunks:
        if new in text:
            skipped += 1
            continue
        unique = [o for o in olds if text.count(o) == 1]
        if not unique:
            return None, (
                f"anchor not unique (hits={[text.count(o) for o in olds]}); "
                f"first line: {olds[0].splitlines()[0][:70] if olds else '?'}"
            ), 0, 0
        text = text.replace(unique[0], new, 1)
        applied += 1
    if text == original:
        return text, "unchanged", applied, skipped
    return text, "ok", applied, skipped


def write_atomic(path, text):
    """Replace a file in one step, so a crash cannot leave it half-written."""
    d = os.path.dirname(path) or "."
    mode = 0o644
    if os.path.exists(path):
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".patch_v11_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv):
    base = root(argv)
    if not os.path.isfile(os.path.join(base, "core.py")):
        print(f"ABORT: {base} does not look like the algo-trading checkout")
        print("usage: python3 patch_v11.py [/path/to/repo]")
        return 2

    # ── prerequisite: patch_v10 must be applied ───────────────────────────
    # The replay numbers in the docstring were measured with the v10 exit
    # ladder. Applying v11 to a v9 tree would change position size AND leave
    # the 0DTE de-risk rung mis-calibrated, which is a combination nobody
    # measured.
    for rel, want in PREREQ.items():
        p = os.path.join(base, rel)
        if not os.path.isfile(p):
            print(f"ABORT: {rel} is missing - nothing was modified")
            return 3
        with open(p, "r", encoding="utf-8") as fh:
            got = hashlib.md5(fh.read().encode()).hexdigest()
        if got != want:
            print(f"ABORT: {rel} is not at the patch_v10 state "
                  f"(md5 {got[:12]}, expected {want[:12]}).")
            print("       Run patch_v10.py first - the risk budget in this")
            print("       patch was measured against the v10 exit ladder,")
            print("       and that combination is the only one validated.")
            print("       Nothing was modified.")
            return 3

    every = list(TARGETS) + list(CREATED)
    notes, staged = [], {}
    for rel in every:
        table = MD5.get(rel)
        path = os.path.join(base, rel)
        if not os.path.isfile(path):
            if rel in CREATED:
                staged[rel] = ("", "created", 0, 0)
                continue
            print(f"ABORT: {rel} is missing from {base} - nothing was modified")
            return 3
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        digest = hashlib.md5(text.encode()).hexdigest()
        if table and digest == table["v11"]:
            staged[rel] = (text, "current", 0, len(PLAN_V11))
            continue
        if table and digest != table["pre"]:
            print(f"ABORT: {rel} is neither the expected pre-v11 state nor the "
                  f"v11 state (md5 {digest[:12]}).")
            print(f"       expected pre-v11 {table['pre'][:12]}")
            print("       The file has drifted - nothing was modified.")
            return 3
        plan = [h for h in PLAN_V11 if h[0] == rel]
        out, status, applied, skipped = apply_hunks(text, plan)
        if out is None:
            print(f"ABORT: {rel}: {status} - nothing was modified")
            return 3
        got = hashlib.md5(out.encode()).hexdigest()
        if table and got != table["v11"]:
            print(f"ABORT: {rel} did not land on the tested v11 content")
            print(f"       got {got[:12]} expected {table['v11'][:12]} "
                  f"- nothing was modified")
            return 3
        staged[rel] = (out, status, applied, skipped)

    print(f"patch_v11: patching {base}")
    for rel in every:
        _text, status, applied, skipped = staged[rel]
        print(f"  {rel:22s} {status:9s} applied={applied} already_present={skipped}")
    for n in notes:
        print(f"  note: {n}")

    backups, written = {}, []
    try:
        for rel, (text, status, _a, _s) in staged.items():
            path = os.path.join(base, rel)
            backups[rel] = None
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    backups[rel] = fh.read()
            if status not in ("unchanged", "current"):
                write_atomic(path, text)
                written.append(rel)
        for rel in every:
            py_compile.compile(os.path.join(base, rel), doraise=True)
    except Exception as exc:
        print(f"ABORT: {exc.__class__.__name__}: {exc} - reverting")
        for rel, text in backups.items():
            path = os.path.join(base, rel)
            if text is None:
                if os.path.exists(path):
                    os.unlink(path)
            else:
                write_atomic(path, text)
        return 4

    if written:
        print(f"patch_v11 applied: {len(written)} file(s) written "
              f"({', '.join(written)})")
    else:
        print("patch_v11 applied: nothing to do - the tree is already at v11")
    print()
    for c in CHANGES:
        print(f"  + {c}")
    print()
    print("verify:  python3 verify_all.py              # 8/8 module self-tests")
    print("         python3 backtest_engine.py --test   # harness self-test")
    print()
    print("replay:  python3 backtest_engine.py --db data/nifty_algo_v3.db "
          "--from 2026-09-08 --to 2026-09-11 --csv blotter.csv")
    print("         # expected: 09-08 +2,544.06 / 09-09 +3,908.73 /")
    print("         #           09-10 +3,048.53 / 09-11 +9,094.77 = +18,596.09")
    print()
    print("revert:  MAX_RISK_PER_TRADE_PCT=0.012 and MAX_DAILY_LOSS_PCT=0.04")
    print("         in env.txt - env.txt overrides these defaults.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))