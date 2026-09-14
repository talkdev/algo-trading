#!/usr/bin/env python3
"""v9: FINAL PER-DAY TRADE REPORT at the end of every backtest_engine run.

    python3 patch_v9.py                 # applies to the directory it lives in
    python3 patch_v9.py /path/to/repo   # or to an explicit checkout

Expects the tree patch_v8.py leaves behind (its MD5 table is the gate) and
lands it on the tested v9 content. All-or-nothing: the hunk is staged in
memory, the result is hash-checked and compiled, and the file is reverted
if anything fails. Re-running is a no-op.

Exit codes: 0 applied or already up to date, 2 wrong directory,
3 unrecognised content / anchor drift (nothing modified),
4 compile failure (reverted).

File: backtest_engine.py is patched in place.
No engine decision changes - the four recorded replay days come out at
exactly the v8 numbers (see REPLAY PROOF below).

------------------------------------------------------------------------------
WHAT IT ADDS: the per-day trade summary is now the LAST thing it prints
------------------------------------------------------------------------------
backtest_engine.py already renders the exact per-trade lifecycle block the
live engine prints (core.TradeConsoleReporter: entry legs, exit or
liquidation legs, committed capital, realised/unrealised P&L) - v7 added
that, and it prints it on EVERY replayed cycle. For a single day that is
roughly 560 blocks; for a four-day run the console scrolls past one hundred
thousand lines and each day's FINAL state - the one with the realised P&L
and the exit fills - is buried in the middle of them, thousands of lines
above the statistics at the bottom of the terminal.

This patch makes backtest_engine.py close every run with one new section,
AFTER the aggregate statistics and the csv note:

  ==============================================================================
  FINAL PER-DAY TRADE REPORT - N replayed sessions, each in its end-of-day state
  ==============================================================================
  ---- TRADE REPORT [BACKTEST] | 2026-09-08 15:30:54 | 1 trade(s), 0 open | mode=each_cycle ----
  ====================================================================================
  Trade-1
  Strategy: BEAR_CALL_SPREAD
  ----------------------------------------------------------------------------
  Trade Start Data: time: 12:03
  Sold: 4 lot of CE with premium: 28.26 at strike: 23700
  Bought: 4 lot of CE with premium: 9.19 at strike: 23800
  ----------------------------------------------------------------------------
  Trade End Data: time: 13:32
  Bought: 4 lot of CE with premium: 20.08 at strike: 23700
  Sold: 4 lot of CE with premium: 5.46 at strike: 23800
  Closed - gamma_window_derisk_1330
  ----------------------------------------------------------------------------
  Position Status: Close
  Total Investment: Rs 33,810.39
      margin blocked Rs 33,748.00 + entry charges Rs 62.39
      premium received Rs 4,958.20 - a credit structure commits margin, not cash premium
  Total Profit: Rs +1,042.33 realised
      gross +4.45 pts x 260 units = Rs 1,157.00, charges Rs 114.67
      entry credit +19.07 pts (fills), exit debit +14.62 pts (fills)
  ====================================================================================

one header per replayed session, in chronological order, and under it every
trade of that session in the state its day ended in - realised P&L and exit
fills for closed trades, the day's OWN liquidation marks for anything still
open. Example closed day above (2026-09-08); an open trade would show

    Position Status: Open
    Total Profit: Rs -116.46 unrealised
        marked to exit +19.05 pts (positions.last_liquidation_premium) -> gross ...

rendered by the same TradeConsoleReporter, so the final block cannot tell a
different story from the per-cycle blocks above it.

------------------------------------------------------------------------------
HOW IT WORKS (three small additions, all reporting, none touching decisions)
------------------------------------------------------------------------------
1. BacktestRunner picks up two book-keeping fields: _day_finals, pins the
   last simulated clock time and the session's last chain at the end of
   every replayed day, and final_report_lines, the captured blocks.
2. run() calls the new _render_final_day_reports(dates) after the last
   session and BEFORE _teardown() removes the scratch book and uninstalls
   the SimClock. It builds a FRESH TradeConsoleReporter per session - empty
   per-position change memory, so every position of the day prints exactly
   once even when the identical final block already scrolled past - and
   captures the text instead of printing it. A rendering failure degrades
   to silence, never to a lost session result.
3. main() prints the captured blocks at the very end of the run.

Honours the existing controls end-to-end: --trade-report=off (or
TRADE_REPORT_ENABLED=false in env.txt) silences this section as well, and
the mode= in each header is the mode the run actually used
(--trade-report=on_change shows mode=on_change, so the header and the run
cannot disagree).

------------------------------------------------------------------------------
REPLAY PROOF: no engine decision moved
------------------------------------------------------------------------------
The four recorded days, replayed on the committed per-day databases merged
into one scratch book (data/per_day/nifty_algo_2026-09-08..11.db), trading
one session per day through --csv, before and after this patch:

    python3 backtest_engine.py --db <merged.db> --from 2026-09-08 \
           --to 2026-09-11 --csv blotter.csv

    v8 blotter.csv == v9 blotter.csv   (byte-identical, diff exit 0)

as it must be: this patch adds a report, not a decision. Day totals under
this checkout's committed config (v8 == v9 for every trade):

    day          trades   net Rs (v8 == v9)
    2026-09-08        1      +1,042.33
    2026-09-09        1      +2,307.48
    2026-09-10        1      +1,791.36
    2026-09-11        2      +6,731.25

python3 backtest_engine.py --test       # harness self-test still passes
"""

