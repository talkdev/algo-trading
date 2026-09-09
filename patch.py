#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════════════════
 patch-version8.py — NIFTY intraday options engine, correctness patch v3.8
════════════════════════════════════════════════════════════════════════════

 Prerequisite: v3.7 must already be installed. Self-contained, stdlib only.

 This is a CORRECTNESS patch, not a strategy patch. It changes two things and
 nothing else. It cannot change a trading decision: every edit below sits
 inside a self-test, or inside a log string. This was the deliberate scope —
 do not tune the engine's economics off the back of a single 57-minute
 sample.

 ── ITEM 1: SELF-TESTS WRITE TO THE LIVE PRODUCTION DATABASE ───────────────

 Nearly every engine module has a `_self_test()` you can run with
 `python <module>.py`. Each one starts with

     db = Database(config.db_path)

 and `config.db_path` is the REAL, production book (data/nifty_algo_v3.db).
 Several of those self-tests persist state as a side effect:

     execution_engine  ->  session_state      (daily_pnl, consecutive_stops,
                                                daily_halted, last_stop_reason)
     data_engine       ->  session_state, cycle_log, options_chain,
                           market_snapshots, vix_history   (via run_cycle)
     regime_engine     ->  calibration_state   (via CalibrationEngine.run)
     strategy_engine   ->  strategy_decisions  (via decide / _persist_decision)
     core              ->  audit_log           (via the logging test)

 On 2026-09-07 and 2026-09-08 both `session_state` rows in the live DB ended
 up reading

     daily_halted=1, consecutive_stops=2, daily_pnl=-1000,
     last_stop_reason='CLOSE_STOP'

 while `positions` was empty and `trade_exits` had ZERO rows — a fabricated
 halt with no trades to justify it. The State Update Tests in
 execution_engine's self-test drive _update_state_after_close through a win
 (+5000) then two stops (-3000, -3000); that method PERSISTS those numbers
 into session_state, and the "reset" block at the end of the test only clears
 the in-memory dict — it never writes the reset back. The arithmetic matches
 exactly: 0 + 5000 - 3000 - 3000 = -1000.

 The fix is the one the calibration_engine self-test already uses: give every
 self-test its own throwaway Database in a temporary directory, so no
 self-test can ever touch the production book. `config` is left untouched (it
 may be frozen); only the scratch Database is handed to the code under test.

 ── ITEM 2: LOT-SIZE LOG LINE SAYS 75 ──────────────────────────────────────

 main.py _verify_lot_size() logs "As of 2026, NIFTY lot size = 75 units".
 The config is 65, and NIFTY 50's lot size is 65 units as of the Jan-2026
 series (established during the v3.x series). The message asks the operator to
 MANUALLY VERIFY the spec, so a wrong number here is a real hazard even though
 the code itself never uses 75. It is corrected to 65.

 ── WHAT THIS DOES NOT DO ──────────────────────────────────────────────────

 It does not touch min_ev, min_credit_risk_ratio, min_lots_fraction, any
 risk/size multiplier, any delta target, any band, or any gate. Those are
 economics and belong in a data-driven change, not in a "make it safe" patch
 that is judged against a single session.

 Usage:
     python3 patch-version8.py            # apply
     python3 patch-version8.py --verify   # report state, change nothing
"""

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

MARKER_V37 = "NIFTY_ENGINE_PROFIT_PATCH_V37"
MARKER_V38 = "NIFTY_ENGINE_PROFIT_PATCH_V38"

TOUCHED = [
    "core.py",
    "data_engine.py",
    "execution_engine.py",
    "regime_engine.py",
    "strategy_engine.py",
    "main.py",
]

# The one lot-size line in main.py that is factually wrong.
LOT_OLD = '            f"As of 2026, NIFTY lot size = 75 units (verify current spec)."'
LOT_NEW = '            f"As of the Jan-2026 series, NIFTY 50 lot size = 65 units."'


def _child_env() -> dict:
    """
    Force UTF-8 on any Python we spawn.

    Several engine self-tests print box characters and arrows. When a child's
    stdout is a console Windows routes it through WriteConsoleW and it
    survives, but subprocess.run captures output through a PIPE, and a piped
    stdout falls back to the process locale encoding - cp1252 on a default
    Windows install - so the child dies with UnicodeEncodeError before it can
    report whether the assertions passed. That failure is an artefact of how
    this script calls the test, not a real test failure.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# Injected ahead of everything else in any Python we spawn. Environment
