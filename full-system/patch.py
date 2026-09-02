#!/usr/bin/env python3
"""
path.py

Safe, idempotent patcher for the NIFTY options algo engine.

Files handled:
    main.py
    decision_journal.py
    data_manager.py
    config.py
    regime_engine.py
    strategy_engine.py

Validated fixes:
    - _effective_regime initialization
    - None-safe OI delta handling
    - IV/RV spread-history unit normalization
    - prevention of repeated synthetic Edge history
    - Edge z-score variance floor
    - cold-start-safe IV-rank handling
    - own-expiry premium marking
    - 30-minute candle refresh cadence
    - detailed console entry diagnostics

The script validates all source files before replacement.
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
    """Raised when a required safe patch cannot be completed."""


def fail(message: str) -> None:
    raise PatchError(message)


def read_file(filename: str) -> str:
    path = ROOT / filename

    if not path.is_file():
        fail(f"Required file not found: {filename}")

    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        fail(f"Unable to read {filename}: {exc}")


def validate_python(
    filename: str,
    source: str,
) -> None:
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


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    total = 0

    for line in source.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)

    return offsets


def _absolute_offset(
    offsets: list[int],
    lineno: int,
    col_offset: int,
) -> int:
    if lineno < 1 or lineno > len(offsets):
        raise PatchError(
            f"Invalid AST line number: {lineno}"
        )

    return offsets[lineno - 1] + col_offset


def find_function(
    source: str,
    function_name: str,
) -> tuple[int, int, str]:
    """
    Find exactly one function or method using the AST.

    Supports:
        - module-level functions;
        - class methods;
        - async functions;
        - functions with any indentation.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        fail(
            f"Cannot parse source while locating "
            f"{function_name}: line {exc.lineno}: {exc.msg}"
        )

    offsets = _line_offsets(source)
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
                f"Python AST end locations unavailable for "
                f"{function_name}"
            )

        start = _absolute_offset(
            offsets,
            node.lineno,
            node.col_offset,
        )

        end = _absolute_offset(
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
            f"Ambiguous function name {function_name}: "
            f"found {len(matches)} definitions"
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
    Fix:
        UnboundLocalError for _effective_regime.

    The assignment is inserted immediately after:
        regime = self.re.confirmed_regime
    """
    start, end, function_source = find_function(
        source,
        "_should_enter_new_position",
    )

    regime_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)"
        r"regime\s*=\s*self\.re\.confirmed_regime\s*$"
    )

    regime_match = regime_pattern.search(function_source)

    if regime_match is None:
        fail(
            "Could not find regime assignment inside "
            "_should_enter_new_position()"
        )

    indent = regime_match.group("indent")

    # Remove existing standalone assignments in this function so the
    # patch remains idempotent and avoids duplicate initialization.
    cleaned = re.sub(
        r"(?m)^[ \t]*_effective_regime\s*=\s*regime\s*$\n?",
        "",
        function_source,
    )

    regime_match = regime_pattern.search(cleaned)

    if regime_match is None:
        fail(
            "Regime assignment disappeared while patching "
            "_should_enter_new_position()"
        )

    indent = regime_match.group("indent")

    insertion = (
        regime_match.group(0)
        + "\n"
        + indent
        + "# Effective regime is initialized before the VIX gate.\n"
        + indent
        + "_effective_regime = regime"
    )

    patched_function = (
        cleaned[:regime_match.start()]
        + insertion
        + cleaned[regime_match.end():]
    )

    return source[:start] + patched_function + source[end:]


def patch_none_deltas(source: str) -> str:
    """
    Fix:
        float(None) in fetch_oi_snapshot().
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

    patched_function = (
        function_source[:match.start()]
        + replacement
        + function_source[match.end():]
    )

    return source[:start] + patched_function + source[end:]


def patch_synthetic_edge_history(source: str) -> str:
    """
    Remove repeated synthetic IV-RV history insertion.

    This patch is optional because prior manual changes may already
    have removed the block. If the block is absent, the source is
    returned unchanged.
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
        + "# One estimate is not a history of NIFTY sessions.\n"
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

    patched_function = (
        function_source[:match.start()]
        + replacement
        + function_source[match.end():]
    )

    return source[:start] + patched_function + source[end:]


def patch_edge_zscore(source: str) -> str:
    """
    Fix IV-RV z-score calculation.

    Current edge is calculated in percentage points while the
    DataManager history is stored as decimal volatility. History is
    converted to percentage points before comparison.
    """
    start, end, function_source = find_function(
        source,
        "_module_edge",
    )

    if "_spread_history = [" not in function_source:
        old = (
            "        _spread_history = list("
            "self.dm.iv_rv_spread_history)"
        )

        new = (
            "        # Current edge is in percentage points.\n"
            "        # Stored history is decimal volatility.\n"
            "        # Normalize history before z-scoring.\n"
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

    if "_min_edge_std_pp" not in function_source:
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
                "Could not find Edge standard-deviation "
                "condition inside _module_edge()"
            )

        function_source = function_source.replace(
            old,
            new,
            1,
        )

    return source[:start] + function_source + source[end:]


def patch_iv_rank_fallback(source: str) -> str:
    """
    Remove unguarded IV-rank assignment inside _select_strategy().
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
    Ensure each position leg uses its own expiry chain.
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
    Set historical NIFTY 30-minute candle refresh to 1,800 seconds.
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

    if "1800" in match.group(0):
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
    Add a configurable minimum standard deviation for Edge z-scores.
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
        "# Minimum IV-RV spread-history standard deviation in\n"
        "# percentage points before trusting an Edge z-score.\n"
        "EDGE_MIN_SPREAD_STD_PP = 0.25"
    )

    return source.replace(
        marker,
        marker + addition,
        1,
    )


