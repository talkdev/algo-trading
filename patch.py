#!/usr/bin/env python3
"""v7: the per-trade console report, and the money bugs found building it.

    python3 patch_v7.py                 # applies to the directory it lives in
    python3 patch_v7.py /path/to/repo   # or to an explicit checkout

Expects the tree patch_v6.py leaves behind (its MD5 table is the gate) and
lands it on the tested v7 content. All-or-nothing: every hunk is staged in
memory, the result is hash-checked per file and compiled, and the tree is
reverted if anything fails. Re-running is a no-op.

Exit codes: 0 applied or already up to date, 2 wrong directory,
3 unrecognised content / anchor drift (nothing modified),
4 compile failure (reverted).

────────────────────────────────────────────────────────────────────────────
WHAT IT ADDS: one console block per trade, per cycle, from BOTH engines
────────────────────────────────────────────────────────────────────────────
The live/paper engine prints it at the end of every cycle (after the cycle
footer) and the replay harness prints it at the end of every replayed cycle,
for every trade of the session - open or closed:

====================================================================================
Trade-1
Strategy: BULL_PUT_SPREAD
----------------------------------------------------------------------------
Trade Start Data: time: 09:20
Sold: 2 lot of PE with premium: 47.65 at strike: 23150
Bought: 2 lot of PE with premium: 28.60 at strike: 23050
----------------------------------------------------------------------------
Trade End Data: time: 11:05
Bought: 2 lot of PE with premium: 40.10 at strike: 23150
Sold: 2 lot of PE with premium: 24.60 at strike: 23050
Closed - CLOSE_TARGET
----------------------------------------------------------------------------
Position Status: Close
Total Investment: Rs 15,730.00 margin held (credit received Rs 2,476.50)
Total Profit: Rs +348.14 realised (gross Rs +461.50 - charges Rs 113.36)
====================================================================================

While the trade is open the same block ends with the live mark instead:

Trade End Data: time: 10:15
Bought: 2 lot of PE with premium: 43.20 at strike: 23150
Sold: 2 lot of PE with premium: 26.10 at strike: 23050
Open
----------------------------------------------------------------------------
Position Status: Open
Total Investment: Rs 15,730.00 margin held (credit received Rs 2,476.50)
Total Profit: Rs +227.50 unrealised (gross Rs +285.10 - charges Rs 57.67 paid)
====================================================================================

Rules the block obeys, all of them enforced by a self-test in each engine:

  * It is rendered from the persisted book (positions / position_legs) plus
    the current option chain - never from a parallel calculation - so it
    cannot disagree with what the engine actually did. When a freshly closed
    trade's own numbers cannot be reproduced from the book, the block says
    so on the Total Profit line instead of printing a confident wrong figure.
  * "Sold"/"Bought" is the action at ENTRY for the start block and the
    action at EXIT for the end block (a leg sold at entry is bought back).
  * Premiums are the fills. Quoted mid is what they are measured against.
  * Total Investment is the capital the trade actually ties up: the margin
    held for a seller, the premium paid for a buyer, plus the credit or
    debit received/paid, stated on its own line.
  * Total Profit is gross minus the charges actually booked, labelled
    realised once the position is closed and unrealised while it is open.
  * Open positions are reprinted every cycle in "each_cycle" mode (the
    default); "on_change" reprints only when a trade opens, closes or its
    mark moves by TRADE_REPORT_MARK_EPS; "off" prints nothing. Config:
    TRADE_REPORT_ENABLED / TRADE_REPORT_MODE / TRADE_REPORT_MARK_EPS /
    TRADE_REPORT_MAX_PER_CYCLE, and `backtest_engine.py --trade-report=`.

────────────────────────────────────────────────────────────────────────────
WHAT IT FIXES
────────────────────────────────────────────────────────────────────────────
1. A trade was settled on the PLAN, not on the FILLS, and its entry charges
   were taken twice. StrategyEngine books entry_credit as the planned net
   credit (gross minus an estimated slippage minus the entry charges in
   points); execute_close() settled gross P&L against that figure and then
   subtracted the same entry charges again in rupees. It now settles on
   realised_entry_credit() - the legs' own fills - and keeps the planned
   figure beside it for comparison (positions.entry_credit_realised).
   Measured on the 2026-09-11 paper book, using this patch's own code:
       trade a99b921f  net Rs   286.68 -> Rs   348.14
       trade a3fcdf68  net Rs   804.26 -> Rs   868.64
       day             net Rs 1,090.94 -> Rs 1,216.78   (+Rs 125.84, +11.5%)
   The day's profit was understated by an eighth, and the same error fed the
   daily loss breaker, the equity curve and the EOD report.

2. Exit charges were computed with the ENTRY side of each leg.
   _compute_transaction_costs() accepted `action` and ignored it, so on exit
   it charged STT to the buy-backs (which pay no STT) and stamp duty to the
   sales (which pay no stamp). An exit is the mirror image of the entry, and
   it is now treated as one. StrategyEngine._compute_costs() and the replay
   harness already did this, so the live book and the replay disagreed on the
   same trade (Rs 2.27 on a 4-lot bear call spread: STT Rs 3.12 charged where
   Rs 0.78 was due).

3. GST was levied on STT and on stamp duty. The 18% goods-and-services tax
   applies to brokerage, exchange transaction charges and the SEBI turnover
   fee - not to statutory levies the broker merely collects. Both the
   per-side computation and _round_trip_cost_pts() are corrected, which
   lowers the friction the minimum-credit and EV gates demand.

4. trade_exits was permanently empty. execute_close() has written exit_adx
   and exit_vwap_dist since it was first drafted; no schema declared either
   column, so the INSERT raised "table trade_exits has no column named
   exit_adx" on every close and the exception was swallowed as a warning.
   The 2026-09-11 book holds 2 CLOSED positions and 0 trade_exits rows, and
   every query that joins trade_exits (EOD stats, exit-quality analysis,
   pnl_15min_after_exit) silently returned nothing. The columns are declared
   in SCHEMA_SQL, added by MIGRATION_SQL and ensured at engine start, so an
   existing book is repaired on the next run.

5. exit_slippage was always 0.000. The quoted mid at exit was computed and
   written to position_legs but never added to the audit dict the slippage
   sum is built from, so the filter dropped every leg. The one number that
   says whether exits are being taken at fair value was permanently zero; it
   is now measured (0.100 pts on the reproduced 2026-09-11 trade).

6. The replay harness threw its exit audit away. _close() flagged a position
   closed and kept the money in memory, but never persisted exit_time,
   exit_reason, exit_priority, exit_premium, gross/exit-cost/net P&L, the
   liquidation mark, or the legs' exit_price and quoted_mid_at_exit - and
   _open() never persisted estimated_margin. A replayed session therefore
   could not answer "what did this trade do", which is exactly what the
   console block needs; both engines now read the same persisted shape.

7. Live-engine day handling. The per-cycle report needed a session latch
   that survives the day roll and dies with it, the unrealised-P&L and
   daily-loss-halt denominators were being taken after the day's own losses
   had already reduced them (measuring the breaker against a moving base),
   and the square-off watchdog judged feed staleness against a clock that
   did not start at today's session open - so an engine started after hours
   or left running overnight could act on yesterday's liveness.

8. eod_report.py had drifted from the engine it reports on: LOT_SIZE 75
   against a configured 65, exchange transaction 0.0003552 against
   0.0003553, STT 0.10% against 0.15%, a 5% daily-loss line against the
   configured 4% - and "cost per lot" divided the day's charges by the lot
   size rather than by the number of lots actually traded.

9. STT: Budget 2026 raised securities-transaction tax on the sale of an
   option from 0.10% to 0.15% of premium (and on exercise from 0.125% to
   0.15%) for transactions on or after 1 April 2026. The engine was costing
   every 2026 trade at the superseded rate, understating the single largest
   statutory charge on a short-premium book by a third. That is not a
   rounding difference: it feeds _round_trip_friction, the minimum-credit
   gate, the EV gate, lot sizing and net P&L, so trades were being approved
   against a tax that no longer exists. STT_OPTIONS_SELL / STT_OPTIONS_EXERCISE
   in env.txt override it when replaying a session that settled under 0.10%.

────────────────────────────────────────────────────────────────────────────
WHAT IT DOES NOT TOUCH
────────────────────────────────────────────────────────────────────────────
strategy_engine.py is in the target list only so the patch can prove the
file is the tested v6 content - it carries no hunks. No signal, threshold,
gate, window or tuned constant moves anywhere, and the replay harness's fill
model is unchanged, so the same trades are taken on the same data. The only
replay number that moves is the cost line, and only because of item 9:

    2026-09-08   Rs 1,047 -> Rs 1,042
    2026-09-09   Rs 1,133 -> Rs 1,121
    2026-09-10   Rs 1,800 -> Rs 1,791
    2026-09-11   Rs 6,753 -> Rs 6,731
    total       Rs 10,733 -> Rs 10,685     (-Rs 48, -0.45%)

Verified after applying: `python3 verify_all.py` passes 7/7, including
`backtest_engine.py --test` (which now renders the block for a forced round
trip in both states and asserts the printed money equals the booked money)
and the execution engine's self-test (which reproduces the 2026-09-11
BULL_PUT_SPREAD through the real paper executor and asserts the settlement,
the exit charges, the trade_exits row and the slippage measurement).

Every changed file compiles; nothing here needs a new dependency, a schema
rebuild or a config migration - missing columns are added on start.
"""
import hashlib
import os
import py_compile
import sys
import tempfile

TARGETS = (
    'core.py',
    'strategy_engine.py',
    'execution_engine.py',
    'main.py',
    'backtest_engine.py',
    'eod_report.py',
)

MD5 = {
    'core.py': {
        "v6":    'facc185b6578de82347a4a5be944469d',
        "final": '67028da3ba47d852ef623100b1e39837',
    },
    'strategy_engine.py': {
        "v6":    '0d1e6a929d9a8e9510495d4e6616add4',
        "final": '0d1e6a929d9a8e9510495d4e6616add4',
    },
    'execution_engine.py': {
        "v6":    '261f782dbc1ec1831e84728c34446846',
        "final": '698975365c48d31ff9743bb3c8d54156',
    },
    'main.py': {
        "v6":    'b30fed6a955941d066944ceb4f695218',
        "final": '4a7da00110beaba57b19ab55ef5397f8',
    },
    'backtest_engine.py': {
        "v6":    'f87c4135a9fda63363f9591e4f2d9202',
        "final": 'b89eecce9d247f9d09e793232c67cd18',
    },
    'eod_report.py': {
        "v6":    '7b85d79a579a5284cb7f0e4e537c2418',
        "final": '971154b9ea813e185a792c8bea560f5a',
    },
}

# Printed after a successful run: what the tree now does that it did not.
CHANGES = (
    "per-trade console block, every cycle, from the live AND the replay engine",
    "trades settle on the fills, not the plan; entry charges no longer doubled",
    "exit charges computed on the exit side of each leg (STT/stamp were swapped)",
    "GST no longer levied on STT and stamp duty",
    "trade_exits rows are written again (exit_adx / exit_vwap_dist declared)",
    "exit_slippage measured instead of permanently 0.000",
    "replay harness persists its exit audit and its estimated margin",
    "live day-roll latches, halt denominator and watchdog clock corrected",
    "eod_report.py constants and per-lot cost aligned with the engine",
    "STT on option sales 0.10% -> 0.15% (Budget 2026, from 1 April 2026)",
)

