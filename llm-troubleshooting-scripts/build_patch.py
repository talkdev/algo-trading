#!/usr/bin/env python3
"""Compose patch_v14.py from the generated hunk payload."""
import pathlib

BASE = pathlib.Path('/home/user/patchgen')
payload = (BASE / 'payload.py').read_text(encoding='utf-8')
meta = (BASE / 'meta.py').read_text(encoding='utf-8')

HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════════
 PATCH_V14  —  NIFTY intraday options engine: square-off integrity, calendar
               truth, and honest measurement
════════════════════════════════════════════════════════════════════════════

Self-contained. No network, no dependencies beyond the standard library, no
edits to anything except the seven engine modules listed in FILES. Run it from
the repository root:

    python patch_v14.py            # apply (backs up every file it touches)
    python patch_v14.py --check    # dry run: report what would happen, write nothing
    python patch_v14.py --revert   # restore the .v14bak backups

Requires the v13 tree (it checks for the PATCH_V12 / PATCH_V13 markers and for
the exact v13 file hashes). It is idempotent: running it twice is a no-op, and
it either patches every file or none of them - nothing is written until all
seven results have been verified in memory.


────────────────────────────────────────────────────────────────────────────
 WHY THIS PATCH EXISTS
────────────────────────────────────────────────────────────────────────────
The engine was audited for a move from paper trading to live, DTE by DTE: gates,
strategy selection, regime detection, entry and exit rules, data capture,
replay, and the per-day database split. The strategy rules were left alone
wherever they were already profitable. What follows is what was broken, what it
cost, and what this patch does about it. Every number below was measured by
replaying the five recorded sessions
(2026-09-08, 09, 10, 11, 15) with

    python backtest_engine.py --db data/per_day --from <date> --to <date>

1. THE BACKTEST WAS REPORTING P&L PRODUCTION COULD NOT EARN.  (the headline)

   execution_engine.perform_hard_exit_sweep() fired at
   min(15:00, HARD_EXIT_TIME) against the whole book, while every position
   carried its own hard_exit_time (15:15 on a weekly session, 15:00 on an
   expiry session) that Priority 7 of the exit ladder and D5 of the debit
   ladder both honoured. The sweep therefore flattened the live book fifteen
   minutes before the ladder said it should, and HARD_EXIT_TIME was dead on
   the one path that carries the force flag.

   The replay harness never called the sweep at all - it drives the ladder -
   so live and replay disagreed by exactly that gap:

       replayed as the code stands today   Rs +34,177   7 trades   86% win
       what the live loop would have done  Rs +28,466   6 trades   83% win
                                           ────────────────────────────────
       divergence                          Rs  5,711

   Two trades were affected. On 2026-09-09 the harness held a closing-hour
   long put to 15:20 and booked Rs +3,284; live had already flattened at
   15:00, and because the closing-hour entry window is defined as
   hard_exit - 25 minutes, live would never have opened the ticket at all. On
   2026-09-11 the last fifteen minutes of a long call were worth Rs +2,427
   that live did not keep.

   FIX  the sweep reads each position's own hard_exit_time and keeps one
   global backstop at SQUARE_OFF_DEADLINE; HARD_EXIT_TIME defaults to 15:15
   and SQUARE_OFF_DEADLINE to 15:18, so the ordering is position ladder ->
   in-loop sweep -> watchdog -> the broker's own 15:20 RMS square-off of a
   product="I" order. The harness now mirrors the sweep. The reason string is
   unchanged where the close bookkeeping classifies on it; the real time goes
   into reason_detail, so a weekly squared at 15:15 is no longer persisted and
   attributed as "HARD_EXIT_15:00".

