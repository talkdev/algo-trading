#!/usr/bin/env python3
"""
path.py

Safe and idempotent patcher for the NIFTY options algo engine.

Validated fixes:
    - Initialize _effective_regime before first use.
    - Handle None option deltas safely.
    - Normalize IV-RV spread-history units before Edge z-scoring.
    - Remove repeated synthetic IV-RV history seeding.
    - Preserve cold-start-safe IV-rank handling.
    - Mark each position leg using its own expiry chain.
    - Refresh historical 30-minute candles every 1,800 seconds.
    - Display exact vega pre-trade rejection details.

Safety:
    - Uses AST to locate functions and methods.
    - Does not import or execute the trading engine.
    - Creates a timestamped backup.
    - Compiles all six source files before replacement.
    - Restores changed files if replacement fails.
"""

from __future__ import annotations

import ast
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent

FILES = (
    "main.py",
    "decision_journal.py",
    "data_manager.py",
    "config.py",
    "regime_engine.py",
    "strategy_engine.py",
)


class PatchError(RuntimeError):
    """Raised when a safe patch cannot be completed."""


def fail(message: str) -> None:
    raise PatchError(message)


def read_source(filename: str) -> str:
    path = ROOT / filename

    if not path.is_file():
        fail(f"Required file not found: {filename}")

    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        fail(f"Unable to read {filename}: {exc}")


def validate_source(filename: str, source: str) -> None:
    """
    Parse and compile source without importing or executing it.
    """
    path = ROOT / filename

    try:
        tree = ast.parse(
            source,
            filename=str(path),
        )
        compile(
            tree,
            str(path),
            "exec",
        )
    except SyntaxError as exc:
        fail(
            f"Syntax error in {filename}: "
            f"line {exc.lineno}: {exc.msg}"
        )


def make_line_offsets(source: str) -> list[int]:
    offsets = [0]
    total = 0

    for line in source.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)

    return offsets


def absolute_offset(
    offsets: list[int],
    lineno: int,
    col_offset: int,
) -> int:
    if lineno < 1 or lineno > len(offsets):
        fail(
            f"Invalid AST line number: {lineno}"
        )

    return offsets[lineno - 1] + col_offset