def patch_strategy_diagnostic_state(source: str) -> str:
    """
    Add operator-facing entry diagnostic state to __init__().
    """
    if "_last_entry_diagnostic" in source:
        return source

    marker = (
        "        self._last_entry_composite: Dict[str, float] = {}"
    )

    if marker not in source:
        fail(
            "Entry composite state was not found in "
            "StrategyEngine.__init__()"
        )

    addition = '''

        # Latest entry-pipeline diagnostic shown on the console.
        self._last_entry_diagnostic = {
            "stage": "IDLE",
            "strategy": "",
            "message": "No entry attempt yet",
            "expiry": "",
            "dte": None,
            "credit": None,
            "min_credit": None,
            "wing_width": None,
            "max_risk": None,
            "lots": None,
            "vix_multiplier": 1.0,
            "pretrade": "NOT_RUN",
            "execution": "NOT_RUN",
            "timestamp": "",
        }
'''

    return source.replace(
        marker,
        marker + addition,
        1,
    )


def patch_strategy_diagnostic_helper(source: str) -> str:
    """
    Add a helper that records detailed entry-stage diagnostics.
    """
    if "def _set_entry_diagnostic(" in source:
        return source

    marker = "    async def _enter_new_position("

    if marker not in source:
        fail(
            "_enter_new_position() was not found"
        )

    helper = '''    def _set_entry_diagnostic(
        self,
        stage: str,
        message: str,
        **values,
    ) -> None:
        """Record and log the latest entry decision."""
        current = getattr(
            self,
            "_last_entry_diagnostic",
            {},
        ).copy()

        current.update(values)
        current["stage"] = stage
        current["message"] = message
        current["timestamp"] = datetime.now(
            self._IST
        ).strftime("%Y-%m-%d %H:%M:%S")

        self._last_entry_diagnostic = current

        logger.info(
            "ENTRY DIAGNOSTIC | "
            f"stage={stage} | "
            f"strategy={current.get('strategy', '')} | "
            f"message={message} | "
            f"expiry={current.get('expiry', '')} | "
            f"dte={current.get('dte')} | "
            f"credit={current.get('credit')} | "
            f"min_credit={current.get('min_credit')} | "
            f"max_risk={current.get('max_risk')} | "
            f"lots={current.get('lots')} | "
            f"pretrade={current.get('pretrade')} | "
            f"execution={current.get('execution')}"
        )

'''

    return source.replace(
        marker,
        helper + marker,
        1,
    )