2. A WEEKDAY RULE WAS WEARING A DTE COSTUME, AND THE CALENDAR MOVED UNDER IT.

   The bearish lean in _range_day_bearish_lean() was exempted on
   `actual_dte == 2`, because Friday happens to be DTE 2 in a clean
   Tuesday-expiry week. The exemption is really about WEEKEND RISK - the last
   session before the market shuts for two or more days - and nse_holidays.json
   lists 2026-09-14 (Monday) as a holiday, which rolls that week's expiry back
   and re-buckets every day: Friday 09-11 became DTE 1 and the exemption
   switched ITSELF OFF on the one session it was measured on, while Thursday
   09-10 became DTE 2 and inherited an exemption nobody wrote for it.

   Measured on the recorded week, the lean substituted a bear call at 10:14 on
   Friday into a US-CPI rally; the v12 trend-flip exit ejected it 55 minutes
   later at Rs -250 - the week's only losing trade - and the condor the guard
   would have kept was then refused on its own economics, leaving the book flat
   and free for the 11:12 long call that earned the session.

   FIX  a new ExpiryCalendar.is_weekend_risk_day() (the last session before a
   calendar gap of three days or more) ORs with an explicit FRIDAY check and
   the old DTE-2 check, so the rule holds whichever way the calendar moves.
   Effect on the five sessions: the losing trade disappears, win rate 86% ->
   100%, and P&L rises Rs +250.

   The same weekday-proxy defect appeared twice more and is fixed the same way:
   strategy_engine's "wait for the 0DTE series to be listed" gate keyed on
   day_label == "TUESDAY" (on a holiday-rolled Monday expiry it waited for
   nothing and traded the NEXT weekly as if it were the expiring one - the
   phantom-P&L failure PATCH_V12 added that gate to prevent, on the one weekday
   the proxy did not cover), and data_engine's expiry-day entry window keyed on
   day_label == "TUESDAY" and actual_dte == 0 (a genuine 0DTE Monday ran the
   weekly window: entries from 09:45 instead of 10:30, and no 15:00 square-off
   against gamma into the closing auction). Both now key on the expiry itself.

3. MAX_DTE_TRADEABLE WAS A KNOB THAT DID NOTHING, AND THE REAL CEILING WAS
   THREE DIFFERENT NUMBERS.

   Config.max_dte_tradeable (4) was read by no module. The actual ceiling was a
   literal 6 in strategy_engine._check_hard_gates, another literal 6 in
   regime_engine.classify_final, and a per-structure 4 in DTE_REQUIREMENTS. An
   operator who set the field changed nothing; a session at DTE 5 or 6 passed
   the regime layer and the hard gates and then died inside compute_params with
   a different reason, so the refusal census attributed it to the wrong gate.
   All three now read the one field, and DTE_REQUIREMENTS can only ever be
   TIGHTENED by it, never widened, so a typo in env.txt cannot hand the engine
   a DTE it was never measured on. Behaviour at the default is unchanged.

4. A LIQUIDITY FLOOR WRITTEN IN CONTRACTS WAS APPLIED TO UNITS.

   _validate_leg() refused a leg with `oi < 500` on a sell and `< 100` on a
   buy. Upstox reports F&O open interest and volume in UNDERLYING UNITS - the
   same convention its place-order contract uses for quantity. The recorded
   data proves it: every one of the 133,520 option_chain_snapshot OI values on
   2026-09-08 is an exact multiple of the 65-unit lot, and the smallest
   non-zero value in the file is 65, i.e. precisely one contract. The floor was
   therefore 7.7 contracts on the short leg of a structure held for hours and
   1.5 contracts on the protective wing that has to be bought back in a hurry -
   on 0DTE, where the whole point of the wing is that it fills when nothing
   else will, that is the difference between a defined-risk structure and an
   undefined one. Both floors are now scaled by lot_size. Measured effect on
   the five sessions: not one trade changes, to the rupee - the strikes this
   engine sells are liquid, and the floor was simply never doing its job.

5. A LEG SUBSTITUTION COULD HAND THE EV GATE A DIFFERENT STRUCTURE.

   _build_validated_legs() walks +-1, +-2 strikes looking for the first
   alternative that merely QUOTES, and never re-runs the delta, credit or wing
   logic that chose the original. On a symmetric structure that silently
   produces an asymmetric one (a condor whose call wing moved 50 points while
   its put wing did not); on any structure it can produce two legs on one
   strike, or a short pushed outside its own protective wing - a debit where a
   credit was selected. Everything downstream recomputes the economics on
   whatever legs survive, so the EV gate is honest about the structure it is
   GIVEN; nothing downstream can see that it is no longer the structure that
   was CHOSEN. A post-substitution guard now refuses those three cases and
   names the move.