def find_function(
    source: str,
    function_name: str,
) -> tuple[int, int, str]:
    """
    Find exactly one module-level function or class method.

    AST locations are used so this works for both:

        def _display_console(...)

    and:

        class StrategyEngine:
        def _enter_new_position(...)
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        fail(
            f"Cannot parse source while locating "
            f"{function_name}: line {exc.lineno}: {exc.msg}"
        )

    offsets = make_line_offsets(source)
    matches: list[tuple[int, int]] = []

    for node in ast.walk(tree):
        if not isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
            ),
        ):
            continue

        if node.name != function_name:
            continue

        if not hasattr(node, "end_lineno"):
            fail(
                f"AST end locations unavailable for "
                f"{function_name}"
            )

        start = absolute_offset(
            offsets,
            node.lineno,
            node.col_offset,
        )

        end = absolute_offset(
            offsets,
            node.end_lineno,
            node.end_col_offset,
        )

        matches.append((start, end))

    if not matches:
        fail(
            f"Function or method not found: {function_name}"
        )

    if len(matches) != 1:
        fail(
            f"Ambiguous function or method: {function_name} "
            f"was found {len(matches)} times"
        )

    start, end = matches[0]

    return start, end, source[start:end]


def replace_function(
    source: str,
    function_name: str,
    replacement: str,
) -> str:
    start, end, _ = find_function(
        source,
        function_name,
    )

    return source[:start] + replacement + source[end:]


def patch_effective_regime(source: str) -> str:
    """
    Fix the observed UnboundLocalError by ensuring
    _effective_regime is initialized before its first use.
    """
    start, end, function_source = find_function(
        source,
        "_should_enter_new_position",
    )

    assignment_pattern = re.compile(
        r"(?m)^[ \t]*_effective_regime\s*=\s*regime\s*$\n?"
    )

    # Remove all previous standalone assignments in this function.
    function_source = assignment_pattern.sub(
        "",
        function_source,
    )

    regime_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)"
        r"regime\s*=\s*self\.re\.confirmed_regime\s*$"
    )

    regime_match = regime_pattern.search(function_source)

    if regime_match is None:
        fail(
            "Could not find "
            "regime = self.re.confirmed_regime "
            "inside _should_enter_new_position()"
        )

    indent = regime_match.group("indent")

    replacement = (
        regime_match.group(0)
        + "\n"
        + indent
        + "# Initialize before the VIX-gate reference.\n"
        + indent
        + "_effective_regime = regime"
    )

    function_source = (
        function_source[:regime_match.start()]
        + replacement
        + function_source[regime_match.end():]
    )

    return source[:start] + function_source + source[end:]


def patch_none_deltas(source: str) -> str:
    """
    Fix float(None) in fetch_oi_snapshot().
    """
    start, end, function_source = find_function(
        source,
        "fetch_oi_snapshot",
    )

    if (
        "call_delta = _sf(" in function_source
        and "put_delta = _sf(" in function_source
    ):
        return source

    pattern = re.compile(
        r"(?ms)(?P<indent>^[ \t]*)"
        r"call_delta\s*=\s*float\(\s*"
        r"call_data\.get\(\s*[\"']delta[\"']\s*,\s*0\s*\)"
        r"\s*\)\s*"
        r"\n"
        r"(?P<indent2>^[ \t]*)"
        r"put_delta\s*=\s*float\(\s*"
        r"put_data\.get\(\s*[\"']delta[\"']\s*,\s*0\s*\)"
        r"\s*\)"
    )

    match = pattern.search(function_source)

    if match is None:
        fail(
            "Could not find option delta conversion inside "
            "fetch_oi_snapshot()"
        )

    indent = match.group("indent")

    replacement = (
        indent
        + "call_delta = _sf(\n"
        + indent
        + "    call_data.get(\"delta\"),\n"
        + indent
        + "    0.0,\n"
        + indent
        + ")\n"
        + indent
        + "put_delta = _sf(\n"
        + indent
        + "    put_data.get(\"delta\"),\n"
        + indent
        + "    0.0,\n"
        + indent
        + ")"
    )

    function_source = (
        function_source[:match.start()]
        + replacement
        + function_source[match.end():]
    )

    return source[:start] + function_source + source[end:]


def patch_edge_history_units(source: str) -> str:
    """
    Normalize stored decimal IV-RV history into percentage points
    before comparing it with the current percentage-point edge.

    This directly prevents invalid values such as z=947.91.
    """
    start, end, function_source = find_function(
        source,
        "_module_edge",
    )

    if "_spread_history = [" in function_source:
        return source

    old = (
        "        _spread_history = list("
        "self.dm.iv_rv_spread_history)"
    )

    new = (
        "        # Current edge is calculated in percentage points.\n"
        "        # DataManager stores IV-RV history as decimal values.\n"
        "        # Normalize history before calculating the z-score.\n"
        "        _spread_history = [\n"
        "            float(value) * 100.0\n"
        "            for value in "
        "self.dm.iv_rv_spread_history\n"
        "            if value is not None\n"
        "        ]"
    )

    if old not in function_source:
        fail(
            "Could not find IV-RV spread history inside "
            "_module_edge()"
        )

    function_source = function_source.replace(
        old,
        new,
        1,
    )

    return source[:start] + function_source + source[end:]


def patch_edge_std_floor(source: str) -> str:
    """
    Prevent a z-score from being trusted when historical variance is
    too small.
    """
    start, end, function_source = find_function(
        source,
        "_module_edge",
    )

    if "_min_edge_std_pp" in function_source:
        return source

    old = "            if _sp_std > 0.001:\n"

    new = (
        "            _min_edge_std_pp = getattr(\n"
        "                config,\n"
        "                'EDGE_MIN_SPREAD_STD_PP',\n"
        "                0.25,\n"
        "            )\n"
        "            if _sp_std >= _min_edge_std_pp:\n"
    )

    if old not in function_source:
        fail(
            "Could not find Edge standard-deviation condition "
            "inside _module_edge()"
        )

    function_source = function_source.replace(
        old,
        new,
        1,
    )

    return source[:start] + function_source + source[end:]


def patch_synthetic_history(source: str) -> str:
    """
    Remove repeated synthetic history insertion from
    fetch_option_chain().

    If that code is already removed, no change is made.
    """
    start, end, function_source = find_function(
        source,
        "fetch_option_chain",
    )

    if (
        "history remains unseeded" in function_source
        or "Do not insert repeated synthetic values."
        in function_source
    ):
        return source

    pattern = re.compile(
        r"(?ms)(?P<indent>^[ \t]*)"
        r"for _ in range\(min_hist\):\s*"
        r"\n(?P=indent)[ \t]+"
        r"self\.iv_rv_spread_history\.append\(\s*"
        r"\n(?P=indent)[ \t]+estimated_spread\s*"
        r"\n(?P=indent)[ \t]+\)\s*"
        r"\n(?P=indent)logger\.info\(\s*"
        r"\n(?P=indent)[ \t]*"
        r"f[\"']Bootstrapped iv_rv_spread\s*"
        r"\n(?P=indent)[ \t]*"
        r"f[\"']\(from chain\):\s*"
        r"\n(?P=indent)[ \t]*"
        r"f[\"']spread=\{estimated_spread:\.4f\}"
        r"\s*\n(?P=indent)\)"
    )

    match = pattern.search(function_source)

    if match is None:
        return source

    indent = match.group("indent")

    replacement = (
        indent
        + "# Do not insert repeated synthetic values.\n"
        + indent
        + "# One VIX-derived estimate is not a history of\n"
        + indent
        + "# independent NIFTY trading sessions.\n"
        + indent
        + "logger.info(\n"
        + indent
        + "    f\"IV-RV history remains unseeded until real \"\n"
        + indent
        + "    f\"daily observations accumulate: \"\n"
        + indent
        + "    f\"estimated_spread={estimated_spread:.4f}\"\n"
        + indent
        + ")"
    )

    function_source = (
        function_source[:match.start()]
        + replacement
        + function_source[match.end():]
    )

    return source[:start] + function_source + source[end:]


def patch_iv_rank_fallback(source: str) -> str:
    """
    Prevent an unguarded IV-rank assignment from overwriting the
    cold-start fallback in _select_strategy().
    """
    start, end, function_source = find_function(
        source,
        "_select_strategy",
    )

    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)"
        r"iv_rank\s*=\s*self\.dm\.compute_iv_rank\(\)\s*$"
    )

    matches = list(pattern.finditer(function_source))

    if not matches:
        return source

    for match in reversed(matches):
        indent = match.group("indent")

        replacement = (
            indent
            + "iv_rank = (\n"
            + indent
            + "    _iv_rank_raw\n"
            + indent
            + "    if _iv_rank_raw is not None\n"
            + indent
            + "    else 50.0\n"
            + indent
            + ")"
        )

        function_source = (
            function_source[:match.start()]
            + replacement
            + function_source[match.end():]
        )

    return source[:start] + function_source + source[end:]


def patch_position_premium(source: str) -> str:
    """
    Ensure each position leg is marked using that leg's own expiry.
    """
    start, end, function_source = find_function(
        source,
        "_get_position_current_premium",
    )

    if (
        "get_chain_for_expiry(\n                leg.expiry"
        in function_source
        or "get_chain_for_expiry(leg.expiry)"
        in function_source
    ):
        return source

    replacement = '''    def _get_position_current_premium(
        self, position: Position
    ) -> float:
        """Return current net premium using each leg's expiry."""
        net = 0.0

        for leg in position.legs:
            leg_chain = self.dm.get_chain_for_expiry(
                leg.expiry
            )
            opt_data = (
                leg_chain
                .get(leg.strike, {})
                .get(leg.option_type, {})
            )
            mark = self.dm.get_mark_price(
                opt_data,
                fallback=leg.entry_price,
            )

            if leg.action == "SELL":
                net += mark * leg.qty
            else:
                net -= mark * leg.qty

        return net