def patch_strategy_entry_diagnostics(source: str) -> str:
    """
    Add diagnostics to the existing entry pipeline without changing
    trade-selection or risk decisions.
    """
    start, end, function_source = find_function(
        source,
        "_enter_new_position",
    )

    if "ENTRY DIAGNOSTIC" in function_source:
        return source

    # Record strategy selection before building.
    strategy_marker = (
        "        logger.info(\n"
        "            f\"Selected: {strategy_name} \"\n"
        "            f\"regime={regime} tranche={tranche}\"\n"
        "        )"
    )

    strategy_replacement = (
        "        self._set_entry_diagnostic(\n"
        "            \"BUILDING\",\n"
        "            \"Starting strategy construction\",\n"
        "            strategy=strategy_name,\n"
        "        )\n"
        + strategy_marker
    )

    if strategy_marker in function_source:
        function_source = function_source.replace(
            strategy_marker,
            strategy_replacement,
            1,
        )

    # Record builder failure.
    build_marker = (
        "        if legs is None:\n"
        "            logger.warning("
    )

    if build_marker in function_source:
        function_source = function_source.replace(
            build_marker,
            (
                "        if legs is None:\n"
                "            self._set_entry_diagnostic(\n"
                "                \"BUILD_FAILED\",\n"
                "                \"Builder returned no legs; "
                "inspect builder log\",\n"
                "                strategy=strategy_name,\n"
                "                pretrade=\"NOT_RUN\",\n"
                "                execution=\"NOT_RUN\",\n"
                "            )\n"
                "            logger.warning("
            ),
            1,
        )

    # Record pre-trade stage.
    pretrade_marker = (
        "        if not await self._pre_trade_checks(\n"
        "            strategy_name, legs\n"
        "        ):"
    )

    if pretrade_marker in function_source:
        function_source = function_source.replace(
            pretrade_marker,
            (
                "        self._set_entry_diagnostic(\n"
                "            \"PRETRADE_CHECK\",\n"
                "            \"Strategy built; running pre-trade checks\",\n"
                "            strategy=strategy_name,\n"
                "            expiry=legs[0].expiry if legs else \"\",\n"
                "            credit=meta.get(\n"
                "                \"net_credit\",\n"
                "                meta.get(\"total_credit\"),\n"
                "            ),\n"
                "            max_risk=meta.get(\"max_risk\"),\n"
                "        )\n"
                "\n"
                "        if not await self._pre_trade_checks(\n"
                "            strategy_name, legs\n"
                "        ):"
            ),
            1,
        )

    # Record pre-trade failure.
    pretrade_failure_marker = (
        "            logger.info(\n"
        "                f\"Pre-trade failed: {strategy_name}\"\n"
        "            )"
    )

    if pretrade_failure_marker in function_source:
        function_source = function_source.replace(
            pretrade_failure_marker,
            (
                "            self._set_entry_diagnostic(\n"
                "                \"PRETRADE_FAILED\",\n"
                "                \"Pre-trade checks rejected the strategy\",\n"
                "                strategy=strategy_name,\n"
                "                pretrade=\"FAILED\",\n"
                "                execution=\"NOT_RUN\",\n"
                "            )\n"
                + pretrade_failure_marker
            ),
            1,
        )

    # Record zero-lot rejection.
    lots_marker = (
        "        if lots < 1:\n"
        "            logger.info(\n"
        "                f\"Lot size=0 for {strategy_name} — skip\"\n"
        "            )"
    )

    if lots_marker in function_source:
        function_source = function_source.replace(
            lots_marker,
            (
                "        if lots < 1:\n"
                "            self._set_entry_diagnostic(\n"
                "                \"LOTS_ZERO\",\n"
                "                \"Lot sizing returned zero; trade skipped\",\n"
                "                strategy=strategy_name,\n"
                "                lots=lots,\n"
                "                vix_multiplier=getattr(\n"
                "                    self, \"_last_vix_mult\", 1.0\n"
                "                ),\n"
                "                pretrade=\"PASSED\",\n"
                "                execution=\"NOT_RUN\",\n"
                "            )\n"
                "            logger.info(\n"
                "                f\"Lot size=0 for {strategy_name} — skip\"\n"
                "            )"
            ),
            1,
        )

    # Record execution start.
    execution_marker = (
        "        success = await self._execute_strategy(\n"
        "            strategy_name, legs, meta, trade_id=trade_id\n"
        "        )"
    )

    if execution_marker in function_source:
        function_source = function_source.replace(
            execution_marker,
            (
                "        self._set_entry_diagnostic(\n"
                "            \"EXECUTING\",\n"
                "            \"Pre-trade checks passed; sending orders\",\n"
                "            strategy=strategy_name,\n"
                "            expiry=new_expiry,\n"
                "            credit=meta.get(\n"
                "                \"net_credit\",\n"
                "                meta.get(\"total_credit\"),\n"
                "            ),\n"
                "            max_risk=meta.get(\"max_risk\"),\n"
                "            lots=lots,\n"
                "            vix_multiplier=getattr(\n"
                "                self, \"_last_vix_mult\", 1.0\n"
                "            ),\n"
                "            pretrade=\"PASSED\",\n"
                "            execution=\"STARTED\",\n"
                "        )\n"
                "\n"
                + execution_marker
            ),
            1,
        )

    # Record execution failure.
    execution_failure_marker = (
        "        if not success:\n"
        "            logger.warning(\n"
        "                f\"Execution failed: {strategy_name}\"\n"
        "            )"
    )

    if execution_failure_marker in function_source:
        function_source = function_source.replace(
            execution_failure_marker,
            (
                "        if not success:\n"
                "            self._set_entry_diagnostic(\n"
                "                \"EXECUTION_FAILED\",\n"
                "                \"Order execution returned failure\",\n"
                "                strategy=strategy_name,\n"
                "                pretrade=\"PASSED\",\n"
                "                execution=\"FAILED\",\n"
                "            )\n"
                "            logger.warning(\n"
                "                f\"Execution failed: {strategy_name}\"\n"
                "            )"
            ),
            1,
        )

    # Record successful fill.
    fill_marker = (
        "        self._refresh_leg_greeks(legs)\n"
        "\n"
        "        position = self._create_position_record("
    )

    if fill_marker in function_source:
        function_source = function_source.replace(
            fill_marker,
            (
                "        self._set_entry_diagnostic(\n"
                "            \"FILLED\",\n"
                "            \"All legs filled; position created\",\n"
                "            strategy=strategy_name,\n"
                "            expiry=new_expiry,\n"
                "            credit=meta.get(\n"
                "                \"net_credit\",\n"
                "                meta.get(\"total_credit\"),\n"
                "            ),\n"
                "            max_risk=meta.get(\"max_risk\"),\n"
                "            lots=lots,\n"
                "            vix_multiplier=getattr(\n"
                "                self, \"_last_vix_mult\", 1.0\n"
                "            ),\n"
                "            pretrade=\"PASSED\",\n"
                "            execution=\"FILLED\",\n"
                "        )\n"
                "\n"
                + fill_marker
            ),
            1,
        )

    return source[:start] + function_source + source[end:]