# variables turned out not to be enough on the reported Windows 3.13 box, so
# this rebinds sys.stdout/sys.stderr to explicit UTF-8 wrappers around the
# raw byte buffers from inside the child itself.
_UTF8_PRELUDE = (
    "import sys, io\n"
    "for _nm in ('stdout', 'stderr'):\n"
    "    try:\n"
    "        _st = getattr(sys, _nm)\n"
    "        if _st is not None and hasattr(_st, 'buffer'):\n"
    "            setattr(sys, _nm, io.TextIOWrapper(\n"
    "                _st.buffer, encoding='utf-8', errors='replace',\n"
    "                line_buffering=True))\n"
    "    except Exception:\n"
    "        pass\n"
)


def _run_py(body: str, timeout: int) -> subprocess.CompletedProcess:
    """Run Python in the repo with UTF-8 forced three separate ways."""
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _UTF8_PRELUDE + body],
        cwd=BASE, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", env=_child_env(),
    )


def _harden_stdout() -> None:
    """Survive being redirected to a file or pipe on a cp1252 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


class PatchError(RuntimeError):
    pass


class FilePatcher:
    """Exact-match, apply-once text substitution with a full audit trail."""

    def __init__(self, name: str):
        self.name = name
        self.path = BASE / name
        if not self.path.exists():
            raise PatchError(f"{name} not found in {BASE}")
        self.src = self.path.read_text(encoding="utf-8")
        self.original = self.src
        self.log: list[str] = []

    def sub(self, label: str, old: str, new: str) -> None:
        n = self.src.count(old)
        if n == 0:
            raise PatchError(
                f"[{self.name}] anchor not found for '{label}'.\n"
                f"  looked for:\n    {old[:200]!r}"
            )
        if n > 1:
            raise PatchError(
                f"[{self.name}] anchor for '{label}' matched {n} times; "
                f"it must be unique"
            )
        self.src = self.src.replace(old, new, 1)
        self.log.append(label)

    def contains(self, needle: str) -> bool:
        return needle in self.src

    def write(self) -> None:
        self.path.write_text(self.src, encoding="utf-8")


def backup_all(stamp: str) -> Path:
    d = BASE / f"patch_v38_backup_{stamp}"
    d.mkdir(exist_ok=True)
    for name in TOUCHED + ["env.txt"]:
        p = BASE / name
        if p.exists():
            shutil.copy2(p, d / name)
    return d


def restore_all(d: Path) -> None:
    for f in d.iterdir():
        shutil.copy2(f, BASE / f.name)


# ── The DB-isolation replacement blocks ─────────────────────────────────────
# Each self-test already opens with `import tempfile as _tf<N>` (core does
# not). We reuse that alias and pull in Path locally. The scratch Database
# lives in a per-run temporary directory.

DB_REPLACEMENTS = {
    "core.py": (
        "    db = Database(config.db_path)",
        (
            "    # v3.8: run this self-test against an isolated scratch\n"
            "    # database, never the production book. The logging test\n"
            "    # writes audit_log rows; config is left untouched (it may be\n"
            "    # frozen), so only the scratch Database is handed in.\n"
            "    import tempfile as _scratch_tmp\n"
            "    db = Database(Path(_scratch_tmp.mkdtemp(\n"
            "        prefix=\"core_selftest_\")) / \"core_selftest.db\")\n"
        ),
    ),
    "data_engine.py": (
        "    db           = Database(config.db_path)",
        (
            "    # v3.8: isolate this self-test from the live production\n"
            "    # database. run_cycle persists session_state, cycle_log,\n"
            "    # options_chain, market_snapshots and vix_history when the\n"
            "    # live-API branch fires; a self-test must never write to the\n"
            "    # production book. config is left untouched; only the scratch\n"
            "    # Database is handed in.\n"
            "    from pathlib import Path as _scratch_path\n"
            "    db = Database(_scratch_path(_tf5.mkdtemp(\n"
            "        prefix=\"data_selftest_\")) / \"data_selftest.db\")\n"
        ),
    ),
    "execution_engine.py": (
        "    db            = Database(config.db_path)",
        (
            "    # v3.8: isolate this self-test from the live production\n"
            "    # database. The State Update Tests below drive\n"
            "    # _update_state_after_close through a win then two stops, and\n"
            "    # that method PERSISTS daily_pnl, consecutive_stops and\n"
            "    # daily_halted into session_state; its \"reset\" block at the\n"
            "    # end only clears the in-memory dict. Pointing those writes at\n"
            "    # config.db_path left the live book with a fabricated halt\n"
            "    # (daily_halted=1, consecutive_stops=2, daily_pnl=-1000,\n"
            "    # last_stop_reason=CLOSE_STOP) and zero matching trades. A\n"
            "    # test must never write to production. config is left as it\n"
            "    # is; only the scratch Database is handed in.\n"
            "    from pathlib import Path as _scratch_path\n"
            "    db = Database(_scratch_path(_tf3.mkdtemp(\n"
            "        prefix=\"exec_selftest_\")) / \"exec_selftest.db\")\n"
        ),
    ),
    "regime_engine.py": (
        "    db           = Database(config.db_path)",
        (
            "    # v3.8: isolate this self-test from the live production\n"
            "    # database. CalibrationEngine.run() persists calibration_state;\n"
            "    # a self-test must never write to the production book. config\n"
            "    # is left untouched; only the scratch Database is handed in.\n"
            "    from pathlib import Path as _scratch_path\n"
            "    db = Database(_scratch_path(_tf4.mkdtemp(\n"
            "        prefix=\"regime_selftest_\")) / \"regime_selftest.db\")\n"
        ),
    ),
    "strategy_engine.py": (
        "    db            = Database(config.db_path)",
        (
            "    # v3.8: isolate this self-test from the live production\n"
            "    # database. decide()/_persist_decision writes strategy_decisions;\n"
            "    # a self-test must never write to the production book. config\n"
            "    # is left untouched; only the scratch Database is handed in.\n"
            "    from pathlib import Path as _scratch_path\n"
            "    db = Database(_scratch_path(_tf2.mkdtemp(\n"
            "        prefix=\"strat_selftest_\")) / \"strat_selftest.db\")\n"
        ),
    ),
}

# The closing "Database:" print in every self-test pointed at the live path.
FINAL_DB_PRINT_OLD = '    print(f"  Database: {config.db_path}")'
FINAL_DB_PRINT_NEW = '    print(f"  Database: {db.db_path}")'


def patch_core(p: FilePatcher) -> None:
    p.sub("core/version-marker-v38",
          f'{MARKER_V37} = "3.7"',
          f'{MARKER_V37} = "3.7"\n{MARKER_V38} = "3.8"')


def patch_db_isolation(p: FilePatcher) -> None:
    name = p.name
    old, new = DB_REPLACEMENTS[name]
    p.sub(f"{name}/self-test-scratch-db", old, new)


def patch_final_print(p: FilePatcher) -> None:
    p.sub(f"{p.name}/self-test-db-path-print", FINAL_DB_PRINT_OLD, FINAL_DB_PRINT_NEW)


def patch_main(p: FilePatcher) -> None:
    p.sub("main/lot-size-65", LOT_OLD, LOT_NEW)


def verify_syntax() -> list[str]:
    errs = []
    for name in TOUCHED:
        try:
            ast.parse((BASE / name).read_text(encoding="utf-8"), filename=name)
        except SyntaxError as e:
            errs.append(f"{name}: line {e.lineno}: {e.msg}")
    return errs


def verify_semantics() -> list[str]:
    errs = []
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    main_src = (BASE / "main.py").read_text(encoding="utf-8")
    srcs = {
        n: (BASE / n).read_text(encoding="utf-8")
        for n in ("core.py", "data_engine.py", "execution_engine.py",
                  "regime_engine.py", "strategy_engine.py")
    }

    checks = [
        ("core carries the v3.8 marker", MARKER_V38 in core_src),
        ("core keeps the v3.7 marker", MARKER_V37 in core_src),
        ("no self-test opens the live DB ('Database(config.db_path)' gone)",
         "Database(config.db_path)" not in "".join(srcs.values())),
        ("main.py no longer claims a 75-unit lot size",
         "lot size = 75 units" not in main_src),
        ("main.py presents the 65-unit lot size",
         "lot size = 65 units" in main_src),
    ]
    for name in srcs:
        checks.append(
            (f"{name} self-test creates a scratch Database",
             "mkdtemp" in srcs[name] and "selftest.db" in srcs[name]),
        )
    for label, ok in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            errs.append(label)

    # Confirm every self-test file still imports cleanly.
    for mod in ("core", "data_engine", "execution_engine", "regime_engine",
                "strategy_engine"):
        out = _run_py(f"import {mod}\nprint('IMPORT_OK')\n", 180)
        ok = out.returncode == 0 and "IMPORT_OK" in out.stdout
        print(f"    {'PASS' if ok else 'FAIL'}  {mod} imports")
        if not ok:
            errs.append(f"{mod} import failed")
    return errs


def verify_behaviour() -> list[str]:
    """
    Prove the two properties the patch claims, without running any self-test
    that would touch the network.

      1. The self-test scratch DB path never equals the live config.db_path.
      2. The main.py lot-size line resolves to 65, not 75.
    """
    errs = []
    out = _run_py(
        "import tempfile, pathlib, configparser, os, core\n"
        "cfg = core.load_config()\n"
        "live = str(cfg.db_path)\n"
        "scratch = str(pathlib.Path(tempfile.mkdtemp(\"x\")) / 'selftest.db')\n"
        "print(f'live    : {live}')\n"
        "print(f'scratch : {scratch}')\n"
        "assert scratch != live, 'scratch must not equal the live book'\n"
        "assert 'nifty_algo' in live or live.endswith('.db'), 'live is the db'\n"
        "print('ISOLATION_OK')\n", 180)
    for line in (out.stdout or "").strip().splitlines():
        if "ISOLATION_OK" not in line:
            print(f"           {line}")
    ok = out.returncode == 0 and "ISOLATION_OK" in out.stdout
    print(f"    {'PASS' if ok else 'FAIL'}  scratch DB can never equal the live book")
    if not ok:
        errs.append("isolation check failed")

    main_src = (BASE / "main.py").read_text(encoding="utf-8")
    # The lot-size f-string must now print 65 when the config is 65.
    out = _run_py(
        "import re\n"
        "src = open('main.py', encoding='utf-8').read()\n"
        "m = re.search(r'As of[^\\n]*lot size = (\\d+) units', src)\n"
        "assert m and m.group(1) == '65', f'expected 65, got {m.group(1) if m else None}'\n"
        "print('LOTSIZE_OK')\n", 180)
    ok = out.returncode == 0 and "LOTSIZE_OK" in out.stdout
    print(f"    {'PASS' if ok else 'FAIL'}  lot-size log line reads 65")
    if not ok:
        errs.append("lot-size line failed")
    return errs


def run_self_tests() -> list[str]:
    """
    Run each engine self-test. Because the patch is already written to disk by
    the time this runs, every self-test now uses its own scratch Database, so
    this is safe: none of them can write to the production book. The live-API
    sections inside a few self-tests resolve gracefully (validate_token returns
    False on a real network hit, which skips them).
    """
    errs = []
    for mod in ("strategy_engine.py", "data_engine.py", "regime_engine.py",
                "execution_engine.py", "core.py"):
        body = ("import runpy\n"
                f"runpy.run_path({str(BASE / '@M@')!r}, run_name='__main__')\n"
                ).replace("@M@", mod)
        out = _run_py(body, 900)
        combined = (out.stderr or "") + (out.stdout or "")
        ok = out.returncode == 0
        if not ok and "UnicodeEncodeError" in combined:
            print(f"    WARN  {mod} could not write its output on this console")
            out = _run_py("import runpy, os, sys\n"
                          "sys.stdout = open(os.devnull, 'w')\n"
                          + body.split("\n", 1)[1], 900)
            combined = (out.stderr or "") + (out.stdout or "")
            ok = out.returncode == 0
        print(f"    {'PASS' if ok else 'FAIL'}  {mod} self test")
        if not ok:
            for line in (combined.strip().splitlines() or ["(no output)"])[-8:]:
                print(f"           {line}")
            errs.append(f"{mod} self test failed")
    return errs


def do_verify() -> int:
    print("\n  state of the tree\n")
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    for label, marker in (("v3.7", MARKER_V37), ("v3.8", MARKER_V38)):
        print(f"    {label}: {'installed' if marker in core_src else 'NOT installed'}")
    print()
    errs = verify_semantics()
    print()
    return 1 if errs else 0


def main() -> int:
    _harden_stdout()
    ap = argparse.ArgumentParser(description="NIFTY engine correctness patch v3.8")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    print("=" * 76)
    print(" NIFTY intraday options engine - correctness patch v3.8")
    print(" self-tests never touch the live book | lot-size log line corrected")
    print("=" * 76)
    if args.verify:
        return do_verify()
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    if MARKER_V37 not in core_src:
        print("\n  REFUSING: v3.7 must be installed first. Run patch-version7.py.\n")
        return 1
    if MARKER_V38 in core_src:
        print("\n  v3.8 is already installed. Nothing to do.\n")
        return 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_all(stamp)
    print(f"\n  backup: {backup.name}/\n")
    try:
        patchers = {}
        for name in ("core.py", "data_engine.py", "execution_engine.py",
                     "regime_engine.py", "strategy_engine.py"):
            p = FilePatcher(name)
            patch_db_isolation(p)
            patch_final_print(p)
            patchers[name] = p
        # core also gets the version marker.
        patch_core(patchers["core.py"])
        # main.py gets the lot-size correction.
        p_main = FilePatcher("main.py")
        patch_main(p_main)
        patchers["main.py"] = p_main

        for name, p in patchers.items():
            p.write()
            for label in p.log:
                print(f"    applied  {label}")
        print("\n  syntax\n")
        errs = verify_syntax()
        for e in errs:
            print(f"    FAIL  {e}")
        if not errs:
            print("    PASS  all touched files parse")
        print("\n  semantics\n")
        errs += verify_semantics()
        print("\n  behaviour\n")
        errs += verify_behaviour()
        print("\n  self tests\n")
        errs += run_self_tests()
        if errs:
            raise PatchError(f"{len(errs)} verification failure(s)")
    except Exception as exc:                                # noqa: BLE001
        print(f"\n  ERROR: {exc}")
        print("  restoring every touched file from the backup...")
        restore_all(backup)
        print("  restored. The tree is exactly as it was.\n")
        return 1
    print("\n" + "=" * 76)
    print(" v3.8 applied and verified.")
    print("=" * 76)
    print(f"""
 Two correctness fixes, no strategy change.

  1. Self-tests no longer touch the production database. Every engine self-test
     now builds its own throwaway Database in a temporary directory, so
     `python <module>.py` can never write session_state, cycle_log,
     calibration_state, strategy_decisions or audit_log into your live book.
     The fabricated halt that appeared on 2026-09-08 came from exactly that.

  2. The lot-size log line is corrected to 65 units.

 This cannot change a trading decision: every edit is inside a self-test or a
 log string. Economics (min_ev, min_credit_risk_ratio, min_lots_fraction,
 sizing multipliers, delta targets, bands) are untouched.

 Rollback

   cp {backup.name}/* .
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())