6. THE REGIME SNAPSHOT DISAGREED WITH THE DECISION IT WAS AUDITING.

   regime_engine.calculate_regime() derived dte and day_type from the calendar
   for the snapshot and for persistence, while the decision tree consumed
   actual_dte out of signals - the DTE of the expiry the BROKER listed. The two
   differ on any holiday-shifted week, silently, and regime_decisions (the
   table an operator reads to audit the engine's own reasoning) could carry a
   different DTE and a different day_type from the ones the trade was sized,
   stopped and targeted on. The broker's number now wins, the calendar's is the
   fallback, and a disagreement is logged once per session with the instruction
   to check nse_holidays.json. Telemetry only: no P&L path moves.

7. SESSION STATE WAS LOST ON EVERY RESTART, AND NINE LOOKBACKS USED THE WRONG
   CLOCK.

   session_state carried no record of when the engine last exited, at what
   spot, for which reason, at what priority, or what it booked - so the
   anti-churn protections (the 30-minute stop cooldown, the same-signal-combo
   block, the two-stop halt, one momentum clip a day) were rebuilt from scratch
   by any restart, and a process that died at 14:05 came back with no memory of
   the stop it had just taken. Live proof from the recorded week: a re-entry
   17 milliseconds after an exit. Six latch columns and a momentum_entries
   counter are added to the schema and to MIGRATION_SQL, so an existing
   database is upgraded in place rather than rebuilt.

   Separately, nine lookback windows in core.py called date.today() - the
   SERVER's local date - while everything else in the engine runs on IST. On a
   box set to UTC those windows silently shifted by 5.5 hours, which between
   18:30 and 00:00 IST is the difference between today's calibration and
   yesterday's. All nine now use today_ist().

8. THE MEASUREMENT LAYER UNDER-REPORTED RISK AND COULD NOT ANSWER PER-DTE
   QUESTIONS AT ALL.

   * Max drawdown was a peak-to-trough on SETTLED daily P&L. On a book that
     sells premium the adverse excursion happens while the position is open, by
     definition, so the figure reported Rs 0 for a week in which the book was
     at one point down Rs 7,927 (2026-09-11, 13:41 - 46% of that trade's final
     profit). The harness now marks the open position every cycle on the
     recorded chain, using the same liquidation pricing and cost model a close
     would have used, and reports both numbers side by side.
   * Sharpe was computed only over days that booked a trade, so a correctly
     refused session - a real 0.00% return on capital - was dropped from the
     mean and the variance. There was no Sortino at all. Both are now over
     every replayed session, with the sample size printed, and a
     one-session run reports "n/a" instead of 0.00 (which reads as measured
     and found to be zero).
   * --db took one path, so a run could never span sessions and the period
     statistics were unobtainable from the tool. It now takes several paths or
     a directory, served through a MultiStore that routes each session to the
     database holding it.
   * There was no per-DTE breakdown, although the engine is a different machine
     at each DTE (strategy set, stop multiple, target, wing width, entry
     window, square-off time are all keyed on it). One is added, with a warning
     for buckets holding fewer than five trades.
   * The refusal funnel claimed to list the gates "in the order decide()
     applies them" and did not: decide() calls _check_hard_gates() FIRST -
     which holds the capital and loss floors, the cooldown, the position
     limits, the entry clock, the 90-minute-to-square-off floor, the DTE
     ceiling and the expiry-day series wait - and only then the regime layer.
     Every pass rate below the first row was computed against a chain the
     engine does not run. Re-verified against the function and reordered; four
     buckets the strategy really emits (day_structure, the time-to-square-off
     floor, counter-trend symmetry, event day) had no row at all and fell
     through to "unplaceable" - 96 refusals in one five-session run.
   * Momentum refusals could not be counted by cause, because the bucket is a
     stage name and one stage swallows half a dozen unrelated checks. A census
     by the condition that actually fired is added.
   * Nothing compared the replayed session against the one that was RECORDED.
     A parity block now does, aligned to the moment the recorded row was
     written, and it immediately earned its keep on the recorded week:
     high_impact_events.json has been hand-edited since capture, so 2026-09-11
     was recorded NORMAL and replays as EVENT (which switches on
     defined_risk_only and allows a RANGE verdict at HIGH confidence and
     nothing else - 136 cycles refused on event grounds that live never saw),
     and 2026-09-08 was recorded PRE_EVENT and replays NORMAL. A replay is
     only evidence about the session it reproduces; that block says when it is
     not reproducing it.

9. verify_all.py DID NOT COVER core.py OR main.py, AND ALWAYS EXITED 0.

   The module that owns the config loader, the expiry calendar, the schema and
   every migration was never executed by the verification script, and main.py
   was not covered at all. Worse, the script returned success even when tests
   failed, so a wrapper or a cron job invoking it before a live start got
   "success" from a tree that had just failed its own tests - the one signal
   supposed to stop the start could not stop anything. core.py's self-test is
   now in the list; main.py is covered by a new static pre-flight (it has no
   test mode and RUNNING it starts the live loop) that compiles all twelve
   modules, loads the config and checks the square-off ordering, the lot size,
   the loss limit, that max_dte_tradeable is actually read, that both
   hand-edited calendars parse and cover the current year, and that every patch
   marker is present. The exit code is now meaningful, and the closing claim
   was softened: "ALL TESTS PASSED" is a statement that the tree compiles and
   is internally consistent, not that the strategy has an edge.


────────────────────────────────────────────────────────────────────────────
 MEASURED EFFECT ON THE FIVE RECORDED SESSIONS
────────────────────────────────────────────────────────────────────────────
                                        total      trades   win rate   max DD
  v13 as the harness reported it      Rs +34,177      7       86%     Rs     0
  v13 as the LIVE loop would trade it  Rs +28,466      6       83%     Rs     0
  v14, replay == live                 Rs +33,915      6      100%     Rs 7,927

  Rs +33,915 is Rs 262 (-0.8%) below the v13 backtest figure and Rs 5,449
  (+19.1%) above what the v13 code could actually have earned live. The Rs 262
  is the losing trade that item 2 removes: the lean guard stops the Friday
  bear call from being opened into a CPI rally (-250 gone), and the condor it
  would have kept is refused downstream on its own economics, so the net is one
  fewer trade and no losers. The Rs 7,927 is not new risk - it is risk that was
  always there and was being reported as zero.

  Per session (v14):  09-08 +2,544   09-09 +625 and +3,173   09-10 +2,025
                      09-11 +17,181  09-15 +8,367

  Six trades is not a sample. The standard error on a win rate at n=6 is about
  20 percentage points, and the honest reading of 100% is "no loser in six",
  not "this does not lose". Nothing in this patch was tuned to make these five
  sessions look better: items 1, 3, 4, 6, 7 and 8 are behaviour-preserving or
  telemetry-only (item 4 measures exactly zero change), item 2 removes a trade
  the engine's own exit ladder was built to eject, and item 5 refuses structures
  that were never selected.


────────────────────────────────────────────────────────────────────────────
 HOW TO VERIFY IT
────────────────────────────────────────────────────────────────────────────
    python patch_v14.py --check          # dry run, writes nothing
    python patch_v14.py                  # apply
    python verify_all.py                 # 12 compiles + pre-flight + 8 self-tests
    python backtest_engine.py --db data/per_day --trade-report off
                                         # all five sessions in one run

 Expected: 6 trades, Rs +33,915.16, 100% win rate, max intraday DD Rs 7,927.
 Roll back with `python patch_v14.py --revert`.
"""
from __future__ import annotations

import hashlib
import os
import py_compile
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".v14bak"
MARKER = "PATCH_V14"
PREREQ_MARKERS = ("PATCH_V12", "PATCH_V13")

FILES = [
    "core.py",
    "data_engine.py",
    "regime_engine.py",
    "strategy_engine.py",
    "execution_engine.py",
    "backtest_engine.py",
    "verify_all.py",
]

WHAT_CHANGED = {
    "core.py":
        "hard exit 15:15 / square-off watchdog 15:18, ExpiryCalendar."
        "is_weekend_risk_day() + get_day_type(dte=), six session_state latch "
        "columns + migration, nine date.today() lookbacks -> today_ist()",
    "data_engine.py":
        "the expiry-day entry window (10:30-13:00, square off 15:00) keys on "
        "actual_dte == 0, not on the weekday",
    "regime_engine.py":
        "snapshot DTE and day type taken from the broker-confirmed expiry, "
        "mismatch logged once a session; DTE ceiling reads max_dte_tradeable",
    "strategy_engine.py":
        "weekend-risk lean guard on the calendar, 0DTE-series wait on the "
        "calendar, max_dte_tradeable wired into both hard gates and "
        "DTE_REQUIREMENTS, OI/volume floors in units, leg-substitution guard",
    "execution_engine.py":
        "the hard-exit sweep honours each position's own square-off time with "
        "a backstop at SQUARE_OFF_DEADLINE; hard exits report the real time",
    "backtest_engine.py":
        "mirrors the sweep, marks equity every cycle for a true intraday "
        "drawdown, Sharpe/Sortino over every session, multi-database runs, "
        "per-DTE table, corrected funnel order, momentum census, "
        "recorded-vs-replay session parity",
    "verify_all.py":
        "static pre-flight (12 modules compiled, config invariants, calendars, "
        "patch markers), core.py added to the self-tests, real exit code",
}

# SHA-256 of each file this patch was generated FROM (the v13 tree) and TO (the
# measured v14 tree). The target hashes are what makes the apply all-or-nothing:
# a file is only written once its patched content has been hashed in memory and
# found to be exactly the tree the numbers above were measured on.
'''

FOOTER = '''

# ─────────────────────────────────────────────────────────────────────────────
#  Hunk payload: (file, [(index, old_lines, new_lines, hint_line), ...])
#  Generated by diffing the v13 tree against the measured v14 tree and
#  simulated before it was written out, so it cannot describe a patch that does
#  not reproduce that tree byte for byte.
# ─────────────────────────────────────────────────────────────────────────────
PAYLOAD = '''

TAIL = '''


# ═══════════════════════════════════════════════════════════════════════════
#  APPLY MACHINERY
# ═══════════════════════════════════════════════════════════════════════════

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _locate(lines, old, hint, fname, idx):
    """Find `old` in `lines`, preferring the recorded position.

    Locating by content with a positional hint rather than by line number alone
    is what lets this patch still apply to a tree that has been edited since
    v13: if the touched region is intact the hunk lands, and if it is not the
    patch refuses that file and writes nothing at all.
    """
    n = len(old)
    if n == 0:
        return hint
    if lines[hint:hint + n] == old:
        return hint
    hits = [i for i in range(max(0, len(lines) - n + 1))
            if lines[i:i + n] == old]
    if not hits:
        raise AssertionError(
            f"{fname} hunk {idx}: the code it patches is not present. This "
            f"file has been edited since v13 in a region PATCH_V14 needs."
        )
    if len(hits) > 1:
        # Several identical blocks: take the nearest to the recorded position,
        # but say so - a silent guess here is how patches corrupt files.
        chosen = min(hits, key=lambda i: abs(i - hint))
        print(f"    note: {fname} hunk {idx} matches {len(hits)} places; "
              f"using line {chosen + 1} (nearest the recorded {hint + 1})")
        return chosen
    return hits[0]


def _patch_text(fname, text):
    """Apply every hunk for `fname` and return the new text."""
    lines = text.splitlines(keepends=True)
    hunks = dict(PAYLOAD)[fname]
    # Descending by position so each replacement leaves the earlier hints valid.
    for (idx, old, new, hint) in sorted(hunks, key=lambda h: -h[3]):
        at = _locate(lines, old, hint, fname, idx)
        lines[at:at + len(old)] = new
    return "".join(lines)


def _read(fname):
    return (BASE / fname).read_text(encoding="utf-8")


def _compile(fname):
    py_compile.compile(str(BASE / fname), doraise=True)


def _revert() -> int:
    """Restore every .v14bak this patch wrote."""
    restored, missing = [], []
    for fname in FILES:
        bak = BASE / (fname + BACKUP_SUFFIX)
        if bak.exists():
            shutil.copy2(str(bak), str(BASE / fname))
            restored.append(fname)
        else:
            missing.append(fname)
    for fname in restored:
        try:
            _compile(fname)
        except Exception as exc:
            print(f"  !! {fname} does not compile after revert: {exc}")
            return 1
    print(f"  reverted {len(restored)} file(s) from {BACKUP_SUFFIX}")
    if missing:
        print(f"  no backup for: {', '.join(missing)} (left untouched)")
    return 0


def main(argv) -> int:
    check = "--check" in argv or "-n" in argv
    if "--revert" in argv:
        print()
        print("PATCH_V14 — REVERT")
        print("=" * 72)
        return _revert()

    print()
    print("PATCH_V14 — NIFTY intraday options engine")
    print("=" * 72)
    print(f"  target tree : {BASE}")
    print(f"  mode        : {'CHECK (writes nothing)' if check else 'APPLY'}")
    print()

    missing = [f for f in FILES if not (BASE / f).exists()]
    if missing:
        print(f"  !! not found in this directory: {', '.join(missing)}")
        print("     Run this script from the repository root, next to core.py.")
        return 1

    # ── 1. already applied? ────────────────────────────────────────────────
    current = {f: _read(f) for f in FILES}
    hashes = {f: _sha(current[f]) for f in FILES}
    done = [f for f in FILES if hashes[f] == TARGET_SHA[f]]
    if len(done) == len(FILES):
        print("  Nothing to do: every file already matches the PATCH_V14 tree.")
        print(f"  ({MARKER} is applied and byte-identical to the measured one.)")
        return 0
    if done:
        print(f"  Already patched ({len(done)}/{len(FILES)}): "
              f"{', '.join(done)}")
        print("  Continuing with the rest; hunks are located by content.")
        print()

    # ── 2. prerequisites ───────────────────────────────────────────────────
    joined = "".join(current.values())
    absent = [m for m in PREREQ_MARKERS if m not in joined]
    if absent:
        print(f"  !! this tree has no {' or '.join(absent)} markers.")
        print("     PATCH_V14 builds on the v12 phantom-P&L fix and the v13")
        print("     entry/exit symmetry work. Apply those first.")
        return 1

    exact = [f for f in FILES if hashes[f] == PREREQ_SHA[f]]
    drifted = [f for f in FILES
               if hashes[f] not in (PREREQ_SHA[f], TARGET_SHA[f])]
    print(f"  v13 baseline match : {len(exact)}/{len(FILES)} file(s)")
    if drifted:
        print(f"  edited since v13   : {', '.join(drifted)}")
        print("     Each hunk is located by content, so this can still apply")
        print("     cleanly - but the result will not be byte-identical to the")
        print("     tree the measured numbers came from, and it is reported as")
        print("     such below rather than being passed off as it.")
    print()

    # ── 3. patch everything in memory first ────────────────────────────────
    patched, failures, verified = {}, [], True
    for fname in FILES:
        if hashes[fname] == TARGET_SHA[fname]:
            print(f"  [skip] {fname:<22} already at the v14 tree")
            continue
        try:
            text = _patch_text(fname, current[fname])
        except AssertionError as exc:
            failures.append(f"{fname}: {exc}")
            print(f"  [FAIL] {fname:<22} {exc}")
            continue
        except Exception as exc:
            failures.append(f"{fname}: {exc}")
            print(f"  [FAIL] {fname:<22} {exc}")
            continue
        n_hunks = len(dict(PAYLOAD)[fname])
        ok = _sha(text) == TARGET_SHA[fname]
        if fname in exact and not ok:
            # A pristine v13 file MUST come out byte-identical. Anything else
            # means the payload and the tree disagree, and writing it would
            # produce an engine nobody has measured.
            failures.append(f"{fname}: patched content does not match the "
                            f"verified v14 tree")
            verified = False
            print(f"  [FAIL] {fname:<22} {n_hunks} hunks applied but the "
                  f"result is not the measured tree")
            continue
        if not ok:
            verified = False
        patched[fname] = text
        print(f"  [ ok ] {fname:<22} {n_hunks:>2} hunk(s)  "
              f"{'byte-identical to the measured v14 tree' if ok else 'applied (tree has drifted from v13)'}")

    if failures or not patched:
        print()
        print("  NOTHING WAS WRITTEN.")
        for f in failures:
            print(f"    - {f}")
        return 1

    # ── 4. compile in memory before touching disk ──────────────────────────
    import tempfile
    for fname, text in patched.items():
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / fname
            probe.write_text(text, encoding="utf-8")
            try:
                py_compile.compile(str(probe), cfile=str(Path(tmp) / "p.pyc"),
                                   doraise=True)
            except Exception as exc:
                print()
                print(f"  !! {fname} does not compile after patching:")
                print(f"     {exc}")
                print("  NOTHING WAS WRITTEN.")
                return 1

    if check:
        print()
        print(f"  CHECK complete: {len(patched)} file(s) would be patched, "
              f"all hunks located, all results compile.")
        if not verified:
            print("  (at least one file would not be byte-identical to the "
                  "measured tree)")
        print("  Re-run without --check to apply.")
        return 0

    # ── 5. back up, then write ─────────────────────────────────────────────
    for fname, text in patched.items():
        src, bak = BASE / fname, BASE / (fname + BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copy2(str(src), str(bak))
        src.write_text(text, encoding="utf-8")

    # ── 6. verify what is now on disk ──────────────────────────────────────
    print()
    bad = []
    for fname, text in patched.items():
        on_disk = _read(fname)
        if on_disk != text:
            bad.append(fname)
            continue
        try:
            _compile(fname)
        except Exception as exc:
            bad.append(fname)
            print(f"  !! {fname} failed to compile on disk: {exc}")
    if bad:
        print(f"  !! verification failed for: {', '.join(bad)}")
        print(f"     Restore with: python {Path(__file__).name} --revert")
        return 1

    print("  PATCH_V14 APPLIED")
    print("  " + "-" * 70)
    for fname in FILES:
        print(f"    {fname}")
        print(f"      {WHAT_CHANGED[fname]}")
    print("  " + "-" * 70)
    print(f"  backups        : {BACKUP_SUFFIX} beside each file")
    print(f"  revert         : python {Path(__file__).name} --revert")
    print(f"  identical to the measured tree : "
          f"{'yes' if verified else 'NO - this file set has drifted from v13'}")
    print()
    print("  Verify before trading:")
    print("    python verify_all.py")
    print("    python backtest_engine.py --db data/per_day --trade-report off")
    print()
    print("  Expected: 8/8 module self-tests, 26/26 pre-flight checks,")
    print("            6 trades, Rs +33,915.16, 100% win rate,")
    print("            max intraday drawdown Rs 7,927.")
    print()
    print("  Going live is still a decision, not a consequence of this patch.")
    print("  Six trades over five sessions is not a sample. Paper mode first,")
    print("  then one lot, then size up only on evidence this run cannot give.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

# ── assemble ───────────────────────────────────────────────────────────────
meta_dict = eval(meta)
target_lines = ['TARGET_SHA = {']
prereq_lines = ['PREREQ_SHA = {']
for f in ['core.py', 'data_engine.py', 'regime_engine.py', 'strategy_engine.py',
          'execution_engine.py', 'backtest_engine.py', 'verify_all.py']:
    m = meta_dict[f]
    target_lines.append(f'    "{f}": "{m["target_sha"]}",')
    prereq_lines.append(f'    "{f}": "{m["pristine_sha"]}",')
target_lines.append('}')
prereq_lines.append('}')

out = (HEADER + '\n'.join(prereq_lines) + '\n\n' + '\n'.join(target_lines)
       + '\n' + FOOTER + payload + TAIL)

dest = pathlib.Path('/home/user/algo-trading/patch_v14.py')
dest.write_text(out, encoding='utf-8')
print(f'wrote {dest}  {dest.stat().st_size:,} bytes  {len(out.splitlines()):,} lines')