def patch_console(source: str) -> str:
    """
    Add the latest entry-pipeline state to main.py's module-level
    _display_console() function.
    """
    start, end, function_source = find_function(
        source,
        "_display_console",
    )

    if "LAST ENTRY DIAGNOSTIC" in function_source:
        return source

    # W is defined later in _display_console(), so insert the block
    # after the W assignment and before the first console output.
    marker = "    W = 72\n"

    if marker not in function_source:
        fail(
            "Console width marker was not found inside "
            "_display_console()"
        )

    block = r'''    entry_diag = getattr(
        se,
        "_last_entry_diagnostic",
        {},
    )

    print("\u2500" * W)
    print(" LAST ENTRY DIAGNOSTIC")
    print("\u2500" * W)
    print(
        f" Stage              : "
        f"{entry_diag.get('stage', 'N/A')}"
    )
    print(
        f" Strategy           : "
        f"{entry_diag.get('strategy') or 'N/A'}"
    )
    print(
        f" Message            : "
        f"{entry_diag.get('message') or 'N/A'}"
    )
    print(
        f" Expiry             : "
        f"{entry_diag.get('expiry') or 'N/A'}"
    )
    print(
        f" DTE                : "
        f"{entry_diag.get('dte') if entry_diag.get('dte') is not None else 'N/A'}"
    )
    print(
        f" Credit             : "
        f"{entry_diag.get('credit') if entry_diag.get('credit') is not None else 'N/A'}"
    )
    print(
        f" Minimum credit     : "
        f"{entry_diag.get('min_credit') if entry_diag.get('min_credit') is not None else 'N/A'}"
    )
    print(
        f" Wing width         : "
        f"{entry_diag.get('wing_width') if entry_diag.get('wing_width') is not None else 'N/A'}"
    )
    print(
        f" Max risk           : "
        f"{entry_diag.get('max_risk') if entry_diag.get('max_risk') is not None else 'N/A'}"
    )
    print(
        f" Lots               : "
        f"{entry_diag.get('lots') if entry_diag.get('lots') is not None else 'N/A'}"
    )
    print(
        f" VIX multiplier     : "
        f"{entry_diag.get('vix_multiplier', 1.0):.3f}"
    )
    print(
        f" Pre-trade          : "
        f"{entry_diag.get('pretrade', 'N/A')}"
    )
    print(
        f" Execution          : "
        f"{entry_diag.get('execution', 'N/A')}"
    )
    print(
        f" Timestamp          : "
        f"{entry_diag.get('timestamp') or 'N/A'}"
    )

'''

    function_source = function_source.replace(
        marker,
        marker + block,
        1,
    )

    return source[:start] + function_source + source[end:]