PLAN_V6 = [
    (
        'core.py',
        [
            '    alert_min_interval_sec:        float = 3.0\n    alert_timeout_sec:             float = 5.0\n\n    def __repr__(self) -> str:\n        def mask(s: str) -> str:\n            if not s:\n',
        ],
        '    alert_min_interval_sec:        float = 3.0\n    alert_timeout_sec:             float = 5.0\n\n    # ── v7: per-trade console lifecycle report ──────────────────────\n    # One block per trade, on the console, every cycle, from BOTH the\n    # live/paper engine and the replay harness: what was bought and sold\n    # at entry, what the position is worth now (or was closed at), and\n    # the money. The block is rendered from the persisted book\n    # (positions / position_legs) plus the current chain, never from a\n    # parallel calculation, so it cannot drift from what the engine\n    # actually did.\n    trade_report_enabled:          bool  = True\n    # each_cycle -> every trade of the session, every cycle. This is the\n    #               requested behaviour and the loudest one: a trade that\n    #               closed at 11:00 is still reprinted at 15:00.\n    # on_change  -> a trade is reprinted only when it opened, closed, or\n    #               its unrealised P&L moved by trade_report_mark_eps.\n    # off        -> nothing is printed.\n    trade_report_mode:             str   = "each_cycle"\n    trade_report_mark_eps:         float = 25.0\n    # 0 = no cap. A cap keeps a many-trade session readable; the newest\n    # trades are the ones printed when the cap bites.\n    trade_report_max_per_cycle:    int   = 0\n\n    def __repr__(self) -> str:\n        def mask(s: str) -> str:\n            if not s:\n',
    ),
    (
        'core.py',
        [
            '        nifty_strike_step=_get_int(env, "NIFTY_STRIKE_STEP", 50),\n\n        # Costs\n        stt_options_sell=_get_float(env, "STT_OPTIONS_SELL", 0.001),\n        stt_options_exercise=_get_float(env, "STT_OPTIONS_EXERCISE", 0.00125),\n        brokerage_per_order=_get_float(env, "BROKERAGE_PER_ORDER", 20.0),\n        exchange_txn_rate=_get_float(env, "EXCHANGE_TXN_RATE", 0.0003553),\n        sebi_rate=_get_float(env, "SEBI_RATE", 0.000001),\n',
        ],
        '        nifty_strike_step=_get_int(env, "NIFTY_STRIKE_STEP", 50),\n\n        # Costs\n        # v7: STT on the SALE of an option in securities is 0.15% of the\n        # premium for transactions on/after 1 April 2026 (Budget 2026,\n        # clause 143 of the Finance Bill 2026: 0.10% -> 0.15% on premium,\n        # 0.125% -> 0.15% on exercise; futures went 0.02% -> 0.05%). The\n        # engine had been costing every 2026 trade at the superseded rate,\n        # so the single largest statutory charge on a short-premium book\n        # was understated by a third. That is not a rounding difference:\n        # it feeds _round_trip_friction, the minimum-credit gate, the EV\n        # gate, lot sizing and net P&L, so trades were being approved\n        # against a tax that no longer exists. Override STT_OPTIONS_SELL\n        # in env.txt when replaying a session that settled under 0.10%.\n        stt_options_sell=_get_float(env, "STT_OPTIONS_SELL", 0.0015),\n        stt_options_exercise=_get_float(env, "STT_OPTIONS_EXERCISE", 0.0015),\n        brokerage_per_order=_get_float(env, "BROKERAGE_PER_ORDER", 20.0),\n        exchange_txn_rate=_get_float(env, "EXCHANGE_TXN_RATE", 0.0003553),\n        sebi_rate=_get_float(env, "SEBI_RATE", 0.000001),\n',
    ),
    (
        'core.py',
        [
            '        ),\n        alert_timeout_sec=min(\n            max(_get_float(env, "ALERT_TIMEOUT_SEC", 5.0), 1.0), 30.0\n        ),\n    )\n\n',
        ],
        '        ),\n        alert_timeout_sec=min(\n            max(_get_float(env, "ALERT_TIMEOUT_SEC", 5.0), 1.0), 30.0\n        ),\n        # ── v7 per-trade console report ───────────────────────────────────\n        trade_report_enabled=_get_bool(env, "TRADE_REPORT_ENABLED", True),\n        trade_report_mode=_get_choice(\n            env, "TRADE_REPORT_MODE", "each_cycle",\n            ("each_cycle", "on_change", "off"),\n        ),\n        trade_report_mark_eps=min(\n            max(_get_float(env, "TRADE_REPORT_MARK_EPS", 25.0), 0.0), 100000.0\n        ),\n        trade_report_max_per_cycle=min(\n            max(_get_int(env, "TRADE_REPORT_MAX_PER_CYCLE", 0), 0), 500\n        ),\n    )\n\n',
    ),
    (
        'core.py',
        [
            '    exit_priority_name      TEXT,\n    exit_spot               REAL,\n    exit_vix                REAL,\n    exit_legs_json          TEXT,\n    exit_premium            REAL,\n    gross_pnl_pts           REAL,\n',
        ],
        '    exit_priority_name      TEXT,\n    exit_spot               REAL,\n    exit_vix                REAL,\n    -- v7: ExecutionEngine.execute_close() has written these two since it was\n    -- first drafted, but no schema declared them, so the whole INSERT failed\n    -- with "table trade_exits has no column named exit_adx" and was swallowed\n    -- as a warning. The exit audit table stayed permanently empty (the\n    -- 2026-09-11 paper book: 2 CLOSED positions, 0 trade_exits rows), which\n    -- also blanked every query that joins trade_exits.\n    exit_adx                REAL,\n    exit_vwap_dist          REAL,\n    exit_legs_json          TEXT,\n    exit_premium            REAL,\n    gross_pnl_pts           REAL,\n',
    ),
    (
        'core.py',
        [
            '    "ALTER TABLE trade_exits ADD COLUMN exit_priority INTEGER",\n    "ALTER TABLE trade_exits ADD COLUMN exit_priority_name TEXT",\n    "ALTER TABLE trade_exits ADD COLUMN pnl_15min_after_exit REAL",\n    "ALTER TABLE session_state ADD COLUMN opening_straddle_pts REAL DEFAULT 0",\n    "ALTER TABLE session_state ADD COLUMN prev_day_vix_close REAL",\n    "ALTER TABLE session_state ADD COLUMN gap_direction TEXT DEFAULT \'FLAT\'",\n',
        ],
        '    "ALTER TABLE trade_exits ADD COLUMN exit_priority INTEGER",\n    "ALTER TABLE trade_exits ADD COLUMN exit_priority_name TEXT",\n    "ALTER TABLE trade_exits ADD COLUMN pnl_15min_after_exit REAL",\n    # v7: the two columns execute_close() has always written but no schema\n    # ever declared - their absence made every trade_exits INSERT fail.\n    "ALTER TABLE trade_exits ADD COLUMN exit_adx REAL",\n    "ALTER TABLE trade_exits ADD COLUMN exit_vwap_dist REAL",\n    "ALTER TABLE session_state ADD COLUMN opening_straddle_pts REAL DEFAULT 0",\n    "ALTER TABLE session_state ADD COLUMN prev_day_vix_close REAL",\n    "ALTER TABLE session_state ADD COLUMN gap_direction TEXT DEFAULT \'FLAT\'",\n',
    ),
    (
        'core.py',
        [
            '\n\n# ─────────────────────────────────────────────\n# SELF TEST\n# ─────────────────────────────────────────────\n\n',
        ],
        '\n\n# ─────────────────────────────────────────────\n# v7 — PER-TRADE CONSOLE REPORT\n# ─────────────────────────────────────────────\n#\n# One block per trade, on the console, every cycle, from BOTH engines\n# (main.py for live/paper, backtest_engine.py for replay). The layout is\n# the operator\'s, fixed:\n#\n#   ====================================================================\n#   Trade-<n>\n#   Strategy: <name>\n#   --------------------------------------------------------------------\n#   Trade Start Data: time: <HH:MM>\n#   Bought/Sold: <n> lot of CE with premium: <n> at strike: <n>\n#   --------------------------------------------------------------------\n#   Trade End Data: time: <HH:MM>\n#   Bought/Sold: <n> lot of CE with premium: <n> at strike: <n>\n#   Open\n#   --------------------------------------------------------------------\n#   Position Status: Open/Close\n#   Total Investment:\n#   Total Profit:\n#   ====================================================================\n#\n# The numbers are read out of the persisted book (positions /\n# position_legs) and marked with the same chain the engine is trading on,\n# so the block is an audit of what happened rather than a second opinion\n# about it. Where a value has to be derived, its basis is printed next to\n# it - a rupee figure with no stated basis is how accounting arguments\n# start.\n#\n#   Total Investment = cash the trade had to put up, plus the entry\n#       charges already paid. A net-debit structure pays premium, so the\n#       basis is the premium paid. A net-credit structure RECEIVES\n#       premium and posts margin instead, so the basis is the margin the\n#       broker blocks (the engine\'s own estimate where it has one, the\n#       structure\'s maximum loss where it does not).\n#\n#   Total Profit = net P&L, on realised fills both sides.\n#       Closed: (realised entry credit - realised exit debit) x lot size\n#           x lots, minus every charge actually booked.\n#       Open: the same identity marked at LIQUIDATION value - shorts at\n#           the ask, longs at the bid, i.e. what closing now would cost -\n#           less the entry charges already paid and an estimate of the\n#           charges still to pay. Conservative by construction, and the\n#           same convention MainEngine.compute_unrealized_pnl uses, so the\n#           open figure and the figure the close eventually books cannot\n#           disagree by a change of mark.\n\nTRADE_REPORT_RULE = "=" * 84\nTRADE_REPORT_SUB  = "-" * 76\n\n\ndef _tr_num(value, default: float = 0.0) -> float:\n    """float(value) that cannot raise on a NULL column."""\n    try:\n        if value is None or value == "":\n            return float(default)\n        return float(value)\n    except (TypeError, ValueError):\n        return float(default)\n\n\ndef _tr_int(value, default: int = 0) -> int:\n    try:\n        if value is None or value == "":\n            return int(default)\n        return int(float(value))\n    except (TypeError, ValueError):\n        return int(default)\n\n\ndef _tr_money(value) -> str:\n    return f"Rs {_tr_num(value):,.2f}"\n\n\ndef _tr_signed(value) -> str:\n    return f"Rs {_tr_num(value):+,.2f}"\n\n\ndef _tr_price(value) -> str:\n    """A premium: 2 decimals, and \'n/a\' rather than 0.00 for a missing fill."""\n    if value is None or value == "":\n        return "n/a"\n    return f"{_tr_num(value):.2f}"\n\n\ndef _tr_strike(value) -> str:\n    """25600.0 -> \'25600\'; 25612.5 -> \'25612.50\'."""\n    if value is None or value == "":\n        return "n/a"\n    k = _tr_num(value)\n    return f"{k:.0f}" if abs(k - round(k)) < 1e-9 else f"{k:.2f}"\n\n\ndef _tr_side(action) -> str:\n    """SELL -> \'Sold\', BUY -> \'Bought\'. Past tense: the fill happened."""\n    a = str(action or "").strip().upper()\n    if a.startswith("B"):\n        return "Bought"\n    if a.startswith("S"):\n        return "Sold"\n    return a.title() or "Traded"\n\n\ndef _tr_closing_side(action) -> str:\n    """The side that CLOSES a leg: a short is bought back, a long is sold."""\n    a = str(action or "").strip().upper()\n    if a.startswith("S"):\n        return "Bought"\n    if a.startswith("B"):\n        return "Sold"\n    return _tr_side(action)\n\n\ndef _tr_symbol(option_type) -> str:\n    t = str(option_type or "").strip().upper()\n    if t.startswith("C"):\n        return "CE"\n    if t.startswith("P"):\n        return "PE"\n    return t or "OPT"\n\n\ndef _tr_hhmm(value) -> str:\n    """Anything the book stores as a time -> \'HH:MM\'."""\n    if value is None or value == "":\n        return "n/a"\n    if isinstance(value, datetime):\n        return value.strftime("%H:%M")\n    if isinstance(value, dtime):\n        return value.strftime("%H:%M")\n    parsed = parse_ist_timestamp(value)\n    if parsed is not None:\n        return parsed.strftime("%H:%M")\n    tail = str(value).strip().split("T")[-1]\n    parts = tail.split(":")\n    if len(parts) >= 2 and parts[0].isdigit():\n        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"\n    return str(value)[:5]\n\n\ndef realised_entry_credit(position: dict, legs: List[dict]) -> Tuple[float, str]:\n    """The premium the ENTRY FILLS actually booked, in points, plus its basis.\n\n    positions.entry_credit is the strategy engine\'s PLANNED net credit:\n    gross credit minus an ESTIMATED slippage and the entry charges\n    expressed in points (StrategyEngine: net_credit = gross_credit -\n    total_slippage - entry_costs_pts). positions.gross_credit is the same\n    figure before those deductions. Neither one is what the fills did.\n\n    Settling a trade against the planned figure does two wrong things at\n    once: it books a modelled slippage as though it had happened, and it\n    removes the entry charges in points before execute_close() removes\n    them again in rupees. On the 2026-09-11 paper book a BULL_PUT_SPREAD\n    was booked at +Rs 286.68 whose own fills say +Rs 347.78 - Rs 57.67 of\n    entry charges counted twice and Rs 3.38 of estimated slippage counted\n    as real, i.e. the day was reported Rs 125 (10%) worse than it was.\n\n    The fills are ground truth and they are already persisted leg by leg,\n    so the realised credit is sum(SELL entry_price) - sum(BUY entry_price).\n    It is only used when EVERY leg carries a price: a partially priced\n    book would silently omit a leg, which is worse than either stored\n    number. The basis string is returned so the caller can log which of\n    the three was used.\n    """\n    rows = list(legs or [])\n    if not rows:\n        for key in ("gross_credit", "entry_credit"):\n            value = (position or {}).get(key)\n            if value is not None and value != "":\n                return _tr_num(value), key\n        return 0.0, "none"\n\n    filled = 0.0\n    priced = 0\n    for leg in rows:\n        price = leg.get("entry_price")\n        if price is None or price == "":\n            continue\n        px = _tr_num(price)\n        if px <= 0:\n            continue\n        filled += px if str(leg.get("action") or "").upper().startswith("S") else -px\n        priced += 1\n\n    if priced == len(rows):\n        return filled, "fills"\n    for key in ("gross_credit", "entry_credit"):\n        value = (position or {}).get(key)\n        if value is not None and value != "":\n            return _tr_num(value), f"{key}_legs_unpriced"\n    return filled, "partial_fills"\n\n\ndef realised_exit_debit(position: dict, legs: List[dict]) -> Tuple[float, str]:\n    """The points it actually cost to close: shorts bought back (+), longs\n    sold (-). Same rule as realised_entry_credit - the leg fills are the\n    record, positions.exit_premium is the fallback."""\n    rows = list(legs or [])\n    if rows:\n        total = 0.0\n        priced = 0\n        for leg in rows:\n            price = leg.get("exit_price")\n            if price is None or price == "":\n                continue\n            px = _tr_num(price)\n            total += px if str(leg.get("action") or "").upper().startswith("S") else -px\n            priced += 1\n        if priced == len(rows):\n            return total, "fills"\n    value = (position or {}).get("exit_premium")\n    if value is not None and value != "":\n        return _tr_num(value), "positions.exit_premium"\n    return 0.0, "none"\n\n\nclass TradeConsoleReporter:\n    """Renders the per-trade lifecycle block for one engine, live or replay.\n\n    Deliberately dumb about trading and strict about arithmetic: it reads\n    the book, marks it with the chain it is handed, and prints. It owns no\n    state except what it printed last (for the quiet mode) and it is not\n    allowed to raise into a trading loop - every query and every render is\n    guarded, because a reporting bug must never become a trading failure.\n    """\n\n    MODES = ("each_cycle", "on_change", "off")\n\n    def __init__(self, db: "Database", config: Config, logger=None,\n                 source: str = "LIVE"):\n        self.db      = db\n        self.config  = config\n        self.logger  = logger\n        self.source  = str(source or "LIVE")\n        # position_id -> (state, last printed profit); on_change only\n        self._last: Dict[str, Tuple[str, float]] = {}\n        self._day: Optional[str] = None\n        self._mode_override: Optional[str] = None\n\n    # ── configuration ────────────────────────────────────────────────\n\n    @property\n    def mode(self) -> str:\n        # An explicit override wins over the configuration, including over\n        # TRADE_REPORT_ENABLED: it is the operator asking for this particular\n        # run (backtest --trade-report), and the harness self-test relies on\n        # it to be deterministic whatever env.txt happens to say.\n        if self._mode_override in self.MODES:\n            return self._mode_override\n        if not bool(getattr(self.config, "trade_report_enabled", True)):\n            return "off"\n        chosen = getattr(self.config, "trade_report_mode", "each_cycle")\n        chosen = str(chosen or "each_cycle").strip().lower()\n        return chosen if chosen in self.MODES else "each_cycle"\n\n    def set_mode(self, mode: Optional[str]) -> None:\n        """CLI override (backtest --trade-report). Config is frozen, so the\n        override lives here; None restores the configured behaviour."""\n        if mode is None:\n            self._mode_override = None\n            return\n        mode = str(mode).strip().lower()\n        self._mode_override = mode if mode in self.MODES else None\n\n    def _debug(self, message: str) -> None:\n        try:\n            if self.logger is not None:\n                self.logger.debug(f"trade report: {message}")\n        except Exception:\n            pass\n\n    # ── book access ──────────────────────────────────────────────────\n\n    def positions_for(self, trading_date: str) -> List[dict]:\n        try:\n            rows = self.db.query(\n                "SELECT * FROM positions WHERE trading_date=? "\n                "ORDER BY entry_time ASC, created_at ASC, rowid ASC",\n                (trading_date,),\n            ) or []\n        except Exception as exc:\n            self._debug(f"positions query failed: {exc}")\n            return []\n        return list(rows)\n\n    def legs_for(self, position_id: str) -> List[dict]:\n        try:\n            rows = self.db.query(\n                "SELECT * FROM position_legs WHERE position_id=? "\n                "ORDER BY leg_id ASC",\n                (str(position_id),),\n            ) or []\n        except Exception as exc:\n            self._debug(f"legs query failed for {position_id}: {exc}")\n            return []\n        # Display order: calls then puts, strike ascending - the same order\n        # the structures are described in everywhere else in the engine.\n        def _key(leg: dict):\n            opt = str(leg.get("option_type") or "").upper()\n            return (0 if opt.startswith("C") else 1, _tr_num(leg.get("strike")))\n        return sorted(rows, key=_key)\n\n    # ── marks ────────────────────────────────────────────────────────\n\n    def _quote(self, leg: dict, chain: dict) -> dict:\n        if not chain:\n            return {}\n        node = chain.get(_tr_num(leg.get("strike"))) or {}\n        return node.get(str(leg.get("option_type") or "")) or {}\n\n    def _mark_leg(self, leg: dict, chain: dict, closing: bool = True):\n        """Price of one leg now. closing=True gives the honest liquidation\n        price - a short is bought back at the ASK, a long is sold at the\n        BID - which is the only mark a profit or an exit decision should\n        ever be made on. Returns None when the chain has nothing."""\n        quote = self._quote(leg, chain)\n        bid = _tr_num(quote.get("bid"))\n        ask = _tr_num(quote.get("ask"))\n        ltp = _tr_num(quote.get("ltp"))\n        if closing:\n            wants_ask = str(leg.get("action") or "").upper().startswith("S")\n            if wants_ask and ask > 0:\n                return ask\n            if not wants_ask and bid > 0:\n                return bid\n        if bid > 0 and ask > 0:\n            return (bid + ask) / 2.0\n        if ltp > 0:\n            return ltp\n        if bid > 0:\n            return bid\n        if ask > 0:\n            return ask\n        return None\n\n    # ── size and money ───────────────────────────────────────────────\n\n    def _lot_size(self) -> float:\n        return float(getattr(self.config, "lot_size", 0) or 0)\n\n    def _lots(self, position: dict, legs: List[dict]) -> int:\n        lots = _tr_int(position.get("final_lots"))\n        if lots > 0:\n            return lots\n        lot_size = self._lot_size()\n        derived = 0\n        if lot_size > 0:\n            for leg in legs:\n                qty = _tr_int(leg.get("qty"))\n                if qty > 0:\n                    derived = max(derived, int(round(qty / lot_size)))\n        return max(derived, 1)\n\n    def investment(self, position: dict, legs: List[dict]) -> Tuple[float, List[str]]:\n        """(rupees committed, basis lines) - see the block comment above."""\n        lot_size = self._lot_size()\n        lots     = self._lots(position, legs)\n        entry_costs = _tr_num(position.get("entry_costs_rupees"))\n\n        paid = received = 0.0\n        for leg in legs:\n            px  = _tr_num(leg.get("entry_price"))\n            qty = _tr_int(leg.get("qty")) or int(round(lot_size * lots))\n            if str(leg.get("action") or "").upper().startswith("S"):\n                received += px * qty\n            else:\n                paid += px * qty\n\n        if paid > received:\n            net_cash = paid - received\n            return net_cash + entry_costs, [\n                f"premium paid {_tr_money(net_cash)} "\n                f"+ entry charges {_tr_money(entry_costs)}",\n            ]\n\n        margin = _tr_num(position.get("estimated_margin"))\n        basis  = "margin blocked"\n        if margin <= 0:\n            margin = _tr_num(position.get("total_max_risk"))\n            basis  = "max structural risk"\n        if margin <= 0:\n            margin = abs(_tr_num(position.get("entry_credit"))) * lot_size * lots\n            basis  = "credit received"\n\n        lines = [\n            f"{basis} {_tr_money(margin)} + entry charges "\n            f"{_tr_money(entry_costs)}",\n        ]\n        if received > paid and basis != "credit received":\n            lines.append(\n                f"premium received {_tr_money(received - paid)} - a credit "\n                f"structure commits margin, not cash premium"\n            )\n        return margin + entry_costs, lines\n\n    def profit(self, position: dict, legs: List[dict],\n               chain: dict) -> Tuple[float, bool, List[str]]:\n        """(net rupees, realised?, basis lines).\n\n        Closed trades are recomputed from the persisted fills rather than\n        quoted from net_pnl_rupees, and the booked figure is named when the\n        two disagree. That is the point of the block: it shows what the\n        fills say next to what the book said.\n        """\n        lot_size = self._lot_size()\n        lots     = self._lots(position, legs)\n        units    = lot_size * lots\n        entry_costs = _tr_num(position.get("entry_costs_rupees"))\n        status   = str(position.get("status") or "OPEN").upper()\n        closed   = status.startswith("CLOSE")\n\n        entry_credit, entry_basis = realised_entry_credit(position, legs)\n\n        if closed:\n            exit_debit, exit_basis = realised_exit_debit(position, legs)\n            exit_costs = _tr_num(position.get("exit_costs_rupees"))\n            gross_pts  = entry_credit - exit_debit\n            gross_rs   = gross_pts * units\n            charges    = entry_costs + exit_costs\n            net        = gross_rs - charges\n            basis = [\n                f"gross {gross_pts:+.2f} pts x {units:,.0f} units = "\n                f"{_tr_money(gross_rs)}, charges {_tr_money(charges)}",\n                f"entry credit {entry_credit:+.2f} pts ({entry_basis}), "\n                f"exit debit {exit_debit:+.2f} pts ({exit_basis})",\n            ]\n            booked = position.get("net_pnl_rupees")\n            if booked is not None and booked != "" and \\\n                    abs(_tr_num(booked) - net) > 0.01:\n                basis.append(\n                    f"the book says {_tr_signed(booked)} - the fills above "\n                    f"are what this block reports"\n                )\n            return net, True, basis\n\n        # Open: mark every leg at what closing it now would cost.\n        liq      = position.get("last_liquidation_premium")\n        liq_src  = "positions.last_liquidation_premium"\n        if liq is None or liq == "":\n            liq = 0.0\n            for leg in legs:\n                if str(leg.get("leg_status") or "").upper() == "CLOSED":\n                    continue\n                mark = self._mark_leg(leg, chain, closing=True)\n                if mark is None:\n                    mark = _tr_num(leg.get("entry_price"))\n                if str(leg.get("action") or "").upper().startswith("S"):\n                    liq += mark\n                else:\n                    liq -= mark\n            liq_src = "chain marks" if chain else "entry prices (chain empty)"\n        liq = _tr_num(liq)\n\n        gross_pts = entry_credit - liq\n        gross_rs  = gross_pts * units\n        # The charge still to pay to get out, on the same convention\n        # MainEngine.compute_unrealized_pnl uses.\n        to_pay    = entry_costs * 0.95\n        net       = gross_rs - entry_costs - to_pay\n        basis = [\n            f"marked to exit {liq:+.2f} pts ({liq_src}) -> gross "\n            f"{gross_pts:+.2f} pts x {units:,.0f} units = {_tr_money(gross_rs)}",\n            f"charges paid {_tr_money(entry_costs)}, still to pay "\n            f"~{_tr_money(to_pay)}, entry credit {entry_credit:+.2f} pts "\n            f"({entry_basis})",\n        ]\n        return net, False, basis\n\n    # ── rendering ────────────────────────────────────────────────────\n\n    def render(self, index: int, position: dict, legs: List[dict],\n               chain: dict, as_of) -> List[str]:\n        lots   = self._lots(position, legs)\n        status = str(position.get("status") or "OPEN").upper()\n        closed = status.startswith("CLOSE")\n\n        out = [TRADE_REPORT_RULE]\n        out.append(f"Trade-{index}")\n        out.append(f"Strategy: {position.get(\'strategy_name\') or \'UNKNOWN\'}")\n        out.append(TRADE_REPORT_SUB)\n        out.append(f"Trade Start Data: time: {_tr_hhmm(position.get(\'entry_time\'))}")\n        for leg in legs:\n            out.append(\n                f"{_tr_side(leg.get(\'action\'))}: {lots} lot of "\n                f"{_tr_symbol(leg.get(\'option_type\'))} with premium: "\n                f"{_tr_price(leg.get(\'entry_price\'))} at strike: "\n                f"{_tr_strike(leg.get(\'strike\'))}"\n            )\n        if not legs:\n            out.append("no leg rows persisted for this position")\n\n        out.append(TRADE_REPORT_SUB)\n        end_time = position.get("exit_time") if closed else as_of\n        out.append(f"Trade End Data: time: {_tr_hhmm(end_time)}")\n        for leg in legs:\n            leg_closed = str(leg.get("leg_status") or "").upper() == "CLOSED"\n            if closed and leg_closed and leg.get("exit_price") not in (None, ""):\n                price, suffix = leg.get("exit_price"), ""\n            else:\n                # Not closed yet: what closing this leg now would cost.\n                price = self._mark_leg(leg, chain)\n                if price is None:\n                    price = leg.get("entry_price")\n                if closed:\n                    suffix = ("  [no exit fill]" if not leg_closed\n                              else "  [exit price not persisted]")\n                else:\n                    suffix = ""\n            out.append(\n                f"{_tr_closing_side(leg.get(\'action\'))}: {lots} lot of "\n                f"{_tr_symbol(leg.get(\'option_type\'))} with premium: "\n                f"{_tr_price(price)} at strike: "\n                f"{_tr_strike(leg.get(\'strike\'))}{suffix}"\n            )\n        if not legs:\n            out.append("no leg rows persisted for this position")\n        if closed:\n            out.append(f"Closed - {position.get(\'exit_reason\') or \'unknown\'}")\n        else:\n            out.append("Open")\n\n        out.append(TRADE_REPORT_SUB)\n        out.append(f"Position Status: {\'Close\' if closed else \'Open\'}")\n        committed, committed_basis = self.investment(position, legs)\n        net, realised, profit_basis = self.profit(position, legs, chain)\n        out.append(f"Total Investment: {_tr_money(committed)}")\n        for line in committed_basis:\n            out.append(f"    {line}")\n        out.append(\n            f"Total Profit: {_tr_signed(net)} "\n            f"{\'realised\' if realised else \'unrealised\'}"\n        )\n        for line in profit_basis:\n            out.append(f"    {line}")\n        out.append(TRADE_REPORT_RULE)\n        return out\n\n    def _header(self, trading_date: str, rows: List[dict], as_of,\n                title: Optional[str]) -> str:\n        n_open = sum(\n            1 for r in rows\n            if str(r.get("status") or "").upper().startswith("OPEN")\n        )\n        stamp = as_of.strftime("%H:%M:%S") if isinstance(as_of, datetime) \\\n            else _tr_hhmm(as_of)\n        label = title or f"TRADE REPORT [{self.source}]"\n        return (\n            f"\\n---- {label} | {trading_date} {stamp} | "\n            f"{len(rows)} trade(s), {n_open} open | mode={self.mode} ----"\n        )\n\n    # ── entry point ──────────────────────────────────────────────────\n\n    def report_cycle(self, trading_date: Optional[str] = None,\n                     chain: Optional[dict] = None,\n                     as_of=None,\n                     title: Optional[str] = None) -> int:\n        """Print the block for every trade of the session. Returns how many.\n\n        Called once per cycle by both engines. In each_cycle mode that is\n        every trade, open and closed, on every cycle - the operator asked\n        for exactly that, and the cost is console volume, not accuracy.\n        """\n        if self.mode == "off":\n            return 0\n        if trading_date is None:\n            trading_date = today_ist().isoformat()\n        if as_of is None:\n            as_of = now_ist()\n        chain = chain or {}\n\n        rows = self.positions_for(trading_date)\n        if not rows:\n            return 0\n\n        if self._day != trading_date:\n            self._day = trading_date\n            self._last.clear()\n\n        eps = _tr_num(getattr(self.config, "trade_report_mark_eps", 25.0), 25.0)\n        cap = _tr_int(getattr(self.config, "trade_report_max_per_cycle", 0))\n        quiet = self.mode == "on_change"\n\n        printed = 0\n        header_done = False\n        for index, position in enumerate(rows, start=1):\n            position_id = str(position.get("position_id") or f"#{index}")\n            legs = self.legs_for(position_id)\n            try:\n                net, realised, _note = self.profit(position, legs, chain)\n                if quiet:\n                    state = "closed" if realised else "open"\n                    previous = self._last.get(position_id)\n                    if previous is not None and previous[0] == state and \\\n                            abs(previous[1] - net) < eps:\n                        continue\n                    self._last[position_id] = (state, net)\n                if cap and printed >= cap:\n                    break\n                lines = self.render(index, position, legs, chain, as_of)\n            except Exception as exc:\n                self._debug(f"render failed for {position_id}: {exc}")\n                continue\n            if not header_done:\n                print(self._header(trading_date, rows, as_of, title))\n                header_done = True\n            for line in lines:\n                print(line)\n            printed += 1\n\n        if printed:\n            try:\n                sys.stdout.flush()\n            except Exception:\n                pass\n        return printed\n\n\n# ─────────────────────────────────────────────\n# SELF TEST\n# ─────────────────────────────────────────────\n\n',
    ),
    (
        'core.py',
        [
            '    print(f"  parse_ist_timestamp():  {parsed}")\n    print(f"  IST timezone:           {IST}")\n\n    db.close()\n    print_section("SELF-TEST COMPLETE", char="#")\n    print(f"  Database: {db.db_path}")\n',
        ],
        '    print(f"  parse_ist_timestamp():  {parsed}")\n    print(f"  IST timezone:           {IST}")\n\n    # ── Trade Console Report (v7) ────────────────────────────────────\n    # One closed and one open trade, written straight into the scratch\n    # book, then rendered. This is the block both engines print on every\n    # cycle, so its shape is asserted rather than eyeballed.\n    print_section("TRADE CONSOLE REPORT TEST")\n    import contextlib as _ctxlib\n    import io as _io\n\n    _td = today_ist().isoformat()\n    _chain = {\n        25600.0: {"call": {"bid": 11.90, "ask": 12.10, "ltp": 12.00},\n                  "put":  {"bid": 4.00,  "ask": 4.20,  "ltp": 4.10}},\n        25750.0: {"call": {"bid": 2.90,  "ask": 3.10,  "ltp": 3.00},\n                  "put":  {"bid": 1.00,  "ask": 1.20,  "ltp": 1.10}},\n    }\n    db.insert("positions", {\n        "position_id": "selftest-closed", "trading_date": _td,\n        "strategy_name": "BEAR_CALL_SPREAD", "strategy_type": "SELL",\n        "entry_time": f"{_td}T12:03:00", "exit_time": f"{_td}T13:32:00",\n        "entry_credit": 14.05, "gross_credit": 14.05,\n        "entry_costs_rupees": 55.0, "exit_costs_rupees": 52.0,\n        "exit_premium": 9.00, "exit_reason": "gamma_window_derisk_1330",\n        "gross_pnl_rupees": 1306.5, "net_pnl_rupees": 1199.5,\n        "final_lots": 4, "estimated_margin": 19000.0, "total_max_risk": 8000.0,\n        "status": "CLOSED",\n    })\n    db.insert("positions", {\n        "position_id": "selftest-open", "trading_date": _td,\n        "strategy_name": "IRON_CONDOR", "strategy_type": "SELL",\n        "entry_time": f"{_td}T13:40:00",\n        "entry_credit": 5.00, "gross_credit": 5.20,\n        "entry_costs_rupees": 60.0, "final_lots": 2,\n        "estimated_margin": 15000.0, "total_max_risk": 6000.0,\n        "status": "OPEN",\n    })\n    for _row in (\n        ("selftest-closed", 25600.0, "call", "SELL", 4, 19.07, 12.00, "CLOSED"),\n        ("selftest-closed", 25750.0, "call", "BUY",  4, 5.02,  3.00, "CLOSED"),\n        ("selftest-open",   25600.0, "call", "SELL", 2, 11.00, None,  "OPEN"),\n        ("selftest-open",   25750.0, "put",  "BUY",  2, 1.10,  None,  "OPEN"),\n    ):\n        db.insert("position_legs", {\n            "position_id": _row[0], "strike": _row[1], "option_type": _row[2],\n            "action": _row[3], "qty": _row[4] * config.lot_size,\n            "entry_price": _row[5], "exit_price": _row[6], "leg_status": _row[7],\n        })\n\n    reporter = TradeConsoleReporter(db, config, logger, source="SELFTEST")\n    _buf = _io.StringIO()\n    with _ctxlib.redirect_stdout(_buf):\n        _printed = reporter.report_cycle(\n            trading_date=_td, chain=_chain, as_of=now_ist()\n        )\n    _text = _buf.getvalue()\n    assert _printed == 2, f"expected 2 trade blocks, got {_printed}"\n    for _frag in (\n        TRADE_REPORT_RULE, TRADE_REPORT_SUB, "Trade-1", "Trade-2",\n        "Strategy: BEAR_CALL_SPREAD", "Strategy: IRON_CONDOR",\n        "Trade Start Data: time: 12:03",\n        "Sold: 4 lot of CE with premium: 19.07 at strike: 25600",\n        "Bought: 4 lot of CE with premium: 5.02 at strike: 25750",\n        "Trade End Data: time: 13:32",\n        "Bought: 4 lot of CE with premium: 12.00 at strike: 25600",\n        "Closed - gamma_window_derisk_1330",\n        "Position Status: Close", "Position Status: Open",\n        "Bought: 2 lot of PE with premium: 1.10 at strike: 25750",\n        "Sold: 2 lot of PE with premium: 1.00 at strike: 25750",\n        "Total Investment: Rs", "Total Profit: Rs",\n    ):\n        assert _frag in _text, f"trade report is missing {_frag!r}"\n    # the open trade is marked to what closing it would cost, not to the mid\n    _open_block = _text.split("Trade-2", 1)[1]\n    assert "Bought: 2 lot of CE with premium: 12.10" in _open_block, \\\n        "open trade must be marked at the ask (liquidation), not the mid"\n    assert "unrealised" in _open_block and "realised" in _text\n    print(_text.rstrip())\n    print("  [OK] Trade console report renders an open and a closed trade")\n\n    db.close()\n    print_section("SELF-TEST COMPLETE", char="#")\n    print(f"  Database: {db.db_path}")\n',
    ),
    (
        'execution_engine.py',
        [
            '    load_config, setup_logging,\n    RateLimiter, UpstoxClient,\n    UpstoxAPIError, AlertNotifier,\n)\nfrom data_engine import MarketDataEngine\nfrom calibration_engine import CalibrationEngine\n',
        ],
        '    load_config, setup_logging,\n    RateLimiter, UpstoxClient,\n    UpstoxAPIError, AlertNotifier,\n    # v7: the entry credit the FILLS booked, as against the one the strategy\n    # engine planned. Used by execute_close() to settle a trade on its own\n    # prices; the same function feeds the per-trade console report.\n    realised_entry_credit,\n    TradeConsoleReporter,\n)\nfrom data_engine import MarketDataEngine\nfrom calibration_engine import CalibrationEngine\n',
    ),
    (
        'execution_engine.py',
        [
            '    def _ensure_extra_columns(self) -> None:\n        """Add any columns that may be missing from older database versions."""\n        extra = [\n            ("positions", "profit_lock_activated",   "INTEGER DEFAULT 0"),\n            ("positions", "profit_lock_stop_level",  "REAL"),\n            ("positions", "exit_priority",           "INTEGER"),\n',
        ],
        '    def _ensure_extra_columns(self) -> None:\n        """Add any columns that may be missing from older database versions."""\n        extra = [\n            # v7: monitor_position() writes the liquidation mark on every\n            # cycle and MainEngine.compute_unrealized_pnl() reads it, but the\n            # column exists in neither SCHEMA_SQL nor MIGRATION_SQL - it was\n            # only ever created by MarketDataEngine._ensure_extra_columns().\n            # That made the exit ladder depend on another engine having been\n            # constructed first: build an ExecutionEngine against a fresh book\n            # on its own (a tool, a test, a refactor) and the first\n            # monitor_position() raises sqlite3.OperationalError, which the\n            # main loop catches as "UNHANDLED ERROR in run_one_cycle" - so\n            # every cycle fails and no position is ever monitored or exited.\n            # Ensure it here too; ensure_column is a no-op when it exists.\n            ("positions", "last_liquidation_premium", "REAL"),\n            # v7: the credit the FILLS booked, written at close next to the\n            # planned credit the strategy engine priced the trade with, so\n            # the two can always be compared after the fact.\n            ("positions", "entry_credit_realised",   "REAL"),\n            ("positions", "profit_lock_activated",   "INTEGER DEFAULT 0"),\n            ("positions", "profit_lock_stop_level",  "REAL"),\n            ("positions", "exit_priority",           "INTEGER"),\n',
    ),
    (
        'execution_engine.py',
        [
            '            ("trade_exits", "exit_priority",         "INTEGER"),\n            ("trade_exits", "exit_priority_name",    "TEXT"),\n            ("trade_exits", "pnl_15min_after_exit",  "REAL"),\n        ]\n        for table, col, coltype in extra:\n            self.db.ensure_column(table, col, coltype)\n',
        ],
        '            ("trade_exits", "exit_priority",         "INTEGER"),\n            ("trade_exits", "exit_priority_name",    "TEXT"),\n            ("trade_exits", "pnl_15min_after_exit",  "REAL"),\n            # v7: execute_close() has written these two into trade_exits since\n            # it was first drafted, but neither SCHEMA_SQL nor MIGRATION_SQL\n            # declared them, so the INSERT raised "no column named exit_adx"\n            # on every single close and the row was dropped (caught and logged\n            # as a warning). The exit audit table was therefore always empty -\n            # the 2026-09-11 paper book holds 2 CLOSED positions and 0\n            # trade_exits rows - and every query that joins trade_exits\n            # silently returned nothing. Declared in core.py too; ensured here\n            # so an existing book is repaired on the next engine start.\n            ("trade_exits", "exit_adx",              "REAL"),\n            ("trade_exits", "exit_vwap_dist",        "REAL"),\n        ]\n        for table, col, coltype in extra:\n            self.db.ensure_column(table, col, coltype)\n',
    ),
    (
        'execution_engine.py',
        [
            '        Compute all transaction costs for a set of legs.\n\n        For ENTRY: STT on sell side, stamp on buy side\n        For EXIT:  STT on the side that was originally bought (now being sold)\n\n        Returns dict with total_rupees and detailed breakdown.\n        """\n',
        ],
        '        Compute all transaction costs for a set of legs.\n\n        For ENTRY: STT on sell side, stamp on buy side\n        For EXIT:  the sides are the MIRROR IMAGE - a leg that was sold at\n                   entry is bought back, and a leg that was bought is sold -\n                   and STT is levied on the SALE of an option, so on exit it\n                   belongs to the legs that were originally BOUGHT.\n\n        Returns dict with total_rupees and detailed breakdown.\n        """\n',
    ),
    (
        'execution_engine.py',
        [
            '        C02        = self.config.lot_size\n        sell_value = buy_value = 0.0\n        num_orders = len(legs)\n\n        for leg in legs:\n            # Use fill price for cost computation\n',
        ],
        '        C02        = self.config.lot_size\n        sell_value = buy_value = 0.0\n        num_orders = len(legs)\n        # v7: `action` used to be accepted and ignored, and the classification\n        # below read leg["action"] - the ENTRY side - for both phases. On an\n        # exit that charges STT to the buy-backs (which pay no STT) and stamp\n        # duty to the sales (which pay no stamp), while the turnover-based\n        # charges stay right, so the error is invisible in the total\'s\n        # magnitude and only shows up when the live book is reconciled\n        # against the replay: StrategyEngine._compute_costs() and\n        # BacktestRunner._close() both pass the CLOSING side, so the same\n        # trade cost Rs 2.27 more live than in replay on a 4-lot bear call\n        # spread (STT Rs 3.12 charged where Rs 0.78 was due).\n        closing = str(action or "").strip().upper() == "EXIT"\n\n        for leg in legs:\n            # Use fill price for cost computation\n',
    ),
    (
        'execution_engine.py',
        [
            '            qty           = lots * C02\n            premium_value = price * qty\n\n            if leg["action"] == "SELL":\n                sell_value += premium_value\n            else:\n                buy_value += premium_value\n',
        ],
        '            qty           = lots * C02\n            premium_value = price * qty\n\n            side = str(leg.get("action") or "").strip().upper()\n            if closing:\n                side = "BUY" if side.startswith("S") else "SELL"\n\n            if side == "SELL":\n                sell_value += premium_value\n            else:\n                buy_value += premium_value\n',
    ),
    (
        'execution_engine.py',
        [
            '        C02 = float(self.config.lot_size or 1)\n        live = [l for l in legs if l.get("leg_status") != "CLOSED"]\n        n_legs = max(len(live), 1)\n        brokerage_pts = (self.config.brokerage_per_order * n_legs) / C02\n        pct_pts = 0.0\n        spread_pts = 0.0\n        for leg in live:\n',
        ],
        '        C02 = float(self.config.lot_size or 1)\n        live = [l for l in legs if l.get("leg_status") != "CLOSED"]\n        n_legs = max(len(live), 1)\n        # v7: brokerage carries 18% GST like every other charge in this\n        # repository (_compute_transaction_costs and\n        # StrategyEngine._compute_costs both compute GST on\n        # brokerage + exchange + sebi). It was the only cost line here that\n        # was added net of GST.\n        brokerage_pts = (self.config.brokerage_per_order * 1.18 * n_legs) / C02\n        pct_pts = 0.0\n        spread_pts = 0.0\n        for leg in live:\n',
    ),
    (
        'execution_engine.py',
        [
            '            # On exit, STT applies to the legs being SOLD, i.e. the ones that\n            # were originally bought.\n            _stt = self.config.stt_options_sell if leg.get("action") == "BUY" else 0.0\n            pct_pts += mid * (\n                self.config.exchange_txn_rate + self.config.sebi_rate + _stt\n            ) * 1.18\n        return round(brokerage_pts + pct_pts + spread_pts, 3)\n\n    def _compute_current_premium(\n',
        ],
        '            # On exit, STT applies to the legs being SOLD, i.e. the ones that\n            # were originally bought.\n            _stt = self.config.stt_options_sell if leg.get("action") == "BUY" else 0.0\n            # v7: GST is levied on the exchange and SEBI charges (and on\n            # brokerage, added separately below), never on STT - STT is a\n            # tax, not a service. The 1.18 used to be applied to the whole\n            # bracket, so the estimate charged 18% GST on a statutory tax,\n            # and it disagreed with _compute_transaction_costs() and\n            # StrategyEngine._compute_costs() in the same repository, both of\n            # which compute GST as (brokerage + exchange + sebi) * 0.18.\n            pct_pts += mid * (\n                (self.config.exchange_txn_rate + self.config.sebi_rate) * 1.18\n                + _stt\n            )\n        return round(brokerage_pts + pct_pts + spread_pts, 3)\n\n    def _compute_current_premium(\n',
    ),
    (
        'execution_engine.py',
        [
            '                    {"leg_id": leg["leg_id"]},\n                )\n\n                exit_legs_info.append({**leg, "exit_price": exit_price, "fill": fill})\n\n                # Accumulate exit premium\n                # For SELL legs: we pay to close (cost)\n',
        ],
        '                    {"leg_id": leg["leg_id"]},\n                )\n\n                # v7: the fill alone is not an audit. quoted_mid_exit is\n                # already computed above and written to position_legs, but it\n                # never made it into this dict, so the exit_slippage sum below\n                # filtered every leg out and trade_exits booked 0.0 slippage\n                # for every close the system ever made - the one number that\n                # says whether the exits are being taken at fair value was\n                # permanently zero.\n                exit_legs_info.append({\n                    **leg,\n                    "exit_price":         exit_price,\n                    "quoted_mid_at_exit": quoted_mid_exit,\n                    "exit_delta":         float(opt.get("delta", 0) or 0),\n                    "fill":               fill,\n                })\n\n                # Accumulate exit premium\n                # For SELL legs: we pay to close (cost)\n',
    ),
    (
        'execution_engine.py',
        [
            '        C02          = self.config.lot_size\n        entry_credit = float(position.get("entry_credit") or 0)\n\n        # Gross P&L = entry_credit - exit_premium (for credit spreads)\n        gross_pnl_pts = entry_credit - exit_premium\n        gross_pnl_rs  = gross_pnl_pts * C02 * lots\n\n        # Costs\n',
        ],
        '        C02          = self.config.lot_size\n        entry_credit = float(position.get("entry_credit") or 0)\n\n        # v7: settle on the FILLS, not on the plan.\n        #\n        # positions.entry_credit is the strategy engine\'s planned NET credit:\n        # gross credit minus an ESTIMATED slippage and the entry charges\n        # converted to points (StrategyEngine: net_credit = gross_credit -\n        # total_slippage - entry_costs_pts). Using it here did two wrong\n        # things at once - it booked a modelled slippage as though it had\n        # happened, and it removed the entry charges in points before the\n        # lines below removed them again in rupees, because total_costs_rs\n        # includes entry_costs_rupees.\n        #\n        # Measured on the 2026-09-11 paper book: BULL_PUT_SPREAD 2 lots,\n        # planned credit 18.58 pts, fills 47.65 / 28.60 = 19.05 pts, exit\n        # 15.50 pts, entry charges Rs 57.67, exit charges Rs 56.05.\n        #   booked : (18.58 - 15.50) x 130 - 113.72 = Rs 286.68\n        #   correct: (19.05 - 15.50) x 130 - 113.72 = Rs 347.78\n        # Rs 61.10 of a real profit never existed - Rs 57.67 charged twice\n        # and Rs 3.38 of estimated slippage charged as real. The day was\n        # reported Rs 125 (10%) worse than the fills say, and since the\n        # replay harness settles on filled prices, the live book and the\n        # backtest could never be reconciled on the same trade.\n        #\n        # The exit ladder is untouched: stop_premium, target_premium and the\n        # profit lock keep comparing against the stored entry_credit exactly\n        # as before, so no exit decision changes. Only the money that is\n        # booked, reported and fed to calibration becomes the money that was\n        # actually made.\n        realised_credit, credit_basis = realised_entry_credit(position, legs)\n        if abs(realised_credit - entry_credit) > 1e-9:\n            self.logger.info(\n                f"entry credit settled on {credit_basis}: "\n                f"planned {entry_credit:+.3f} pts vs realised "\n                f"{realised_credit:+.3f} pts "\n                f"({(realised_credit - entry_credit) * C02 * lots:+,.2f} Rs "\n                f"on {lots} lot(s))"\n            )\n\n        # Gross P&L = realised entry credit - what it cost to close\n        gross_pnl_pts = realised_credit - exit_premium\n        gross_pnl_rs  = gross_pnl_pts * C02 * lots\n\n        # Costs\n',
    ),
    (
        'execution_engine.py',
        [
            '        hold_minutes = (now - entry_time).total_seconds() / 60.0\n\n        # ── Update position ───────────────────────────────────────────────\n        self.db.update(\n            "positions",\n            {\n                "status":            "CLOSED",\n                "exit_time":         now.isoformat(),\n                "exit_reason":       reason,\n                "exit_priority":     priority,\n                "exit_premium":      exit_premium,\n                "gross_pnl_rupees":  gross_pnl_rs,\n                "exit_costs_rupees": exit_costs_rs,\n                "net_pnl_rupees":    net_pnl_rs,\n                "updated_at":        now.isoformat(),\n            },\n            {"position_id": position["position_id"]},\n        )\n\n        # ── Persist trade exit ────────────────────────────────────────────\n        priority_name = EXIT_PRIORITY_NAMES.get(priority, reason)\n',
        ],
        '        hold_minutes = (now - entry_time).total_seconds() / 60.0\n\n        # ── Update position ───────────────────────────────────────────────\n        _close_update = {\n            "status":            "CLOSED",\n            "exit_time":         now.isoformat(),\n            "exit_reason":       reason,\n            "exit_priority":     priority,\n            "exit_premium":      exit_premium,\n            "gross_pnl_rupees":  gross_pnl_rs,\n            "exit_costs_rupees": exit_costs_rs,\n            "net_pnl_rupees":    net_pnl_rs,\n            # v7: kept next to the planned figure in entry_credit so the two\n            # can be compared on any closed trade, forever.\n            "entry_credit_realised": realised_credit,\n            "updated_at":        now.isoformat(),\n        }\n        try:\n            self.db.update(\n                "positions", _close_update,\n                {"position_id": position["position_id"]},\n            )\n        except Exception as _cue:\n            # An audit column that could not be added must never cost the\n            # close itself: retry without it and say so.\n            self.logger.warning(\n                f"positions close update failed ({_cue}); retrying without "\n                f"entry_credit_realised"\n            )\n            _close_update.pop("entry_credit_realised", None)\n            self.db.update(\n                "positions", _close_update,\n                {"position_id": position["position_id"]},\n            )\n\n        # ── Persist trade exit ────────────────────────────────────────────\n        priority_name = EXIT_PRIORITY_NAMES.get(priority, reason)\n',
    ),
    (
        'execution_engine.py',
        [
            '            "Exit Reason":     reason,\n            "Exit Priority":   f"{priority} ({priority_name})",\n            "Exit Premium":    f"{exit_premium:.2f}pts",\n            "Gross P&L (Rs)":  f"{gross_pnl_rs:,.0f}",\n            "Total Costs (Rs)":f"{total_costs_rs:,.0f}",\n            "Net P&L (Rs)":    f"{net_pnl_rs:,.0f}",\n',
        ],
        '            "Exit Reason":     reason,\n            "Exit Priority":   f"{priority} ({priority_name})",\n            "Exit Premium":    f"{exit_premium:.2f}pts",\n            "Entry Credit":    f"{realised_credit:.2f}pts realised "\n                               f"({credit_basis}) vs {entry_credit:.2f}pts "\n                               f"planned",\n            "Gross P&L (Rs)":  f"{gross_pnl_rs:,.0f}",\n            "Total Costs (Rs)":f"{total_costs_rs:,.0f}",\n            "Net P&L (Rs)":    f"{net_pnl_rs:,.0f}",\n',
    ),
    (
        'execution_engine.py',
        [
            '                state["consecutive_stops"] = 0\n\n        # Daily loss limit check\n        current_cap = float(state.get("current_capital", self.config.starting_capital) or 0)\n        if current_cap > 0:\n            daily_loss_pct = max(0.0, -float(state.get("daily_pnl", 0.0) or 0.0)) / current_cap\n            if daily_loss_pct >= self.config.max_daily_loss_pct:\n                state["daily_halted"] = True\n                self.logger.warning(\n',
        ],
        '                state["consecutive_stops"] = 0\n\n        # Daily loss limit check\n        # v7: measured against capital AT THE START OF THE DAY, the basis\n        # main.check_daily_loss_halt() and the replay harness both use. The\n        # denominator here was the post-loss capital, which made one\n        # configured limit mean two different things inside a single process:\n        # the deeper the loss, the smaller this denominator, so this copy of\n        # the check tripped earlier than the one that pages the operator and\n        # flattens the book - and it tripped silently, with no alert and no\n        # risk_halt row to explain why entries had stopped.\n        current_cap = float(state.get("current_capital", self.config.starting_capital) or 0)\n        daily_pnl   = float(state.get("daily_pnl", 0.0) or 0.0)\n        day_start_cap = current_cap - daily_pnl\n        if day_start_cap <= 0:\n            day_start_cap = current_cap\n        if day_start_cap > 0:\n            daily_loss_pct = max(0.0, -daily_pnl) / day_start_cap\n            if daily_loss_pct >= self.config.max_daily_loss_pct:\n                state["daily_halted"] = True\n                self.logger.warning(\n',
    ),
    (
        'execution_engine.py',
        [
            '    print("  Hard exit sweep ran without error")\n    print("  [OK] Hard exit sweep test passed")\n\n    db.close()\n    print_section("EXECUTION ENGINE SELF-TEST COMPLETE", char="#")\n    print("  All tests passed")\n',
        ],
        '    print("  Hard exit sweep ran without error")\n    print("  [OK] Hard exit sweep test passed")\n\n    # ── Test 11: v7 close settlement — the fills, not the plan ─────────\n    # Reproduces the 2026-09-11 BULL_PUT_SPREAD that exposed the bug: the\n    # strategy engine books entry_credit as the PLANNED net credit (gross\n    # minus an estimated slippage minus the entry charges in points), and\n    # execute_close used to settle against that figure while subtracting the\n    # same entry charges again in rupees. The leg rows carry what the fills\n    # actually did, so they are the basis now.\n    print_section("Close Settlement Tests (v7)")\n    import contextlib as _ctxlib11\n    import io as _io11\n\n    _pid11 = "selftest-close-v7"\n    _lots11 = 2\n    _units11 = config.lot_size * _lots11\n    db.insert("positions", {\n        "position_id": _pid11, "trading_date": today_ist().isoformat(),\n        "strategy_name": "BULL_PUT_SPREAD", "strategy_type": "SELL",\n        "entry_time": now_ist().isoformat(),\n        # planned: 19.05 gross - 0.026 estimated slippage - 0.4436 costs\n        "entry_credit": 18.58,\n        "gross_credit": 19.05,\n        "entry_costs_rupees": 57.67,\n        "final_lots": _lots11, "estimated_margin": 15730.0,\n        "total_max_risk": 5693.0, "status": "OPEN",\n    })\n    for _k11, _a11, _px11 in ((23150.0, "SELL", 47.65), (23050.0, "BUY", 28.60)):\n        db.insert("position_legs", {\n            "position_id": _pid11, "strike": _k11, "option_type": "put",\n            "action": _a11, "qty": _units11, "entry_price": _px11,\n            "leg_status": "OPEN",\n        })\n\n    # The paper executor fills a buy-back at the ask and a sale at the bid,\n    # so this chain IS the fill: 40.10 and 24.60, as on 2026-09-11.\n    _chain11 = {\n        23150.0: {"put": {"bid": 40.00, "ask": 40.10, "ltp": 40.05,\n                          "delta": -0.30}},\n        23050.0: {"put": {"bid": 24.60, "ask": 24.70, "ltp": 24.65,\n                          "delta": -0.22}},\n    }\n    _saved_chain11 = market_engine.last_chain\n    market_engine.last_chain = _chain11\n\n    _pos11 = db.query_one(\n        "SELECT * FROM positions WHERE position_id=?", (_pid11,))\n    _legs11 = engine._get_position_legs(_pid11)\n\n    # (a) the realised credit is the fills, and it is not the planned figure\n    _rc11, _basis11 = realised_entry_credit(_pos11, _legs11)\n    print(f"  entry credit: planned {float(_pos11[\'entry_credit\']):.3f} pts, "\n          f"realised {_rc11:.3f} pts (basis={_basis11})")\n    assert _basis11 == "fills", \\\n        f"expected the fills to be the basis, got {_basis11}"\n    assert abs(_rc11 - (47.65 - 28.60)) < 1e-9, \\\n        f"realised credit should be 19.05, got {_rc11}"\n    assert abs(_rc11 - float(_pos11["entry_credit"])) > 0.01, \\\n        "this test is pointless if the planned and realised credit agree"\n\n    # (b) exit charges land on the leg SOLD at exit, not on the buy-back\n    _exit_costs11 = engine._compute_transaction_costs(\n        [{"action": "SELL", "exit_price": 40.10},\n         {"action": "BUY",  "exit_price": 24.60}], _lots11, "EXIT")\n    _expect_stt11 = 24.60 * _units11 * config.stt_options_sell\n    print(f"  exit STT: Rs{_exit_costs11[\'breakdown\'][\'stt\']:.2f} "\n          f"(due Rs{_expect_stt11:.2f}, on the leg sold at exit)")\n    assert abs(_exit_costs11["breakdown"]["stt"] - _expect_stt11) < 0.01, \\\n        f"exit STT should be {_expect_stt11:.2f}, got " \\\n        f"{_exit_costs11[\'breakdown\'][\'stt\']:.2f}"\n    # ...and the entry side is untouched by the exit-side fix\n    _entry_costs11 = engine._compute_transaction_costs(\n        [{"action": "SELL", "exec_price": 47.65},\n         {"action": "BUY",  "exec_price": 28.60}], _lots11, "ENTRY")\n    assert abs(_entry_costs11["breakdown"]["stt"]\n               - 47.65 * _units11 * config.stt_options_sell) < 0.01, \\\n        "entry STT must still be charged on the leg sold at entry"\n\n    # (c) close through the real path and read the book back\n    _state11 = dict(market_engine.state)\n    try:\n        engine.execute_close(\n            _pos11, "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {})\n    finally:\n        market_engine.last_chain = _saved_chain11\n\n    _row11 = db.query_one(\n        "SELECT * FROM positions WHERE position_id=?", (_pid11,))\n    _expect_gross11 = ((47.65 - 28.60) - (40.10 - 24.60)) * _units11\n    _expect_net11 = _expect_gross11 - 57.67 - float(_row11["exit_costs_rupees"])\n    # What the pre-v7 code booked for the very same trade: it settled on the\n    # PLANNED credit and charged the exit STT/stamp on the entry-side legs\n    # (the old call passed no side, so the entry rules applied at exit too).\n    _old_exit_costs11 = engine._compute_transaction_costs(\n        [{"action": "SELL", "exit_price": 40.10},\n         {"action": "BUY",  "exit_price": 24.60}],\n        _lots11, "ENTRY")["total_rupees"]\n    _old_net11 = (18.58 - 15.50) * _units11 - 57.67 - _old_exit_costs11\n    print(f"  gross P&L: Rs{float(_row11[\'gross_pnl_rupees\']):,.2f} "\n          f"(the fills say Rs{_expect_gross11:,.2f}; the planned credit "\n          f"would have said Rs{(18.58 - 15.50) * _units11:,.2f})")\n    print(f"  net P&L:   Rs{float(_row11[\'net_pnl_rupees\']):,.2f} "\n          f"(pre-v7 booked Rs{_old_net11:,.2f} for this trade), charges "\n          f"Rs{57.67 + float(_row11[\'exit_costs_rupees\']):,.2f}")\n    assert _row11["status"] == "CLOSED", "position not closed"\n    assert abs(float(_row11["exit_premium"]) - 15.50) < 1e-9, \\\n        f"exit premium should be 15.50, got {_row11[\'exit_premium\']}"\n    assert abs(float(_row11["gross_pnl_rupees"]) - _expect_gross11) < 0.01, \\\n        f"gross P&L must settle on the fills: {_expect_gross11:.2f}"\n    assert abs(float(_row11["net_pnl_rupees"]) - _expect_net11) < 0.01, \\\n        "net P&L is not gross minus the charges actually booked"\n    assert abs(float(_row11["entry_credit_realised"]) - 19.05) < 1e-9, \\\n        "entry_credit_realised not persisted"\n    assert abs(float(_row11["net_pnl_rupees"]) - _old_net11) > 1.0, \\\n        "still settling on the planned credit and the wrong exit leg"\n\n    _exit_row11 = db.query_one(\n        "SELECT * FROM trade_exits WHERE position_id=?", (_pid11,))\n    # v7: this row used never to exist. execute_close() writes exit_adx and\n    # exit_vwap_dist, no schema declared them, the INSERT raised, and the\n    # warning was swallowed - so the whole exit audit table stayed empty\n    # (2026-09-11 book: 2 CLOSED positions, 0 trade_exits rows).\n    assert _exit_row11 is not None, (\n        "trade_exits row missing - the exit INSERT is failing again; check "\n        "that every column execute_close() writes is declared in core.py")\n    for _col11 in ("exit_adx", "exit_vwap_dist", "exit_priority",\n                   "exit_priority_name", "exit_slippage"):\n        assert _col11 in _exit_row11.keys(), \\\n            f"trade_exits is missing the {_col11} column"\n    assert abs(float(_exit_row11["gross_pnl_pts"]) - 3.55) < 1e-9, \\\n        f"trade_exits.gross_pnl_pts should be 3.55, got " \\\n        f"{_exit_row11[\'gross_pnl_pts\']}"\n    assert abs(float(_exit_row11["net_pnl_rupees"])\n               - float(_row11["net_pnl_rupees"])) < 0.01, \\\n        "trade_exits and positions disagree on what the trade made"\n    assert _exit_row11["exit_priority_name"] == "TIME_TARGET", \\\n        f"exit priority name wrong: {_exit_row11[\'exit_priority_name\']!r}"\n    _legs_back11 = json.loads(_exit_row11["exit_legs_json"] or "[]")\n    assert len(_legs_back11) == 2, "both exit legs must be in exit_legs_json"\n    assert all(_l.get("exit_price") and _l.get("quoted_mid_at_exit")\n               for _l in _legs_back11), \\\n        "exit legs must carry the fill and the quoted mid it was taken against"\n    # v7: exit_slippage was 0.0 on every close the system ever made, because\n    # the quoted mid never reached the audit dict the sum is built from. Both\n    # legs here fill one tick away from the mid, so it must now be measured.\n    _expect_slip11 = sum(\n        abs(float(_l["exit_price"]) - float(_l["quoted_mid_at_exit"]))\n        for _l in _legs_back11)\n    assert _expect_slip11 > 0, "the test chain should not fill exactly on the mid"\n    assert abs(float(_exit_row11["exit_slippage"]) - _expect_slip11) < 0.01, \\\n        f"exit_slippage should be {_expect_slip11:.3f}, got " \\\n        f"{_exit_row11[\'exit_slippage\']}"\n    print(f"  exit slippage measured: {_expect_slip11:.3f} pts "\n          f"(booked {float(_exit_row11[\'exit_slippage\']):.3f}; pre-v7 always 0.000)")\n    print(f"  trade_exits row written: gross {float(_exit_row11[\'gross_pnl_pts\']):.2f}pts "\n          f"net Rs{float(_exit_row11[\'net_pnl_rupees\']):,.2f} "\n          f"({_exit_row11[\'result\']})")\n\n    # (d) and the console block reports the same money\n    _rep11 = TradeConsoleReporter(db, config, logger, source="SELFTEST")\n    _buf11 = _io11.StringIO()\n    with _ctxlib11.redirect_stdout(_buf11):\n        _n11 = _rep11.report_cycle(\n            trading_date=today_ist().isoformat(), chain={}, as_of=now_ist())\n    _text11 = _buf11.getvalue()\n    assert _n11 == 1, f"expected 1 trade block, got {_n11}"\n    for _frag11 in (\n        "Trade-1", "Strategy: BULL_PUT_SPREAD",\n        "Sold: 2 lot of PE with premium: 47.65 at strike: 23150",\n        "Bought: 2 lot of PE with premium: 28.60 at strike: 23050",\n        "Closed - CLOSE_TARGET", "Position Status: Close",\n        f"Rs {float(_row11[\'net_pnl_rupees\']):+,.2f} realised",\n    ):\n        assert _frag11 in _text11, f"trade block is missing {_frag11!r}"\n    assert "the book says" not in _text11, \\\n        "the block and the book must agree on a freshly closed trade"\n\n    market_engine.state.clear()\n    market_engine.state.update(_state11)\n    print("  [OK] Close settlement tests passed (v7)")\n\n    db.close()\n    print_section("EXECUTION ENGINE SELF-TEST COMPLETE", char="#")\n    print("  All tests passed")\n',
    ),
    (
        'main.py',
        [
            '    ExpiryCalendar, now_ist, today_ist,\n    print_section, print_kv_table,\n    load_config, setup_logging,\n)\nfrom data_engine import MarketDataEngine\nfrom regime_engine import RegimeEngine, merge_regime_into_signals\n',
        ],
        '    ExpiryCalendar, now_ist, today_ist,\n    print_section, print_kv_table,\n    load_config, setup_logging,\n    # v7: per-trade console report + the fill-based entry credit\n    TradeConsoleReporter, realised_entry_credit,\n)\nfrom data_engine import MarketDataEngine\nfrom regime_engine import RegimeEngine, merge_regime_into_signals\n',
    ),
    (
        'main.py',
        [
            '    8. If entry possible: run strategy engine → execute entry\n    9. Update cycle log with P&L\n    10. Print cycle summary\n\n    Separate timers:\n    - Spot bar collection: every spot_bar_interval_sec (60s)\n',
        ],
        "    8. If entry possible: run strategy engine → execute entry\n    9. Update cycle log with P&L\n    10. Print cycle summary\n    11. Print the per-trade console report (v7: every trade of the session,\n        performed or in progress, in the operator's fixed block format)\n\n    Separate timers:\n    - Spot bar collection: every spot_bar_interval_sec (60s)\n",
    ),
    (
        'main.py',
        [
            '        self.execution_engine = ExecutionEngine(\n            self.config, self.db, self.market_engine, self.cal_engine,\n            self.client, self.logger\n        )\n\n        # ── Loop state ────────────────────────────────────────────────────\n',
        ],
        '        self.execution_engine = ExecutionEngine(\n            self.config, self.db, self.market_engine, self.cal_engine,\n            self.client, self.logger\n        )\n\n        # ── v7: per-trade console report ─────────────────────────────────\n        # Prints the lifecycle block for every trade of the session, on every\n        # cycle: entry legs and fills, exit legs (or the current mark while\n        # the trade is still in progress), status, investment and profit. It\n        # reads the persisted book, so it can only report what the engine\n        # actually did. Config: TRADE_REPORT_ENABLED / TRADE_REPORT_MODE\n        # (each_cycle | on_change | off) / TRADE_REPORT_MAX_PER_CYCLE.\n        self.trade_reporter = TradeConsoleReporter(\n            self.db, self.config, self.logger, source="TRADE ENGINE"\n        )\n\n        # ── Loop state ────────────────────────────────────────────────────\n',
    ),
    (
        'main.py',
        [
            '        state["last_stop_reason"] = ""\n        state["last_stop_signal_combo"] = ""\n        self.market_engine._save_session_state()\n        self.logger.info(f"Daily state reset for new day: {today_str}")\n\n    def _market_open(self) -> bool:\n        if ExpiryCalendar.is_holiday(today_ist()):\n',
        ],
        '        state["last_stop_reason"] = ""\n        state["last_stop_signal_combo"] = ""\n        self.market_engine._save_session_state()\n\n        # ── v7: clear the per-session latches on the day roll ─────────────\n        # The session counters above were reset; the flags on this instance\n        # were not, and every one of them is a "do this once per day" latch:\n        #   _halt_action_done   the daily-loss halt would set daily_halted on\n        #                       the new day but never cancel orders or\n        #                       flatten again, because the action is guarded\n        #                       by "once per session" - the engine would sit\n        #                       through a breach it is configured to act on\n        #   _soft_halt_alerted  the soft threshold would never page again\n        #   _eod_done           EOD tasks (calibration, daily summary, the\n        #                       final flatten) would never run again\n        #   _feed_stale*        yesterday\'s stale feed would still be blocking\n        #                       entries at today\'s open\n        # The main loop normally exits after 15:35, so this only matters for a\n        # process deliberately left running across sessions - which is exactly\n        # the process nobody is watching.\n        self._halt_action_done      = False\n        self._soft_halt_alerted     = False\n        self._eod_done              = False\n        self._feed_stale            = False\n        self._feed_stale_alerted    = False\n        self._watchdog_next_flatten = 0.0\n        self._watchdog_last_alert   = 0.0\n        self._watchdog_failures     = 0\n        self._last_cycle_ok_mono    = time_module.monotonic()\n        self._last_cycle_ok_at      = now_ist()\n        self.loop_count             = 0\n\n        self.logger.info(\n            f"Daily state reset for new day: {today_str} "\n            f"(halt/EOD/feed latches cleared, cycle counter restarted)"\n        )\n\n    def _market_open(self) -> bool:\n        if ExpiryCalendar.is_holiday(today_ist()):\n',
    ),
    (
        'main.py',
        [
            '                current_prem = pos.get("last_known_premium")\n            if current_prem is None:\n                continue\n            entry_credit = float(pos.get("entry_credit") or 0)\n            lots         = int(pos.get("final_lots", 1) or 1)\n            gross = (entry_credit - float(current_prem)) * C02 * lots\n\n',
        ],
        '                current_prem = pos.get("last_known_premium")\n            if current_prem is None:\n                continue\n            # v7: the same basis execute_close() now settles on.\n            # positions.entry_credit is the PLANNED net credit - gross credit\n            # minus an estimated slippage and the entry charges expressed in\n            # points - and the entry charges are subtracted again in rupees\n            # two lines below. The unrealised number that gates the daily\n            # loss halt therefore carried a double charge plus a modelled\n            # slippage the fills may never have produced (Rs 61 on the\n            # 2026-09-11 book), and it jumped by exactly that amount at the\n            # moment the position closed and was settled on its own prices.\n            # A risk limit must not move when a position is closed.\n            try:\n                _legs = self.execution_engine._get_position_legs(\n                    pos["position_id"]\n                )\n            except Exception:\n                _legs = []\n            entry_credit, _basis = realised_entry_credit(pos, _legs)\n            lots         = int(pos.get("final_lots", 1) or 1)\n            gross = (entry_credit - float(current_prem)) * C02 * lots\n\n',
    ),
    (
        'main.py',
        [
            '                f"vol regime defaulting to NEUTRAL"\n            )\n        self._print_cycle_footer(signals, total_pnl)\n        self.loop_count += 1\n\n    def _print_cycle_footer(self, signals: dict, total_pnl: float) -> None:\n',
        ],
        '                f"vol regime defaulting to NEUTRAL"\n            )\n        self._print_cycle_footer(signals, total_pnl)\n\n        # ── Step 11: Per-trade console report (v7) ────────────────────────\n        # Every trade of the session - the ones already performed and the one\n        # in progress - in the operator\'s fixed format. Printed after the\n        # cycle summary so the bottom of the screen always holds the newest\n        # state of the book.\n        self._print_trade_report()\n\n        self.loop_count += 1\n\n    def _print_cycle_footer(self, signals: dict, total_pnl: float) -> None:\n',
    ),
    (
        'main.py',
        [
            '            "Cal Tier":           signals.get("calibration_tier", 0),\n        })\n        print()\n\n    # ─────────────────────────────────────────────────────────────────────\n    # DAILY SUMMARY\n',
        ],
        '            "Cal Tier":           signals.get("calibration_tier", 0),\n        })\n        print()\n\n    def _print_trade_report(self, title: Optional[str] = None) -> None:\n        """v7: print the lifecycle block for every trade of the session.\n\n        One block per trade, in the operator\'s fixed format: entry legs and\n        their fills, exit legs (or the current liquidation mark while the\n        trade is still in progress), position status, cash committed and net\n        profit. The numbers are read out of the book, so the console and the\n        database cannot tell two different stories about the same trade.\n\n        Reporting is never allowed to become a trading failure: anything that\n        goes wrong is logged and the cycle carries on.\n        """\n        try:\n            self.trade_reporter.report_cycle(\n                trading_date=today_ist().isoformat(),\n                chain=self.market_engine.last_chain,\n                as_of=now_ist(),\n                title=title,\n            )\n        except Exception as e:\n            self.logger.debug(f"trade report error: {e}")\n\n    # ─────────────────────────────────────────────────────────────────────\n    # DAILY SUMMARY\n',
    ),
    (
        'main.py',
        [
            '            )\n            self.execution_engine.close_all_positions("EOD_CLOSE", force=True)\n\n        # ── Run EOD calibration tasks ─────────────────────────────────────\n        try:\n            self.cal_engine.run_eod_tasks(trading_date)\n',
        ],
        '            )\n            self.execution_engine.close_all_positions("EOD_CLOSE", force=True)\n\n        # ── v7: every trade of the day, one last time, in its final state ──\n        # The per-cycle report ends when the loop ends; this is the copy that\n        # stays on the screen next to the EOD summary, so the day can be read\n        # trade by trade without opening the database.\n        self._print_trade_report(title=f"END OF DAY TRADES - {trading_date}")\n\n        # ── Run EOD calibration tasks ─────────────────────────────────────\n        try:\n            self.cal_engine.run_eod_tasks(trading_date)\n',
    ),
    (
        'main.py',
        [
            '                self._feed_stale = False\n            return\n\n        idle     = time_module.monotonic() - self._last_cycle_ok_mono\n        degrade  = self._watchdog_sec("feed_degrade_sec", 45.0)\n        force    = self._watchdog_sec("feed_force_exit_sec", 120.0)\n        live     = not self.config.paper_trade_mode\n',
        ],
        '                self._feed_stale = False\n            return\n\n        # v7: the liveness clock starts at TODAY\'S session open, not at\n        # process start. A process left up since yesterday reports an idle of\n        # fourteen hours at 09:15:00, which trips feed_degrade_sec and then\n        # feed_force_exit_sec before the first cycle of the new session has had\n        # any chance to run - in live mode that is a forced flatten at the open\n        # caused by nothing but the calendar. _last_cycle_ok_at existed for\n        # exactly this and was never read; it is now maintained in run().\n        idle     = time_module.monotonic() - self._last_cycle_ok_mono\n        last_ok  = self._last_cycle_ok_at\n        if last_ok is None or last_ok.date() != now_dt.date():\n            session_open = now_dt.replace(\n                hour=9, minute=15, second=0, microsecond=0\n            )\n            idle = max(0.0, (now_dt - session_open).total_seconds())\n        degrade  = self._watchdog_sec("feed_degrade_sec", 45.0)\n        force    = self._watchdog_sec("feed_force_exit_sec", 120.0)\n        live     = not self.config.paper_trade_mode\n',
    ),
    (
        'main.py',
        [
            '                    try:\n                        self.run_one_cycle()\n                        self._last_cycle_ok_mono = time_module.monotonic()\n                    except Exception as e:\n                        self.logger.error(\n                            f"UNHANDLED ERROR in run_one_cycle: {e}"\n',
        ],
        '                    try:\n                        self.run_one_cycle()\n                        self._last_cycle_ok_mono = time_module.monotonic()\n                        # v7: feeds the watchdog\'s day-aware liveness clock in\n                        # _watchdog_once(). The field was initialised in\n                        # __init__ and never updated, so a process that spanned\n                        # midnight measured its idle from the previous session.\n                        self._last_cycle_ok_at   = now_ist()\n                    except Exception as e:\n                        self.logger.error(\n                            f"UNHANDLED ERROR in run_one_cycle: {e}"\n',
    ),
    (
        'backtest_engine.py',
        [
            '        config: Config,\n        fills: FillModel,\n        verbose: bool = False,\n    ):\n        self.store = store\n        self.config = config\n',
        ],
        '        config: Config,\n        fills: FillModel,\n        verbose: bool = False,\n        trade_report: Optional[str] = None,\n    ):\n        self.store = store\n        self.config = config\n',
    ),
    (
        'backtest_engine.py',
        [
            '        self.results = Results(float(config.starting_capital))\n        self._scratch: Optional[str] = None\n        self._sink = io.StringIO()\n\n    @contextlib.contextmanager\n    def _quiet(self):\n',
        ],
        '        self.results = Results(float(config.starting_capital))\n        self._scratch: Optional[str] = None\n        self._sink = io.StringIO()\n        # ── v7: per-trade console report ──────────────────────────────────\n        # trade_report overrides TRADE_REPORT_MODE from the config\n        # (each_cycle | on_change | off); None keeps the configured mode.\n        # The reporter itself is built in _build(), where the scratch book it\n        # reads exists.\n        self.trade_report_mode = trade_report\n        self.reporter = None\n\n    @contextlib.contextmanager\n    def _quiet(self):\n',
    ),
    (
        'backtest_engine.py',
        [
            '        self._sink.truncate(0)\n        with contextlib.redirect_stdout(self._sink):\n            yield\n\n    # -- engine wiring ----------------------------------------------------\n    def _build(self):\n',
        ],
        '        self._sink.truncate(0)\n        with contextlib.redirect_stdout(self._sink):\n            yield\n\n    # -- v7: per-trade console report -------------------------------------\n    def _report_trades(self, day: "DaySlice") -> int:\n        """Print the lifecycle block for every trade of the replayed session.\n\n        Called once per replayed cycle - including the cycles that end in a\n        `continue`, so the console shows the book on every cycle and not only\n        on the cycles that reached the entry decision. It is called OUTSIDE\n        _quiet(): the engine\'s own per-cycle narration is muted during a\n        replay because it buries the report, but this IS the report.\n\n        Returns how many blocks were printed. It cannot raise into the replay:\n        a rendering problem must never cost a session\'s results.\n        """\n        if self.reporter is None:\n            return 0\n        try:\n            return self.reporter.report_cycle(\n                trading_date=day.trading_date,\n                chain=(self.me.last_chain or {}),\n                as_of=self.clock.now(),\n            )\n        except Exception as exc:\n            if self.verbose:\n                print(f"  trade report failed: {exc}")\n            return 0\n\n    # -- engine wiring ----------------------------------------------------\n    def _build(self):\n',
    ),
    (
        'backtest_engine.py',
        [
            '        self.db, self.client, self.me, self.se, self.xe = db, client, me, se, xe\n        self.regime = rg\n        self.merge_regime = getattr(regime_engine, "merge_regime_into_signals", None)\n\n    def _teardown(self):\n        try:\n',
        ],
        '        self.db, self.client, self.me, self.se, self.xe = db, client, me, se, xe\n        self.regime = rg\n        self.merge_regime = getattr(regime_engine, "merge_regime_into_signals", None)\n\n        # v7: the same per-trade console block the live engine prints, driven\n        # off the scratch book this runner writes. It reads positions /\n        # position_legs, which is why _open() and _close() now persist the\n        # exit the way the live execute_close() does: without it a closed\n        # replay trade had no exit time, no exit price and no P&L anywhere in\n        # the database, and the block could only have been a reconstruction.\n        self.reporter = core.TradeConsoleReporter(\n            db, self.config, logger, source="BACKTEST"\n        )\n        self.reporter.set_mode(self.trade_report_mode)\n\n    def _teardown(self):\n        try:\n',
    ),
    (
        'backtest_engine.py',
        [
            '            "final_lots": lots,\n            "max_loss_per_lot": params.get("max_loss_per_lot"),\n            "total_max_risk": params.get("total_max_risk"),\n            "status": "OPEN",\n            "last_known_premium": credit,\n            "profit_lock_activated": 0,\n',
        ],
        '            "final_lots": lots,\n            "max_loss_per_lot": params.get("max_loss_per_lot"),\n            "total_max_risk": params.get("total_max_risk"),\n            # v7: the live execute_entry() persists this and the console\n            # report needs it to say what a credit structure commits; without\n            # it the replay book could only fall back to total_max_risk.\n            "estimated_margin": params.get("estimated_margin"),\n            "status": "OPEN",\n            "last_known_premium": credit,\n            "profit_lock_activated": 0,\n',
    ),
    (
        'backtest_engine.py',
        [
            '            px = self.fills.price(q, close_action, urgent=urgent)\n            if px is None or px <= 0:\n                px = f["exec_price"]\n            exit_legs.append({\n                "action": close_action,\n                "option_type": f["option_type"],\n                "exec_price": px,\n            })\n            debit += px if close_action == "BUY" else -px\n\n',
        ],
        '            px = self.fills.price(q, close_action, urgent=urgent)\n            if px is None or px <= 0:\n                px = f["exec_price"]\n            _bid = float(q.get("bid") or 0)\n            _ask = float(q.get("ask") or 0)\n            exit_legs.append({\n                "action": close_action,\n                "option_type": f["option_type"],\n                "strike": f["strike"],\n                "exec_price": px,\n                # recorded for the same reason the live book records\n                # quoted_mid_at_exit: slippage against the touch is only\n                # measurable if the touch at exit was written down.\n                "quoted_mid": ((_bid + _ask) / 2.0)\n                              if (_bid > 0 and _ask > 0) else px,\n            })\n            debit += px if close_action == "BUY" else -px\n\n',
    ),
    (
        'backtest_engine.py',
        [
            '        pnl = gross_pts * self.config.lot_size * lots - costs\n\n        now = self.clock.now()\n        self.db.update("positions", {"status": "CLOSED",\n                                     "updated_at": now.isoformat()},\n                       {"position_id": live["position_id"]})\n        self.db.execute(\n            "UPDATE position_legs SET leg_status=\'CLOSED\' WHERE position_id=?",\n            (live["position_id"],),\n        )\n\n        strikes = "/".join(\n            f"{f[\'action\'][0]}{f[\'option_type\'][0].upper()}{f[\'strike\']:.0f}"\n',
        ],
        '        pnl = gross_pts * self.config.lot_size * lots - costs\n\n        now = self.clock.now()\n        # v7: write the exit into the book the way the live execute_close()\n        # does. The replay used to flip two status flags and discard the rest,\n        # so a closed trade in the scratch database had no exit time, no exit\n        # reason, no P&L and no exit price on any leg - nothing that reads the\n        # book (the per-trade console report, an audit of a replay, any future\n        # reporter pointed at a saved run) could tell how the trade ended or\n        # what it made. Results/Trade still carries exactly the same numbers,\n        # so no replayed P&L moves: this is bookkeeping parity, not a change\n        # to the simulation.\n        _gross_rs = gross_pts * self.config.lot_size * lots\n        self.db.update(\n            "positions",\n            {\n                "status":            "CLOSED",\n                "exit_time":         now.isoformat(),\n                "exit_reason":       reason,\n                "exit_priority":     priority,\n                "exit_premium":      round(debit, 4),\n                "gross_pnl_rupees":  round(_gross_rs, 2),\n                "exit_costs_rupees": exit_costs,\n                "net_pnl_rupees":    round(pnl, 2),\n                "last_known_premium": round(debit, 4),\n                "updated_at":        now.isoformat(),\n            },\n            {"position_id": live["position_id"]},\n        )\n        for f, xl in zip(live["filled"], exit_legs):\n            self.db.execute(\n                "UPDATE position_legs SET leg_status=\'CLOSED\', exit_price=?, "\n                "quoted_mid_at_exit=? WHERE position_id=? AND strike=? AND "\n                "option_type=?",\n                (xl["exec_price"], xl["quoted_mid"], live["position_id"],\n                 f["strike"], f["option_type"]),\n            )\n\n        strikes = "/".join(\n            f"{f[\'action\'][0]}{f[\'option_type\'][0].upper()}{f[\'strike\']:.0f}"\n',
    ),
    (
        'backtest_engine.py',
        [
            '            except Exception as exc:\n                if self.verbose:\n                    print(f"  {trading_date} {dt:%H:%M}: run_cycle failed: {exc}")\n                continue\n            # reset_if_new_day() rebinds MarketDataEngine.state to a fresh\n            # dict on day rollover (including the first cycle, when the\n',
        ],
        '            except Exception as exc:\n                if self.verbose:\n                    print(f"  {trading_date} {dt:%H:%M}: run_cycle failed: {exc}")\n                self._report_trades(day)   # v7: a cycle is a cycle\n                continue\n            # reset_if_new_day() rebinds MarketDataEngine.state to a fresh\n            # dict on day rollover (including the first cycle, when the\n',
    ),
    (
        'backtest_engine.py',
        [
            '                    if self.verbose:\n                        print(f"  {trading_date} {dt:%H:%M} EXIT  "\n                              f"{t.exit_reason[:34]:34s} pnl={t.pnl_rs:>10,.0f}")\n                    continue\n\n            # ── daily loss halt ──────────────────────────────────────────\n',
        ],
        '                    if self.verbose:\n                        print(f"  {trading_date} {dt:%H:%M} EXIT  "\n                              f"{t.exit_reason[:34]:34s} pnl={t.pnl_rs:>10,.0f}")\n                    # v7: the block for the trade that just closed is printed\n                    # on the cycle that closed it, in its final state.\n                    self._report_trades(day)\n                    continue\n\n            # ── daily loss halt ──────────────────────────────────────────\n',
    ),
    (
        'backtest_engine.py',
        [
            '                              f"({day_pnl:,.0f}) — no further entries")\n            if state.get("daily_halted") and live is None:\n                self.results.add_rejection("daily_loss_halt")\n                continue\n\n            # ── otherwise consider a new entry ────────────────────────────\n',
        ],
        '                              f"({day_pnl:,.0f}) — no further entries")\n            if state.get("daily_halted") and live is None:\n                self.results.add_rejection("daily_loss_halt")\n                self._report_trades(day)   # v7\n                continue\n\n            # ── otherwise consider a new entry ────────────────────────────\n',
    ),
    (
        'backtest_engine.py',
        [
            '                except Exception as exc:\n                    if self.verbose:\n                        print(f"  decide() failed: {exc}")\n                    continue\n\n                if decision.get("action") == "ENTER":\n',
        ],
        '                except Exception as exc:\n                    if self.verbose:\n                        print(f"  decide() failed: {exc}")\n                    self._report_trades(day)   # v7\n                    continue\n\n                if decision.get("action") == "ENTER":\n',
    ),
    (
        'backtest_engine.py',
        [
            '                else:\n                    self.results.add_rejection(decision.get("reason", "unknown"))\n\n        # ── forced flat at the last snapshot of the session ──────────────\n        if live is not None:\n            self.clock.set(day.cycle_dt(day.cycles[-1]))\n',
        ],
        '                else:\n                    self.results.add_rejection(decision.get("reason", "unknown"))\n\n            # ── v7: end of the cycle ───────────────────────────────────────\n            # The book as it now stands: every trade of this session, the ones\n            # already performed and the one in progress, on every cycle.\n            self._report_trades(day)\n\n        # ── forced flat at the last snapshot of the session ──────────────\n        if live is not None:\n            self.clock.set(day.cycle_dt(day.cycles[-1]))\n',
    ),
    (
        'backtest_engine.py',
        [
            '                signals = {"spot": live["entry_spot"]}\n            t = self._close(live, signals, "END_OF_DATA_FORCED_FLAT", 7, day)\n            self.results.add_trade(t)\n\n    # -- driver -----------------------------------------------------------\n    def run(self, dates: List[str]) -> Results:\n',
        ],
        '                signals = {"spot": live["entry_spot"]}\n            t = self._close(live, signals, "END_OF_DATA_FORCED_FLAT", 7, day)\n            self.results.add_trade(t)\n            # v7: the session\'s last trade gets its final block too - the loop\n            # above ended before this close happened.\n            self._report_trades(day)\n\n    # -- driver -----------------------------------------------------------\n    def run(self, dates: List[str]) -> Results:\n',
    ),
    (
        'backtest_engine.py',
        [
            '    identity all get executed at least once per self-test run.\n    """\n    day = store.load_day(dates[0])\n    runner = BacktestRunner(store, cfg, FillModel(0.25, 0.5), verbose=False)\n    runner._build()\n    try:\n        entry_ct = day.cycles[len(day.cycles) // 4]\n',
        ],
        '    identity all get executed at least once per self-test run.\n    """\n    day = store.load_day(dates[0])\n    # trade_report is forced on so this coverage does not depend on what\n    # TRADE_REPORT_MODE / TRADE_REPORT_ENABLED say in env.txt.\n    runner = BacktestRunner(store, cfg, FillModel(0.25, 0.5), verbose=False,\n                            trade_report="each_cycle")\n    runner._build()\n    try:\n        entry_ct = day.cycles[len(day.cycles) // 4]\n',
    ),
    (
        'backtest_engine.py',
        [
            '            signals = runner.me.run_cycle()\n            runner.xe.monitor_position(dict(row), signals)\n\n        trade = runner._close(live, signals, "HARNESS_FORCED_EXIT", 7, day)\n\n        row = runner.db.query_one(\n',
        ],
        '            signals = runner.me.run_cycle()\n            runner.xe.monitor_position(dict(row), signals)\n\n        # ── v7: the per-trade console block, while the trade is open ──────\n        # The report is part of what this harness promises an operator, so the\n        # self-test renders it and asserts on its shape rather than trusting\n        # that it still works. This is the in-progress state: no exit fills\n        # yet, so the legs are marked at what closing them now would cost.\n        import contextlib as _ctxlib\n        import io as _io\n\n        def _render_blocks() -> Tuple[int, str]:\n            _buf = _io.StringIO()\n            with _ctxlib.redirect_stdout(_buf):\n                _n = runner._report_trades(day)\n            return _n, _buf.getvalue()\n\n        _n_open, _open_block = _render_blocks()\n        assert _n_open == 1, f"open trade rendered {_n_open} block(s), expected 1"\n        for _frag in ("Trade-1", "Strategy: HARNESS_FORCED_CONDOR",\n                      "Trade Start Data: time:", "Trade End Data: time:",\n                      "Position Status: Open", "Total Investment: Rs",\n                      "Total Profit: Rs"):\n            assert _frag in _open_block, f"open block missing {_frag!r}"\n        assert "\\nOpen\\n" in _open_block, "open block is missing the \'Open\' line"\n        assert "unrealised" in _open_block, \\\n            "an open trade must be marked, not reported as realised"\n        assert _open_block.count("=" * 84) == 2, "block must be ruled top and bottom"\n        assert _open_block.count("-" * 76) == 3, "block must have three sub-rules"\n        assert _open_block.count(" lot of ") == 8, \\\n            "4 legs must be printed twice: once at entry, once at the exit"\n\n        trade = runner._close(live, signals, "HARNESS_FORCED_EXIT", 7, day)\n\n        row = runner.db.query_one(\n',
    ),
    (
        'backtest_engine.py',
        [
            '            "WHERE position_id=? AND leg_status!=\'CLOSED\'",\n            (live["position_id"],))\n        assert open_legs["n"] == 0, "leg rows left open after close"\n\n        expect = round(\n            trade.gross_pts * cfg.lot_size * trade.lots - trade.costs_rs, 2)\n',
        ],
        '            "WHERE position_id=? AND leg_status!=\'CLOSED\'",\n            (live["position_id"],))\n        assert open_legs["n"] == 0, "leg rows left open after close"\n\n        # ── v7: the exit is persisted, not just flagged ───────────────────\n        # A closed replay trade used to leave exit_time, exit_reason,\n        # exit_premium, the P&L columns and every leg\'s exit_price empty, so\n        # nothing that reads the book could say how it ended.\n        exit_row = runner.db.query_one(\n            "SELECT exit_time, exit_reason, exit_priority, exit_premium, "\n            "gross_pnl_rupees, exit_costs_rupees, net_pnl_rupees "\n            "FROM positions WHERE position_id=?", (live["position_id"],))\n        assert exit_row["exit_time"], "exit_time not persisted"\n        assert exit_row["exit_reason"] == "HARNESS_FORCED_EXIT", \\\n            f"exit_reason not persisted: {exit_row[\'exit_reason\']}"\n        assert exit_row["exit_priority"] == 7, "exit_priority not persisted"\n        assert abs(float(exit_row["net_pnl_rupees"]) - trade.pnl_rs) < 0.01, \\\n            f"book net P&L {exit_row[\'net_pnl_rupees\']} != trade {trade.pnl_rs}"\n        assert abs(float(exit_row["gross_pnl_rupees"])\n                   - trade.gross_pts * cfg.lot_size * trade.lots) < 0.01, \\\n            "book gross P&L disagrees with the trade"\n        assert abs(float(exit_row["exit_costs_rupees"])\n                   - (trade.costs_rs - live["entry_costs"])) < 0.01, \\\n            "book exit costs disagree with the trade"\n        priced_legs = runner.db.query_one(\n            "SELECT COUNT(*) AS n FROM position_legs WHERE position_id=? "\n            "AND exit_price IS NOT NULL AND quoted_mid_at_exit IS NOT NULL",\n            (live["position_id"],))\n        assert priced_legs["n"] == 4, \\\n            f"exit fill persisted on {priced_legs[\'n\']}/4 legs"\n\n        # ── v7: and the console block now reports the closed trade ────────\n        _n_closed, _closed_block = _render_blocks()\n        assert _n_closed == 1, \\\n            f"closed trade rendered {_n_closed} block(s), expected 1"\n        for _frag in ("Closed - HARNESS_FORCED_EXIT", "Position Status: Close",\n                      "Total Profit: Rs"):\n            assert _frag in _closed_block, f"closed block missing {_frag!r}"\n        assert "unrealised" not in _closed_block, \\\n            "a closed trade must not be reported as unrealised"\n        assert f"Rs {trade.pnl_rs:+,.2f} realised" in _closed_block, (\n            f"the console block does not report the P&L the harness computed "\n            f"({trade.pnl_rs:+,.2f})")\n\n        expect = round(\n            trade.gross_pts * cfg.lot_size * trade.lots - trade.costs_rs, 2)\n',
    ),
    (
        'backtest_engine.py',
        [
            '            "gross_pts": trade.gross_pts,\n            "pnl_rs": trade.pnl_rs,\n            "held_min": trade.held_min,\n        }\n    finally:\n        runner._teardown()\n',
        ],
        '            "gross_pts": trade.gross_pts,\n            "pnl_rs": trade.pnl_rs,\n            "held_min": trade.held_min,\n            "report_open": _open_block,\n            "report_closed": _closed_block,\n        }\n    finally:\n        runner._teardown()\n',
    ),
    (
        'backtest_engine.py',
        [
            '    print(f"    closed after {fr[\'held_min\']} min, "\n          f"gross {fr[\'gross_pts\']:+.2f} pts, net Rs {fr[\'pnl_rs\']:+,.0f}  [OK]")\n    print(f"    positions/position_legs rows written and closed  [OK]")\n    print(f"    P&L identity reconciles to the paisa  [OK]")\n\n    import shutil\n    shutil.rmtree(tmp, ignore_errors=True)\n',
        ],
        '    print(f"    closed after {fr[\'held_min\']} min, "\n          f"gross {fr[\'gross_pts\']:+.2f} pts, net Rs {fr[\'pnl_rs\']:+,.0f}  [OK]")\n    print(f"    positions/position_legs rows written and closed  [OK]")\n    print(f"    exit time, reason, P&L and every leg\'s exit price persisted  [OK]")\n    print(f"    P&L identity reconciles to the paisa  [OK]")\n    print(f"    console block rendered open and closed, and the closed block")\n    print(f"    reports the P&L the harness computed  [OK]")\n    print()\n    print("    the per-trade console block, as it appears every cycle:")\n    for _line in fr["report_closed"].rstrip().splitlines():\n        print(f"      {_line}" if _line.strip() else "")\n\n    import shutil\n    shutil.rmtree(tmp, ignore_errors=True)\n',
    ),
    (
        'backtest_engine.py',
        [
            '    ap.add_argument("--audit", action="store_true", help="report data coverage only")\n    ap.add_argument("--test", action="store_true", help="run the harness self-test")\n    ap.add_argument("--verbose", action="store_true")\n    args = ap.parse_args()\n\n    if args.test:\n',
        ],
        '    ap.add_argument("--audit", action="store_true", help="report data coverage only")\n    ap.add_argument("--test", action="store_true", help="run the harness self-test")\n    ap.add_argument("--verbose", action="store_true")\n    ap.add_argument(\n        "--trade-report", dest="trade_report", default=None,\n        choices=("each_cycle", "on_change", "off"),\n        help="v7 per-trade console block. each_cycle (the default, and the "\n             "loudest) prints every trade of the session on every cycle; "\n             "on_change prints a trade only when it opens, closes, or its "\n             "unrealised P&L moves by TRADE_REPORT_MARK_EPS; off suppresses "\n             "it. Overrides TRADE_REPORT_MODE from the config.",\n    )\n    args = ap.parse_args()\n\n    if args.test:\n',
    ),
    (
        'backtest_engine.py',
        [
            '    print(f"REPLAYING {len(dates)} SESSION(S): {dates[0]} .. {dates[-1]}")\n    print(hr("═"))\n\n    runner = BacktestRunner(\n        store, config, FillModel(args.fill_edge, args.stress_exit), args.verbose\n    )\n    res = runner.run(dates)\n    print_report(res, config, args)\n',
        ],
        '    print(f"REPLAYING {len(dates)} SESSION(S): {dates[0]} .. {dates[-1]}")\n    print(hr("═"))\n\n    # v7: say what the console is about to do, because each_cycle mode prints\n    # a block per trade per cycle and the final summary ends up a long way\n    # above the bottom of the scrollback.\n    _tr_mode = str(\n        args.trade_report\n        or getattr(config, "trade_report_mode", "each_cycle")\n        or "each_cycle"\n    ).lower()\n    if getattr(config, "trade_report_enabled", True) and _tr_mode != "off":\n        print(f"  per-trade console report : {_tr_mode}"\n              + ("  (--trade-report=on_change for a quiet run)"\n                 if _tr_mode == "each_cycle" else ""))\n        print()\n\n    runner = BacktestRunner(\n        store, config, FillModel(args.fill_edge, args.stress_exit), args.verbose,\n        trade_report=args.trade_report,\n    )\n    res = runner.run(dates)\n    print_report(res, config, args)\n',
    ),
    (
        'eod_report.py',
        [
            'if not LOG_DIR.is_absolute():\n    LOG_DIR = BASE_DIR / LOG_DIR\n\nLOT_SIZE              = int(_ENV.get("NIFTY_LOT_SIZE", "75") or 75)\nSTARTING_CAPITAL      = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1_000_000)\nMAX_DAILY_LOSS_PCT    = float(_ENV.get("MAX_DAILY_LOSS_PCT", "0.02") or 0.02)\nBROKERAGE_PER_ORDER   = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)\nEXCHANGE_TXN_RATE     = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003552") or 0.0003552)\nSTT_OPTIONS_SELL      = float(_ENV.get("STT_OPTIONS_SELL", "0.001") or 0.001)\n\n\n# ─────────────────────────────────────────────────────────────────────────────\n',
        ],
        'if not LOG_DIR.is_absolute():\n    LOG_DIR = BASE_DIR / LOG_DIR\n\n# v7: these defaults must agree with the ones core.load_config() ships, or\n# this report describes a different engine from the one that traded. They did\n# not: LOT_SIZE defaulted to 75 (the pre-January-2026 NIFTY lot; the engine\n# has used 65 since the January 2026 series and core.py defaults to 65),\n# EXCHANGE_TXN_RATE to 0.0003552 against the engine\'s 0.0003553, STT to the\n# superseded 0.10% against the 0.15% that applies from 1 April 2026, and the\n# daily loss limit to 2% against the engine\'s 4%. env.txt holds only the\n# Upstox token by design, so in practice these defaults were the values in\n# force - which made cost_per_lot_rupees wrong by 15% before the metric\n# itself was even computed correctly.\nLOT_SIZE              = int(_ENV.get("NIFTY_LOT_SIZE", "65") or 65)\nSTARTING_CAPITAL      = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1_000_000)\nMAX_DAILY_LOSS_PCT    = float(_ENV.get("MAX_DAILY_LOSS_PCT", "0.04") or 0.04)\nBROKERAGE_PER_ORDER   = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)\nEXCHANGE_TXN_RATE     = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003553") or 0.0003553)\nSTT_OPTIONS_SELL      = float(_ENV.get("STT_OPTIONS_SELL", "0.0015") or 0.0015)\n\n\n# ─────────────────────────────────────────────────────────────────────────────\n',
    ),
    (
        'eod_report.py',
        [
            '\n    total_costs = sum(float(e.get("total_costs_rupees") or 0) for e in trade_exits)\n\n    return {\n        "total_trades":                n,\n        "total_actual_costs_rupees":   round(total_costs, 2),\n        "avg_actual_costs_rupees":     round(total_costs / n, 2) if n else 0,\n        "cost_per_lot_rupees":         round(total_costs / (n * LOT_SIZE), 2) if n else 0,\n        "avg_actual_slippage_pts":     round(statistics.mean(actual_slips), 3)\n                                       if actual_slips else None,\n        "max_actual_slippage_pts":     round(max(actual_slips), 3)\n',
        ],
        '\n    total_costs = sum(float(e.get("total_costs_rupees") or 0) for e in trade_exits)\n\n    # v7: "per lot" has to mean per LOT TRADED. The old expression divided by\n    # (number of trades x lot size), which is neither: it halved on every\n    # 2-lot trade, so the metric moved with position size instead of with\n    # cost, and it was computed on a 75-unit lot the engine has not traded\n    # since January 2026. Lots are summed over the entries that actually have\n    # an exit, because total_costs above is summed over the exits.\n    exited_ids = {\n        str(e.get("trade_id") or e.get("position_id") or "") for e in trade_exits\n    }\n    total_lots = 0\n    for entry in trade_entries:\n        key = str(entry.get("trade_id") or entry.get("position_id") or "")\n        if exited_ids and key not in exited_ids:\n            continue\n        try:\n            total_lots += int(float(entry.get("final_lots") or 0))\n        except (TypeError, ValueError):\n            pass\n    if total_lots <= 0:\n        total_lots = n\n    total_units = total_lots * LOT_SIZE\n\n    return {\n        "total_trades":                n,\n        "total_lots_traded":           total_lots,\n        "total_actual_costs_rupees":   round(total_costs, 2),\n        "avg_actual_costs_rupees":     round(total_costs / n, 2) if n else 0,\n        "cost_per_lot_rupees":         round(total_costs / total_lots, 2)\n                                       if total_lots else 0,\n        "cost_per_unit_rupees":        round(total_costs / total_units, 2)\n                                       if total_units else 0,\n        "avg_actual_slippage_pts":     round(statistics.mean(actual_slips), 3)\n                                       if actual_slips else None,\n        "max_actual_slippage_pts":     round(max(actual_slips), 3)\n',
    ),
]