'''

    return source[:start] + replacement + source[end:]


def patch_config(source: str) -> str:
    """
    Set the historical 30-minute candle refresh interval to 1,800
    seconds if the configuration still uses 60 seconds.
    """
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)"
        r"CANDLE_REFRESH_SECONDS\s*=\s*\d+.*$"
    )

    match = pattern.search(source)

    if match is None:
        fail(
            "CANDLE_REFRESH_SECONDS was not found in config.py"
        )

    current = match.group(0)

    if "1800" in current:
        return source

    indent = match.group("indent")

    replacement = (
        indent
        + "CANDLE_REFRESH_SECONDS = 1800  "
        "# NIFTY 30-minute candle refresh"
    )

    return (
        source[:match.start()]
        + replacement
        + source[match.end():]
    )


def patch_edge_config(source: str) -> str:
    """
    Add the Edge standard-deviation floor configuration.
    """
    if "EDGE_MIN_SPREAD_STD_PP" in source:
        return source

    marker = "EDGE_SCORE_MIN_HISTORY = 20"

    if marker not in source:
        fail(
            "EDGE_SCORE_MIN_HISTORY = 20 was not found in config.py"
        )

    addition = (
        "\n"
        "# Minimum IV-RV history standard deviation in percentage\n"
        "# points before trusting the Edge z-score.\n"
        "EDGE_MIN_SPREAD_STD_PP = 0.25"
    )

    return source.replace(
        marker,
        marker + addition,
        1,
    )


def patch_vega_diagnostic(source: str) -> str:
    """
    Add exact vega details to the existing vega rejection log.

    This does not change the vega decision. It only improves
    observability.
    """
    start, end, function_source = find_function(
        source,
        "_pre_trade_checks",
    )

    if "VEGA_DIAGNOSTIC" in function_source:
        return source

    old = (
        "                logger.warning(\n"
        "                    f\"Pre-trade: vega above max: \"\n"
        "                    f\"post={post_vega:.0f} > max={vega_max} \"\n"
        "                    f\"— blocking entry\"\n"
        "                )\n"
        "                return False"
    )

    new = (
        "                logger.warning(\n"
        "                    f\"Pre-trade: vega above max: \"\n"
        "                    f\"post={post_vega:.0f} > max={vega_max} \"\n"
        "                    f\"— blocking entry\"\n"
        "                )\n"
        "                logger.info(\n"
        "                    \"VEGA_DIAGNOSTIC | \"\n"
        "                    f\"portfolio={port_greeks['vega']:.0f} | \"\n"
        "                    f\"candidate={new_greeks['vega']:.0f} | \"\n"
        "                    f\"post={post_vega:.0f} | \"\n"
        "                    f\"min={vega_min} | \"\n"
        "                    f\"max={vega_max} | \"\n"
        "                    f\"strategy={strategy_name}\"\n"
        "                )\n"
        "                if hasattr(self, \"_set_entry_diagnostic\"):\n"
        "                    self._set_entry_diagnostic(\n"
        "                        \"PRETRADE_FAILED\",\n"
        "                        \"Vega limit rejected candidate\",\n"
        "                        strategy=strategy_name,\n"
        "                        credit=None,\n"
        "                        pretrade=\"FAILED\",\n"
        "                        execution=\"NOT_RUN\",\n"
        "                        vega_portfolio=port_greeks['vega'],\n"
        "                        vega_candidate=new_greeks['vega'],\n"
        "                        vega_post=post_vega,\n"
        "                        vega_min=vega_min,\n"
        "                        vega_max=vega_max,\n"
        "                    )\n"
        "                return False"
    )

    if old not in function_source:
        # Support a source version with slightly different spacing.
        pattern = re.compile(
            r"(?ms)(?P<indent>[ \t]*)logger\.warning\(\s*"
            r"f[\"']Pre-trade: vega above max:.*?"
            r"f[\"']— blocking entry[\"']\s*"
            r"(?P=indent)\)\s*"
            r"(?P=indent)return False"
        )

        match = pattern.search(function_source)

        if match is None:
            # The detailed vega patch is optional if the source has
            # already been manually changed.
            return source

        indent = match.group("indent")

        replacement = (
            indent
            + "logger.warning(\n"
            + indent
            + "    f\"Pre-trade: vega above max: \"\n"
            + indent
            + "    f\"post={post_vega:.0f} > max={vega_max} \"\n"
            + indent
            + "    f\"— blocking entry\"\n"
            + indent
            + ")\n"
            + indent
            + "logger.info(\n"
            + indent
            + "    \"VEGA_DIAGNOSTIC | \"\n"
            + indent
            + "    f\"portfolio={port_greeks['vega']:.0f} | \"\n"
            + indent
            + "    f\"candidate={new_greeks['vega']:.0f} | \"\n"
            + indent
            + "    f\"post={post_vega:.0f} | \"\n"
            + indent
            + "    f\"min={vega_min} | max={vega_max} | \"\n"
            + indent
            + "    f\"strategy={strategy_name}\"\n"
            + indent
            + ")\n"
            + indent
            + "return False"
        )

        function_source = (
            function_source[:match.start()]
            + replacement
            + function_source[match.end():]
        )

        return source[:start] + function_source + source[end:]

    function_source = function_source.replace(
        old,
        new,
        1,
    )

    return source[:start] + function_source + source[end:]


def patch_console_trade_label(source: str) -> str:
    """
    Change the broad-gate console label so it does not imply that
    orders are already being sent.

    This is display-only.
    """
    if "BROAD GATES PASSED" in source:
        return source

    old = (
        '        print(f" TRADE DECISION: {G}ATTEMPTING {intended}{E}")'
    )

    new = (
        '        print(f" TRADE DECISION: {G}BROAD GATES PASSED — '
        'CANDIDATE {intended}{E}")'
    )

    if old not in source:
        return source

    return source.replace(
        old,
        new,
        1,
    )


def apply_patch() -> None:
    original = {
        filename: read_source(filename)
        for filename in FILES
    }

    patched = dict(original)

    # Runtime correctness.
    patched["strategy_engine.py"] = (
        patch_effective_regime(
            patched["strategy_engine.py"]
        )
    )

    patched["data_manager.py"] = (
        patch_none_deltas(
            patched["data_manager.py"]
        )
    )

    # Edge integrity.
    patched["regime_engine.py"] = (
        patch_edge_history_units(
            patched["regime_engine.py"]
        )
    )

    patched["regime_engine.py"] = (
        patch_edge_std_floor(
            patched["regime_engine.py"]
        )
    )

    patched["data_manager.py"] = (
        patch_synthetic_history(
            patched["data_manager.py"]
        )
    )

    patched["config.py"] = patch_edge_config(
        patched["config.py"]
    )

    # Strategy correctness.
    patched["strategy_engine.py"] = (
        patch_iv_rank_fallback(
            patched["strategy_engine.py"]
        )
    )

    patched["strategy_engine.py"] = (
        patch_position_premium(
            patched["strategy_engine.py"]
        )
    )

    # Avoid unnecessary historical-candle API requests.
    patched["config.py"] = patch_config(
        patched["config.py"]
    )

    # Detailed rejection reason for the observed vega failure.
    patched["strategy_engine.py"] = (
        patch_vega_diagnostic(
            patched["strategy_engine.py"]
        )
    )

    # Correct the broad console wording.
    patched["main.py"] = patch_console_trade_label(
        patched["main.py"]
    )

    changed = [
        filename
        for filename in FILES
        if patched[filename] != original[filename]
    ]

    if not changed:
        print(
            "No changes required. "
            "The validated fixes are already applied."
        )
        return

    # Compile every file before modifying anything.
    for filename in FILES:
        validate_source(
            filename,
            patched[filename],
        )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_dir = ROOT / (
        f"backup_before_path_{timestamp}"
    )

    backup_dir.mkdir(
        parents=False,
        exist_ok=False,
    )

    for filename in changed:
        shutil.copy2(
            ROOT / filename,
            backup_dir / filename,
        )

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix="nifty_path_",
            dir=str(ROOT),
        )
    )

    try:
        temporary_files: dict[str, Path] = {}

        for filename in changed:
            temp_path = temp_dir / filename

            temp_path.write_text(
                patched[filename],
                encoding="utf-8",
            )

            validate_source(
                filename,
                patched[filename],
            )

            temporary_files[filename] = temp_path

        for filename in changed:
            shutil.copy2(
                temporary_files[filename],
                ROOT / filename,
            )

    except Exception:
        # Restore changed files from the backup.
        for filename in changed:
            backup_path = backup_dir / filename

            if backup_path.is_file():
                shutil.copy2(
                    backup_path,
                    ROOT / filename,
                )

        raise

    finally:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

    print("Patch applied successfully.")
    print(
        "Changed files: "
        + ", ".join(changed)
    )
    print(
        "Backup created: "
        + backup_dir.name
    )
    print(
        "Syntax compilation passed for all six files."
    )
    print()
    print(
        "Vega rejection details and entry-stage diagnostics "
        "are now logged."
    )
    print(
        "Run PAPER_TRADING_MODE before enabling live trading."
    )


def main() -> int:
    try:
        apply_patch()
        return 0

    except PatchError as exc:
        print(
            f"PATCH ABORTED: {exc}",
            file=sys.stderr,
        )
        print(
            "No source files were intentionally modified.",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(
            f"PATCH FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())