def apply_patch() -> None:
    original = {
        filename: read_file(filename)
        for filename in FILES
    }

    patched = dict(original)

    # Required runtime fixes.
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

    patched["regime_engine.py"] = (
        patch_edge_zscore(
            patched["regime_engine.py"]
        )
    )

    # Safe accounting/data fixes.
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

    patched["data_manager.py"] = (
        patch_synthetic_edge_history(
            patched["data_manager.py"]
        )
    )

    patched["config.py"] = patch_config(
        patched["config.py"]
    )

    patched["config.py"] = patch_edge_config(
        patched["config.py"]
    )

    # Detailed operator diagnostics.
    patched["strategy_engine.py"] = (
        patch_strategy_diagnostic_state(
            patched["strategy_engine.py"]
        )
    )

    patched["strategy_engine.py"] = (
        patch_strategy_diagnostic_helper(
            patched["strategy_engine.py"]
        )
    )

    patched["strategy_engine.py"] = (
        patch_strategy_entry_diagnostics(
            patched["strategy_engine.py"]
        )
    )

    patched["main.py"] = patch_console(
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
            "The patch is already applied."
        )
        return

    # Validate every file before touching originals.
    for filename in FILES:
        validate_python(
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

            validate_python(
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
        "Detailed entry diagnostics were added to the console."
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