PLANS = {"v6": PLAN_V6, "final": []}


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
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".patch_v7_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv):
    base = root(argv)
    missing = [r for r in TARGETS if not os.path.isfile(os.path.join(base, r))]
    if missing:
        print(f"ABORT: not found under {base}: {', '.join(missing)}")
        print("usage: python3 patch_v7.py [/path/to/repo]")
        return 2

    staged, notes = {}, []
    for rel in TARGETS:
        path = os.path.join(base, rel)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        digest = hashlib.md5(text.encode()).hexdigest()
        table = MD5[rel]

        if digest == table["final"]:
            staged[rel] = (text, "current", 0, len([
                h for h in PLANS["v6"] if h[0] == rel]))
            continue

        if digest != table["v6"]:
            notes.append(
                f"{rel}: content is neither the tested v6 state nor the v7 "
                f"result (got {digest[:12]}); trying the hunks anyway")

        mine = [h for h in PLANS["v6"] if h[0] == rel]
        if not mine and digest != table["final"]:
            print(f"ABORT: {rel}: this patch carries no hunk for that file and "
                  f"it is not the tested v7 content ({digest[:12]} != "
                  f"{table['final'][:12]}) - nothing was modified")
            return 3

        out, status, applied, skipped = apply_hunks(text, mine)
        if out is None:
            print(f"ABORT: {rel}: {status} - nothing was modified")
            print("The file has drifted from the tested v6 content. Restore it")
            print("(git checkout -- " + rel + ") or apply the missing patch.")
            return 3

        got = hashlib.md5(out.encode()).hexdigest()
        if got != table["final"]:
            print(f"ABORT: {rel} did not land on the tested v7 content")
            print(f"       got {got[:12]} expected {table['final'][:12]} "
                  f"- nothing was modified")
            return 3
        staged[rel] = (out, status, applied, skipped)

    print(f"patch_v7: patching {base}")
    for rel in TARGETS:
        _text, status, applied, skipped = staged[rel]
        print(f"  {rel:22s} {status:9s} applied={applied} already_present={skipped}")
    for n in notes:
        print(f"  note: {n}")

    backups, written = {}, []
    try:
        for rel, (text, status, _a, _s) in staged.items():
            path = os.path.join(base, rel)
            with open(path, "r", encoding="utf-8") as fh:
                backups[rel] = fh.read()
            if status not in ("unchanged", "current"):
                write_atomic(path, text)
                written.append(rel)
        for rel in TARGETS:
            py_compile.compile(os.path.join(base, rel), doraise=True)
    except Exception as exc:
        print(f"ABORT: {exc.__class__.__name__}: {exc} - reverting")
        for rel, text in backups.items():
            write_atomic(os.path.join(base, rel), text)
        return 4

    if written:
        print(f"patch_v7 applied: {len(written)} file(s) changed "
              f"({', '.join(written)})")
    else:
        print("patch_v7 applied: nothing to do - the tree is already at v7")
    print()
    for c in CHANGES:
        print(f"  + {c}")
    print()
    print("verify:  python3 verify_all.py")
    print("         python3 backtest_engine.py --test")
    print("         python3 execution_engine.py")
    print("replay:  python3 backtest_engine.py --db data/per_day/"
          "nifty_algo_2026-09-11.db --trade-report=each_cycle")
    print("report:  TRADE_REPORT_MODE=each_cycle|on_change|off in env.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))