import hashlib
import os
import py_compile
import sys
import tempfile

TARGETS = (
    "backtest_engine.py",
)

CREATED = ()

MD5 = {
    "backtest_engine.py": {
        "v8": "454d6f438daa0d20829693c53396dd90",
        "v9": "9d37be5733c29b66d5c3054a20407da4",
    },
}

# Printed after a successful run: what the tree now does that it did not.
CHANGES = (
    "backtest_engine.py: every run ends with FINAL PER-DAY TRADE REPORT - "
    "one TRADE REPORT [BACKTEST] block per replayed session, in order, "
    "each session in its end-of-day state",
    "closed trades show realised P&L from the persisted fills; still-open "
    "legs are marked against that day's OWN last chain, never the next "
    "session's",
    "printed after the aggregate statistics, as the very last output of a "
    "single- or multi-day run",
    "fresh reporter per session, so every trade of the day prints exactly "
    "once and mode= tells the mode the run actually used",
    "--trade-report=off / TRADE_REPORT_ENABLED=false silences it too - a "
    "silenced run stays silenced",
    "a rendering failure degrades to silence, never to a lost replay",
    "no engine decision changed: the four replay days are byte-identical "
    "to v8 in the trade blotter",
)

# (file, [anchor candidates], replacement) - anchored whole-block edits.
PLAN_V9 = [
    (
        "backtest_engine.py",
        [
            '        self.trade_report_mode = trade_report\n        self.reporter = None\n',
        ],
        "        self.trade_report_mode = trade_report\n        self.reporter = None\n        # ── v9: the final per-day trade report ────────────────────────────\n        # The per-cycle blocks are exact but unreadable as a DAY summary:\n        # over a multi-session replay each day's FINAL state is buried under\n        # thousands of scrolling blocks. At the end of every replayed day,\n        # run_day() pins that session's closing clock time and last chain\n        # here, and _render_final_day_reports() turns the book of every\n        # replayed day into one end-of-day block per session, printed by\n        # main() as the very last thing on the console - after the\n        # statistics. Open legs are marked against the day's OWN last chain,\n        # never against the next session's.\n        self._day_finals: Dict[str, Tuple[datetime, dict]] = {}\n        self.final_report_lines: List[str] = []\n",
    ),
    (
        "backtest_engine.py",
        [
            '            t = self._close(live, signals, "END_OF_DATA_FORCED_FLAT", 7, day)\n            self.results.add_trade(t)\n            # v7: the session\'s last trade gets its final block too - the loop\n            # above ended before this close happened.\n            self._report_trades(day)\n\n    # -- driver -----------------------------------------------------------\n    def run(self, dates: List[str]) -> Results:\n        self._build()\n        try:\n            for d in dates:\n                self.run_day(d)\n        finally:\n            self._teardown()\n        return self.results\n',
        ],
        '            t = self._close(live, signals, "END_OF_DATA_FORCED_FLAT", 7, day)\n            self.results.add_trade(t)\n            # v7: the session\'s last trade gets its final block too - the loop\n            # above ended before this close happened.\n            self._report_trades(day)\n\n        # ── v9: pin the session\'s closing state for the end-of-run report ──\n        # The last simulated clock time and the session\'s own last chain: the\n        # final per-day report marks a still-open leg against the same day it\n        # traded, never against the next session\'s (or a missing) chain.\n        self._day_finals[trading_date] = (\n            self.clock.now(), dict(self.me.last_chain or {})\n        )\n\n    # -- v9: final per-day trade report -------------------------------------\n    def _render_final_day_reports(self, dates: List[str]) -> None:\n        """Render each replayed day\'s trade book in its FINAL end-of-day state.\n\n        Called by run() after the last replayed session and BEFORE\n        _teardown() removes the scratch book and uninstalls the SimClock.\n        The blocks are captured as text rather than printed here: main()\n        puts them after the aggregate statistics, so the very last thing on\n        the console is the per-day report the operator asked for - for every\n        session, one header per session, every trade of that session in the\n        state its day ended in (realised P&L and exit fills for closed\n        trades, the day\'s own liquidation marks for anything still open).\n\n        A FRESH reporter per session: its per-position change memory is\n        empty, so every position of the day prints exactly once even when\n        the per-cycle reporter already showed the identical final block, and\n        the mode= in the header is the mode the run actually used. The\n        configured mode is honoured end-to-end: --trade-report=off (or\n        TRADE_REPORT_ENABLED=false) silences this report as well. A\n        rendering failure degrades to silence, never to a lost session\n        result - this is reporting, not trading.\n        """\n        replayed = set(self.results.days)\n        for trading_date in dates:\n            if trading_date not in replayed:\n                continue\n            try:\n                as_of, chain = self._day_finals.get(trading_date) or (None, {})\n                reporter = core.TradeConsoleReporter(\n                    self.db, self.config,\n                    getattr(self.reporter, "logger", None), source="BACKTEST",\n                )\n                reporter.set_mode(self.trade_report_mode)\n                buf = io.StringIO()\n                with contextlib.redirect_stdout(buf):\n                    reporter.report_cycle(\n                        trading_date=trading_date,\n                        chain=chain,\n                        as_of=(as_of or self.clock.now()),\n                    )\n                block = buf.getvalue().strip("\\n")\n                if block:\n                    self.final_report_lines.append(block)\n            except Exception as exc:\n                if self.verbose:\n                    print(f"  final day report failed for {trading_date}: {exc}")\n\n    # -- driver -----------------------------------------------------------\n    def run(self, dates: List[str]) -> Results:\n        self._build()\n        try:\n            for d in dates:\n                self.run_day(d)\n            self._render_final_day_reports(dates)\n        finally:\n            self._teardown()\n        return self.results\n',
    ),
    (
        "backtest_engine.py",
        [
            '    res = runner.run(dates)\n    print_report(res, config, args)\n    if args.csv:\n        write_csv(res, args.csv)\n    return 0\n',
        ],
        '    res = runner.run(dates)\n    print_report(res, config, args)\n    if args.csv:\n        write_csv(res, args.csv)\n\n    # ── v9: the final per-day trade report is the very last output ─────────\n    # One block per replayed session in its end-of-day state, printed after\n    # the aggregate statistics - the operator asked to see the day\'s final\n    # book at the bottom of the terminal, not 13,000 lines up the\n    # scrollback. --trade-report=off (or TRADE_REPORT_ENABLED=false) leaves\n    # this list empty, so a silenced run stays silenced here too.\n    if runner.final_report_lines:\n        print()\n        print(hr("═"))\n        n_sessions = len(runner.final_report_lines)\n        print(f"FINAL PER-DAY TRADE REPORT — {n_sessions} replayed "\n              f"{\'session\' if n_sessions == 1 else \'sessions\'}, each in its "\n              f"end-of-day state")\n        print(hr("═"))\n        for block in runner.final_report_lines:\n            print(block)\n    return 0\n',
    )
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
    """Replace a file in one step, so a crash cannot leave it half-written.

    The mode is carried over from the file being replaced (or 0644 for a new
    one): mkstemp creates 0600, and os.replace() keeps the temporary file's
    mode, which would otherwise leave the patched tree readable only by its
    owner.
    """
    d = os.path.dirname(path) or "."
    mode = 0o644
    if os.path.exists(path):
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".patch_v9_", suffix=".tmp")
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
    every = TARGETS + CREATED
    missing = [r for r in TARGETS if not os.path.isfile(os.path.join(base, r))]
    if missing:
        print(f"ABORT: not found under {base}: {', '.join(missing)}")
        print("usage: python3 patch_v9.py [/path/to/repo]")
        return 2

    staged, notes = {}, []
    for rel in every:
        path = os.path.join(base, rel)
        text = None
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        digest = hashlib.md5(text.encode()).hexdigest() if text is not None else None
        table = MD5[rel]

        if digest == table["v9"]:
            staged[rel] = (text, "current", 0, len([
                h for h in PLAN_V9 if h[0] == rel]))
            continue

        if digest != table["v8"]:
            notes.append(
                f"{rel}: content is neither the tested v8 state nor the v9 "
                f"result (got {digest[:12] if digest else 'missing'}); trying "
                f"the hunks anyway")

        mine = [h for h in PLAN_V9 if h[0] == rel]
        out, status, applied, skipped = apply_hunks(text or "", mine)
        if out is None:
            print(f"ABORT: {rel}: {status} - nothing was modified")
            print("The file has drifted from the tested v8 content. Apply")
            print("patch_v8.py first, or restore the file (git checkout -- "
                  + rel + ") and re-apply the patch chain.")
            return 3

        got = hashlib.md5(out.encode()).hexdigest()
        if got != table["v9"]:
            print(f"ABORT: {rel} did not land on the tested v9 content")
            print(f"       got {got[:12]} expected {table['v9'][:12]} "
                  f"- nothing was modified")
            return 3
        staged[rel] = (out, status, applied, skipped)

    print(f"patch_v9: patching {base}")
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
        print(f"patch_v9 applied: {len(written)} file(s) written "
              f"({', '.join(written)})")
    else:
        print("patch_v9 applied: nothing to do - the tree is already at v9")
    print()
    for c in CHANGES:
        print(f"  + {c}")
    print()
    print("verify:  python3 backtest_engine.py --test   # harness self-test")
    print()
    print("replay:  python3 backtest_engine.py --db data/per_day/"
          "nifty_algo_2026-09-08.db --from 2026-09-08 --to 2026-09-08")
    print("         python3 backtest_engine.py --db data/nifty_algo_v3.db "
          "--from 2026-09-08 --to 2026-09-11")
    print("         # the FINAL PER-DAY TRADE REPORT is the last section printed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))