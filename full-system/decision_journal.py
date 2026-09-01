#!/usr/bin/env python3
"""
decision_journal.py — Daily decision journal for LLM-assisted tuning.

Run at 15:35 IST every trading day after market close.
Assembles everything the engine did today into a structured JSON
that can be passed to an LLM for analysis and parameter suggestions.

Usage:
    python decision_journal.py                    # today
    python decision_journal.py --date 2026-09-01  # specific date
    python decision_journal.py --export-prompt    # also write LLM prompt
"""

import sqlite3
import json
import os
import sys
import argparse
import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Any
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────
# Schema: add these tables to state.db via init_sqlite in data_manager
# ─────────────────────────────────────────────────────────────────────

ADDITIONAL_SCHEMA = """
-- Every regime refresh cycle: what did the engine see and decide
CREATE TABLE IF NOT EXISTS regime_cycle_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    -- Market state
    spot                REAL,
    vix                 REAL,
    iv_atm              REAL,
    rv_20d              REAL,
    skew                REAL,
    forward_iv          REAL,
    adx                 REAL,
    ema_slope_pct       REAL,
    -- Module raw scores (before persistence)
    raw_vol             REAL,
    raw_edge            REAL,
    raw_trend           REAL,
    raw_flow            REAL,
    -- Module confirmed scores (after persistence)
    conf_vol            REAL,
    conf_edge           REAL,
    conf_trend          REAL,
    conf_flow           REAL,
    -- Weights actually used this cycle
    weight_vol          REAL,
    weight_edge         REAL,
    weight_trend        REAL,
    weight_flow         REAL,
    -- Composite and regime
    composite_score     REAL,
    confirmed_regime    TEXT,
    regime_changed      INTEGER,
    persistence_count   INTEGER,
    -- Entry gate result
    entry_gate_passed   INTEGER,
    entry_gate_blocked_reason TEXT,
    -- Active expiry info
    active_expiry       TEXT,
    active_expiry_dte   INTEGER
);

-- Every entry attempt: what was tried, what happened
CREATE TABLE IF NOT EXISTS entry_attempt_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    strategy_name       TEXT,
    regime_at_attempt   TEXT,
    composite_score     REAL,
    -- Build result
    build_result        TEXT,  -- SUCCESS, FAILED_CREDIT, FAILED_DTE, FAILED_SPREAD, etc.
    build_failure_reason TEXT,
    -- If built: what were the parameters
    expiry_date         TEXT,
    dte                 INTEGER,
    net_credit          REAL,
    min_credit_required REAL,
    wing_width          INTEGER,
    short_call_strike   REAL,
    short_put_strike    REAL,
    expected_move       REAL,
    -- Pre-trade check result
    pretrade_passed     INTEGER,
    pretrade_fail_reason TEXT,
    -- Execution result
    execution_result    TEXT,  -- FILLED, PARTIAL, REJECTED, ABORTED
    lots_requested      INTEGER,
    lots_filled         INTEGER,
    actual_credit       REAL,
    -- Sizing inputs
    vix_at_entry        REAL,
    vix_adaptive_mult   REAL,
    max_risk_used       REAL,
    -- If trade opened: trade_id
    trade_id            TEXT
);

-- Every monitoring cycle for open positions
CREATE TABLE IF NOT EXISTS position_monitor_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    trade_id            TEXT NOT NULL,
    strategy_name       TEXT,
    -- Current state
    current_premium     REAL,
    stop_loss           REAL,
    profit_target       REAL,
    unrealized_pnl      REAL,
    -- Stop checks
    stop_breach_ticks   INTEGER,
    distance_to_call_pct REAL,
    distance_to_put_pct  REAL,
    -- Partial profit
    partial_taken       INTEGER,
    -- Decision
    action_taken        TEXT,  -- HOLD, STOP_BREACH_TICK, PARTIAL_PROFIT, CLOSED_TARGET, CLOSED_STOP
    spot                REAL,
    vix                 REAL
);
"""


def ensure_logging_tables(db_path: str) -> None:
    """Add decision logging tables to existing state.db."""
    conn = sqlite3.connect(db_path)
    for statement in ADDITIONAL_SCHEMA.strip().split(";"):
        statement = statement.strip()
        if statement:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # table already exists
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# Data assembly
# ─────────────────────────────────────────────────────────────────────

def _get_table_columns(conn: sqlite3.Connection, table: str) -> set:
    """IMP-01: return the set of column names for a table."""
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    except Exception:
        return set()


def get_today_trades(conn: sqlite3.Connection, trading_date: str) -> List[Dict]:
    """FIX-01: schema-safe trade query — only selects columns that exist."""
    # Core columns that must exist
    core_cols = [
        "trade_id", "strategy_name", "regime_at_entry",
        "entry_timestamp", "exit_timestamp",
        "entry_spot", "entry_vix",
        "realized_pnl", "transaction_costs", "net_pnl",
        "exit_reason",
    ]
    # Optional columns — included only if they exist in the schema
    optional_cols = [
        "regime_at_exit", "holding_days",
        "exit_spot", "exit_vix",
        "total_credit_received", "total_debit_paid", "net_premium",
        "max_risk", "realized_pnl_percent",
        "composite_score_at_entry", "vol_score", "edge_score",
        "trend_score", "flow_score",
        "days_to_expiry_at_entry", "expiry_date",
        "stop_loss", "profit_target", "slippage_total_points",
    ]
    try:
        existing = _get_table_columns(conn, "closed_trades")
        select_cols = [
            c for c in core_cols + optional_cols
            if c in existing
        ]
        if not select_cols:
            return []
        cols_str = ", ".join(select_cols)
        cursor = conn.execute(
            f"SELECT {cols_str} FROM closed_trades "
            f"WHERE DATE(entry_timestamp) = ? "
            f"OR DATE(exit_timestamp) = ? "
            f"ORDER BY entry_timestamp",
            (trading_date, trading_date),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"get_today_trades: {e}")
        return []


def get_open_positions(conn: sqlite3.Connection) -> List[Dict]:
    """FIX-02: schema-safe open positions query."""
    optional = [
        "trade_id", "strategy_name", "regime_at_entry",
        "entry_timestamp", "entry_spot", "entry_vix",
        "total_credit", "total_debit", "net_premium", "max_risk",
        "stop_loss", "profit_target", "expiry_date",
        "days_to_expiry", "legs_json",
    ]
    try:
        existing = _get_table_columns(conn, "open_positions")
        select_cols = [c for c in optional if c in existing]
        if not select_cols:
            return []
        cols_str = ", ".join(select_cols)
        cursor = conn.execute(
            f"SELECT {cols_str} FROM open_positions "
            f"WHERE status = 'OPEN'"
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"get_open_positions: {e}")
        return []


def get_regime_cycles_today(conn: sqlite3.Connection, trading_date: str) -> List[Dict]:
    """FIX-03: graceful fallback when regime_cycle_log doesn't exist yet."""
    try:
        existing = _get_table_columns(conn, "regime_cycle_log")
        if not existing:
            return []
        want = [
            "timestamp", "spot", "vix", "iv_atm", "rv_20d",
            "adx", "ema_slope_pct",
            "raw_vol", "raw_edge", "raw_trend", "raw_flow",
            "conf_vol", "conf_edge", "conf_trend", "conf_flow",
            "weight_vol", "weight_edge", "weight_trend", "weight_flow",
            "composite_score", "confirmed_regime", "regime_changed",
            "persistence_count", "entry_gate_passed",
            "entry_gate_blocked_reason", "active_expiry_dte",
        ]
        select_cols = [c for c in want if c in existing]
        if not select_cols:
            return []
        cols_str = ", ".join(select_cols)
        cursor = conn.execute(
            f"SELECT {cols_str} FROM regime_cycle_log "
            f"WHERE DATE(timestamp) = ? ORDER BY timestamp",
            (trading_date,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        logger.debug(f"get_regime_cycles_today: {e}")
        return []


def get_entry_attempts_today(conn: sqlite3.Connection, trading_date: str) -> List[Dict]:
    """FIX-04: graceful fallback when entry_attempt_log doesn't exist yet."""
    try:
        existing = _get_table_columns(conn, "entry_attempt_log")
        if not existing:
            return []
        want = [
            "timestamp", "strategy_name", "regime_at_attempt",
            "composite_score", "build_result", "build_failure_reason",
            "expiry_date", "dte", "net_credit", "min_credit_required",
            "wing_width", "expected_move", "pretrade_passed",
            "pretrade_fail_reason", "execution_result",
            "lots_requested", "lots_filled", "actual_credit",
            "vix_at_entry", "vix_adaptive_mult", "max_risk_used", "trade_id",
        ]
        select_cols = [c for c in want if c in existing]
        if not select_cols:
            return []
        cols_str = ", ".join(select_cols)
        cursor = conn.execute(
            f"SELECT {cols_str} FROM entry_attempt_log "
            f"WHERE DATE(timestamp) = ? ORDER BY timestamp",
            (trading_date,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        logger.debug(f"get_entry_attempts_today: {e}")
        return []


def get_regime_history_today(conn: sqlite3.Connection, trading_date: str) -> List[Dict]:
    cursor = conn.execute("""
        SELECT timestamp, vol_score, edge_score, trend_score, flow_score,
               composite_score, confirmed_regime, persistence_count, macro_override
        FROM regime_history
        WHERE DATE(timestamp) = ?
        ORDER BY timestamp
    """, (trading_date,))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_market_state_eod(conn: sqlite3.Connection, trading_date: str) -> Dict:
    """FIX-05: schema-safe market state query."""
    try:
        existing = _get_table_columns(conn, "market_state")
        if not existing:
            return {}
        want = [
            "spot", "vix", "iv_atm", "rv_20d", "skew",
            "adx", "ema_50", "composite_score", "regime",
        ]
        select_cols = [c for c in want if c in existing]
        if not select_cols:
            return {}
        cols_str = ", ".join(select_cols)
        cursor = conn.execute(
            f"SELECT {cols_str} FROM market_state "
            f"WHERE DATE(timestamp) = ? "
            f"ORDER BY timestamp DESC LIMIT 1",
            (trading_date,),
        )
        cols = [d[0] for d in cursor.description]
        row = cursor.fetchone()
        return dict(zip(cols, row)) if row else {}
    except Exception as e:
        logger.warning(f"get_market_state_eod: {e}")
        return {}


def get_circuit_breakers_today(conn: sqlite3.Connection, trading_date: str) -> List[Dict]:
    """FIX-06: safe circuit breaker query."""
    try:
        existing = _get_table_columns(conn, "circuit_breaker_log")
        if not existing:
            return []
        want = ["timestamp", "level", "trigger", "action",
                "daily_pnl", "drawdown", "regime"]
        select_cols = [c for c in want if c in existing]
        if not select_cols:
            return []
        cols_str = ", ".join(select_cols)
        cursor = conn.execute(
            f"SELECT {cols_str} FROM circuit_breaker_log "
            f"WHERE DATE(timestamp) = ?",
            (trading_date,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        logger.debug(f"get_circuit_breakers_today: {e}")
        return []


def get_capital_state(conn: sqlite3.Connection) -> Dict:
    """FIX-06b: schema-safe capital state query."""
    try:
        existing = _get_table_columns(conn, "engine_capital_state")
        if not existing:
            return {}
        want = [
            "current_capital", "peak_capital", "weekly_pnl", "daily_pnl",
            "cb_level_2_active", "cb_level_3_active", "cb_level_4_active",
            "kill_switch_active", "daily_trading_halted",
        ]
        select_cols = [c for c in want if c in existing]
        if not select_cols:
            return {}
        cols_str = ", ".join(select_cols)
        cursor = conn.execute(
            f"SELECT {cols_str} FROM engine_capital_state WHERE id = 1"
        )
        cols = [d[0] for d in cursor.description]
        row = cursor.fetchone()
        return dict(zip(cols, row)) if row else {}
    except Exception as e:
        logger.debug(f"get_capital_state: {e}")
        return {}


def get_current_parameters() -> Dict:
    """Snapshot all tunable parameters from config."""
    return {
        # Regime weights
        "WEIGHT_VOL": config.WEIGHT_VOL,
        "WEIGHT_EDGE": config.WEIGHT_EDGE,
        "WEIGHT_TREND": config.WEIGHT_TREND,
        "WEIGHT_FLOW": config.WEIGHT_FLOW,
        # Regime thresholds
        "STRONG_SELL_ENTER": getattr(config, "STRONG_SELL_ENTER", 0.45),
        "STRONG_SELL_EXIT": getattr(config, "STRONG_SELL_EXIT", 0.35),
        "MILD_SELL_ENTER": getattr(config, "MILD_SELL_ENTER", 0.15),
        "MILD_SELL_EXIT": getattr(config, "MILD_SELL_EXIT", 0.05),
        # Strategy parameters
        "CONDOR_SIGMA_MULTIPLIER": config.CONDOR_SIGMA_MULTIPLIER,
        "CONDOR_MIN_CREDIT_PCT_OF_WIDTH": config.CONDOR_MIN_CREDIT_PCT_OF_WIDTH,
        "CONDOR_WING_WIDTH": config.CONDOR_WING_WIDTH,
        "CONDOR_TARGET_PCT": config.CONDOR_TARGET_PCT,
        "STRADDLE_TARGET_PCT": config.STRADDLE_TARGET_PCT,
        "SPREAD_TARGET_PCT": config.SPREAD_TARGET_PCT,
        "STRADDLE_STOP_MULT": config.STRADDLE_STOP_MULT,
        # Stop parameters
        "STOP_BREACH_TICKS_REQUIRED": getattr(config, "STOP_BREACH_TICKS_REQUIRED", 3),
        "STOP_SPOT_FRACTION_OF_DISTANCE": getattr(config, "STOP_SPOT_FRACTION_OF_DISTANCE", 0.80),
        # Sizing
        "MAX_RISK_PER_TRADE_PCT": config.MAX_RISK_PER_TRADE_PCT,
        "VIX_ADAPTIVE_SIZING": getattr(config, "VIX_ADAPTIVE_SIZING", True),
        "VIX_ADAPTIVE_REFERENCE": getattr(config, "VIX_ADAPTIVE_REFERENCE", 16.0),
        # Timing
        "EXEC_START_TIME": str(config.EXEC_START_TIME),
        "EXEC_END_TIME": str(config.EXEC_END_TIME),
        "TIME_EXIT_EXPIRY": str(config.TIME_EXIT_EXPIRY),
        "CONDOR_EXIT_DTE": config.CONDOR_EXIT_DTE,
        "SPREAD_EXIT_DTE": config.SPREAD_EXIT_DTE,
        # Persistence
        "ADAPTIVE_PERSISTENCE_ENABLED": getattr(config, "ADAPTIVE_PERSISTENCE_ENABLED", True),
        "ADAPTIVE_PERSISTENCE_FAST_THRESHOLD": getattr(config, "ADAPTIVE_PERSISTENCE_FAST_THRESHOLD", 0.60),
        # Partial profit
        "PARTIAL_PROFIT_ENABLED": getattr(config, "PARTIAL_PROFIT_ENABLED", True),
        "PARTIAL_PROFIT_TRIGGER_PCT": getattr(config, "PARTIAL_PROFIT_TRIGGER_PCT", 0.25),
        "PARTIAL_PROFIT_CLOSE_PCT": getattr(config, "PARTIAL_PROFIT_CLOSE_PCT", 0.50),
        # Flow weight
        "FLOW_WEIGHT_NONE_THRESHOLD": getattr(config, "FLOW_WEIGHT_NONE_THRESHOLD", 0.50),
    }


def compute_daily_statistics(
    trades: List[Dict],
    regime_cycles: List[Dict],
    entry_attempts: List[Dict],
) -> Dict:
    """Compute summary statistics for the day."""
    stats = {}

    # Trade statistics
    if trades:
        closed_today = [t for t in trades if t.get("exit_timestamp")]
        opened_today = [t for t in trades if t.get("entry_timestamp")]
        pnls = [t["net_pnl"] for t in closed_today if t.get("net_pnl") is not None]

        stats["trades_opened"] = len(opened_today)
        stats["trades_closed"] = len(closed_today)
        stats["total_net_pnl"] = round(sum(pnls), 2) if pnls else 0
        stats["win_rate"] = round(
            sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1
        ) if pnls else None
        stats["avg_pnl_per_trade"] = round(
            sum(pnls) / len(pnls), 2
        ) if pnls else None
        stats["exit_reasons"] = {}
        for t in closed_today:
            reason = t.get("exit_reason", "UNKNOWN")
            stats["exit_reasons"][reason] = stats["exit_reasons"].get(reason, 0) + 1
    else:
        stats["trades_opened"] = 0
        stats["trades_closed"] = 0
        stats["total_net_pnl"] = 0

    # Regime statistics
    if regime_cycles:
        regimes_seen = [r["confirmed_regime"] for r in regime_cycles if r.get("confirmed_regime")]
        stats["regime_changes"] = sum(1 for r in regime_cycles if r.get("regime_changed"))
        stats["dominant_regime"] = max(set(regimes_seen), key=regimes_seen.count) if regimes_seen else None
        stats["regime_distribution"] = {}
        for r in regimes_seen:
            stats["regime_distribution"][r] = stats["regime_distribution"].get(r, 0) + 1
        # Normalize to percentages
        total = sum(stats["regime_distribution"].values())
        if total > 0:
            stats["regime_distribution"] = {
                k: round(v / total * 100, 1)
                for k, v in stats["regime_distribution"].items()
            }

        # Average module scores
        for module in ["raw_vol", "raw_edge", "raw_trend", "raw_flow", "composite_score"]:
            vals = [r[module] for r in regime_cycles if r.get(module) is not None]
            if vals:
                stats[f"avg_{module}"] = round(sum(vals) / len(vals), 3)

        # Entry gate block reasons
        blocked = [r["entry_gate_blocked_reason"] for r in regime_cycles
                   if r.get("entry_gate_blocked_reason")]
        stats["entry_blocks"] = {}
        for reason in blocked:
            if reason:
                # Extract first word as category
                cat = reason.split(":")[0].strip() if ":" in reason else reason[:30]
                stats["entry_blocks"][cat] = stats["entry_blocks"].get(cat, 0) + 1

    # Entry attempt statistics
    if entry_attempts:
        stats["build_attempts"] = len(entry_attempts)
        results = {}
        for a in entry_attempts:
            r = a.get("build_result", "UNKNOWN")
            results[r] = results.get(r, 0) + 1
        stats["build_results"] = results

        # Credit analysis: how often did credit floor block us?
        credit_blocks = [
            a for a in entry_attempts
            if a.get("build_result") == "FAILED_CREDIT"
        ]
        if credit_blocks:
            avg_achieved = sum(
                a["net_credit"] for a in credit_blocks if a.get("net_credit")
            ) / len(credit_blocks)
            avg_required = sum(
                a["min_credit_required"] for a in credit_blocks
                if a.get("min_credit_required")
            ) / len(credit_blocks)
            stats["credit_block_avg_achieved"] = round(avg_achieved, 2)
            stats["credit_block_avg_required"] = round(avg_required, 2)
            stats["credit_gap"] = round(avg_required - avg_achieved, 2)

    return stats


# ─────────────────────────────────────────────────────────────────────
# Journal assembly
# ─────────────────────────────────────────────────────────────────────

def init_all_tables(db_path: str) -> None:
    """IMP-02: create all required tables upfront.

    Run with --init before first use to ensure all logging
    tables exist even before the engine has run.
    """
    conn = sqlite3.connect(db_path)
    ensure_logging_tables(db_path)
    # Also create regime_history if missing (engine creates it but
    # journal reads it — ensure it exists for safe querying)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regime_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT    NOT NULL,
            vol_score         REAL,
            edge_score        REAL,
            trend_score       REAL,
            flow_score        REAL,
            composite_score   REAL,
            raw_regime        TEXT,
            confirmed_regime  TEXT,
            persistence_count INTEGER,
            macro_override    INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"All tables initialised in {db_path}")


def get_trailing_pnl_context(conn: sqlite3.Connection, trading_date: str) -> Dict:
    """IMP-05: compute trailing P&L context for trend analysis."""
    try:
        existing = _get_table_columns(conn, "closed_trades")
        if "net_pnl" not in existing or "exit_timestamp" not in existing:
            return {}
        # 7-day trailing
        row7 = conn.execute("""
            SELECT COUNT(*) as cnt,
                   SUM(net_pnl) as total,
                   AVG(net_pnl) as avg_pnl,
                   SUM(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0 END)
                   / MAX(COUNT(*), 1) * 100 as win_rate
            FROM closed_trades
            WHERE exit_timestamp IS NOT NULL
            AND DATE(exit_timestamp) BETWEEN
                DATE(?, '-7 days') AND DATE(?)
        """, (trading_date, trading_date)).fetchone()
        # 30-day trailing
        row30 = conn.execute("""
            SELECT COUNT(*) as cnt,
                   SUM(net_pnl) as total,
                   AVG(net_pnl) as avg_pnl,
                   SUM(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0 END)
                   / MAX(COUNT(*), 1) * 100 as win_rate
            FROM closed_trades
            WHERE exit_timestamp IS NOT NULL
            AND DATE(exit_timestamp) BETWEEN
                DATE(?, '-30 days') AND DATE(?)
        """, (trading_date, trading_date)).fetchone()
        return {
            "trailing_7d": {
                "trades": row7[0] or 0,
                "total_pnl": round(row7[1] or 0, 2),
                "avg_pnl": round(row7[2] or 0, 2),
                "win_rate": round(row7[3] or 0, 1),
            },
            "trailing_30d": {
                "trades": row30[0] or 0,
                "total_pnl": round(row30[1] or 0, 2),
                "avg_pnl": round(row30[2] or 0, 2),
                "win_rate": round(row30[3] or 0, 1),
            },
        }
    except Exception as e:
        logger.debug(f"get_trailing_pnl_context: {e}")
        return {}


def assemble_daily_journal(trading_date: str, db_path: str) -> Dict:
    """Assemble the complete daily decision journal."""
    conn = sqlite3.connect(db_path)

    journal = {
        "journal_version": "1.0",
        "trading_date": trading_date,
        "generated_at": datetime.now(IST).isoformat(),
        "engine_version": "arena-algo-trading",

        # Current parameter snapshot
        "parameters": get_current_parameters(),

        # Market context
        "market_state_eod": get_market_state_eod(conn, trading_date),

        # What the engine decided throughout the day
        "regime_cycles": get_regime_cycles_today(conn, trading_date),
        "regime_history": get_regime_history_today(conn, trading_date),

        # Entry decisions
        "entry_attempts": get_entry_attempts_today(conn, trading_date),

        # Trade outcomes
        "trades_today": get_today_trades(conn, trading_date),
        "open_positions": get_open_positions(conn),

        # Risk events
        "circuit_breakers": get_circuit_breakers_today(conn, trading_date),

        # Capital state
        "capital_state": get_capital_state(conn),

        # IMP-05: trailing P&L context for trend analysis
        "trailing_pnl": get_trailing_pnl_context(conn, trading_date),
    }

    conn.close()

    # Compute summary statistics
    journal["daily_statistics"] = compute_daily_statistics(
        journal["trades_today"],
        journal["regime_cycles"],
        journal["entry_attempts"],
    )

    return journal


# ─────────────────────────────────────────────────────────────────────
# LLM prompt generator
# ─────────────────────────────────────────────────────────────────────

def generate_llm_prompt(journal: Dict) -> str:
    """
    Generate a structured prompt for LLM analysis.
    Designed to be passed to Claude, GPT-4, or similar.
    """
    j = journal
    stats = j.get("daily_statistics", {})
    params = j.get("parameters", {})
    market = j.get("market_state_eod", {})
    capital = j.get("capital_state", {})

    # FIX-07: ensure all values have safe defaults before formatting
    _safe_stats = stats or {}
    _safe_market = market or {}
    _safe_capital = capital or {}

    prompt = f"""You are analyzing a NIFTY options algorithmic trading engine.
Today is {j['trading_date']}. Review the engine's decisions and suggest parameter improvements.

## ENGINE CONTEXT
This is a regime-based premium-selling engine for NIFTY weekly options.
It uses 4 modules (Vol Surface, Edge/VRP, Trend, Flow) weighted into a composite score
to classify market regime, then selects and executes options strategies.

## TODAY'S MARKET CONDITIONS
- NIFTY Spot: {market.get('spot', 'N/A')}
- India VIX: {market.get('vix', 'N/A')}
- ATM IV: {market.get('iv_atm', 'N/A')}
- 20-day RV: {market.get('rv_20d', 'N/A')}
- ADX: {market.get('adx', 'N/A')}
- EOD Regime: {market.get('regime', 'N/A')}
- Composite Score: {market.get('composite_score', 'N/A')}

## TODAY'S TRADING SUMMARY
- Trades Opened: {_safe_stats.get('trades_opened', 0)}
- Trades Closed: {_safe_stats.get('trades_closed', 0)}
- Total Net P&L: \u20b9{_safe_stats.get('total_net_pnl', 0):,.0f}
- Win Rate: {_safe_stats.get('win_rate', 'N/A')}%
- Avg P&L per Trade: \u20b9{_safe_stats.get('avg_pnl_per_trade', 'N/A')}
- Exit Reasons: {json.dumps(_safe_stats.get('exit_reasons', {}), indent=2)}

## REGIME BEHAVIOR TODAY
- Regime Changes: {stats.get('regime_changes', 0)}
- Dominant Regime: {stats.get('dominant_regime', 'N/A')}
- Regime Distribution (%): {json.dumps(stats.get('regime_distribution', {}), indent=2)}
- Avg Vol Score: {stats.get('avg_raw_vol', 'N/A')}
- Avg Edge Score: {stats.get('avg_raw_edge', 'N/A')}
- Avg Trend Score: {stats.get('avg_raw_trend', 'N/A')}
- Avg Flow Score: {stats.get('avg_raw_flow', 'N/A')}
- Avg Composite: {stats.get('avg_composite_score', 'N/A')}

## ENTRY GATE ANALYSIS
- Build Attempts: {stats.get('build_attempts', 0)}
- Build Results: {json.dumps(stats.get('build_results', {}), indent=2)}
- Entry Blocks by Reason: {json.dumps(stats.get('entry_blocks', {}), indent=2)}
"""

    # Credit gap analysis
    if stats.get('credit_gap'):
        prompt += f"""
## CREDIT FLOOR ANALYSIS
- Average Credit Achieved: {stats.get('credit_block_avg_achieved', 'N/A')} pts
- Average Credit Required: {stats.get('credit_block_avg_required', 'N/A')} pts
- Credit Gap (required - achieved): {stats.get('credit_gap', 'N/A')} pts
NOTE: If credit gap > 0, the minimum credit floor is blocking viable trades.
"""

    # Trade details
    trades = j.get("trades_today", [])
    if trades:
        prompt += "\n## INDIVIDUAL TRADE DETAILS\n"
        for t in trades:
            prompt += f"""
Trade: {t.get('strategy_name')} | Regime: {t.get('regime_at_entry')}
  Entry: spot={t.get('entry_spot')} vix={t.get('entry_vix')} dte={t.get('days_to_expiry_at_entry')}
  Composite at entry: {t.get('composite_score_at_entry')}
  Module scores: vol={t.get('vol_score')} edge={t.get('edge_score')} trend={t.get('trend_score')} flow={t.get('flow_score')}
  Credit: ₹{t.get('total_credit_received', 0) * 65:.0f} | Max Risk: ₹{t.get('max_risk', 0):.0f}
  Exit: {t.get('exit_reason')} | Net P&L: ₹{t.get('net_pnl', 0):.0f} ({t.get('realized_pnl_percent', 0):.1f}%)
  Slippage: {t.get('slippage_total_points', 0):.1f} pts
"""

    # Open positions
    open_pos = j.get("open_positions", [])
    if open_pos:
        prompt += f"\n## OPEN POSITIONS GOING INTO TOMORROW ({len(open_pos)} positions)\n"
        for p in open_pos:
            prompt += f"  {p.get('strategy_name')} | expiry={p.get('expiry_date')} dte={p.get('days_to_expiry')} | credit=₹{p.get('total_credit', 0)*65:.0f}\n"

    # Capital state
    if capital:
        prompt += f"""
## CAPITAL STATE
- Current Capital: ₹{capital.get('current_capital', 0):,.0f}
- Peak Capital: ₹{capital.get('peak_capital', 0):,.0f}
- Daily P&L: ₹{capital.get('daily_pnl', 0):,.0f}
- Weekly P&L: ₹{capital.get('weekly_pnl', 0):,.0f}
- Kill Switch: {capital.get('kill_switch_active', False)}
- Daily Halted: {capital.get('daily_trading_halted', False)}
"""

    # Circuit breakers
    cbs = j.get("circuit_breakers", [])
    if cbs:
        prompt += "\n## CIRCUIT BREAKER EVENTS TODAY\n"
        for cb in cbs:
            prompt += f"  Level {cb.get('level')}: {cb.get('trigger')} -> {cb.get('action')}\n"

    # Current parameters
    # IMP-04: module signal quality
    _cycles = j.get("regime_cycles", [])
    if _cycles:
        _total = len(_cycles)
        for _mod in ["raw_vol", "raw_edge", "raw_trend", "raw_flow"]:
            _none_count = sum(
                1 for c in _cycles if c.get(_mod) is None
            )
            _pos = sum(
                1 for c in _cycles
                if c.get(_mod) is not None and c[_mod] > 0
            )
            _neg = sum(
                1 for c in _cycles
                if c.get(_mod) is not None and c[_mod] < 0
            )
            prompt += (
                f"  {_mod:<12}: "
                f"None={_none_count}/{_total} "
                f"(+)={_pos} "
                f"(-)={_neg}\n"
            )
    else:
        prompt += "  No regime cycle data yet (engine not yet running)\n"

    # IMP-05: trailing P&L context
    _trailing = j.get("trailing_pnl", {})
    _t7 = _trailing.get("trailing_7d", {})
    _t30 = _trailing.get("trailing_30d", {})
    if _t7 or _t30:
        prompt += f"""
## TRAILING P&L CONTEXT
- Last 7 days:  {_t7.get('trades', 0)} trades | """
        prompt += (
            f"P&L \u20b9{_t7.get('total_pnl', 0):,.0f} | "
            f"Win {_t7.get('win_rate', 0):.1f}%\n"
        )
        prompt += f"- Last 30 days: {_t30.get('trades', 0)} trades | "
        prompt += (
            f"P&L \u20b9{_t30.get('total_pnl', 0):,.0f} | "
            f"Win {_t30.get('win_rate', 0):.1f}%\n"
        )

    prompt += f"""
## CURRENT PARAMETERS (what was active today)
Regime Weights: VOL={params.get('WEIGHT_VOL')} EDGE={params.get('WEIGHT_EDGE')} TREND={params.get('WEIGHT_TREND')} FLOW={params.get('WEIGHT_FLOW')}
Regime Thresholds: STRONG_SELL_ENTER={params.get('STRONG_SELL_ENTER')} MILD_SELL_ENTER={params.get('MILD_SELL_ENTER')}
Condor: sigma_mult={params.get('CONDOR_SIGMA_MULTIPLIER')} min_credit_pct={params.get('CONDOR_MIN_CREDIT_PCT_OF_WIDTH')} wing={params.get('CONDOR_WING_WIDTH')}
Targets: condor={params.get('CONDOR_TARGET_PCT')} straddle={params.get('STRADDLE_TARGET_PCT')} spread={params.get('SPREAD_TARGET_PCT')}
Stops: straddle_mult={params.get('STRADDLE_STOP_MULT')} breach_ticks={params.get('STOP_BREACH_TICKS_REQUIRED')} dist_frac={params.get('STOP_SPOT_FRACTION_OF_DISTANCE')}
Sizing: max_risk_pct={params.get('MAX_RISK_PER_TRADE_PCT')} vix_adaptive={params.get('VIX_ADAPTIVE_SIZING')} vix_ref={params.get('VIX_ADAPTIVE_REFERENCE')}
Partial profit: enabled={params.get('PARTIAL_PROFIT_ENABLED')} trigger={params.get('PARTIAL_PROFIT_TRIGGER_PCT')} close={params.get('PARTIAL_PROFIT_CLOSE_PCT')}
Persistence: adaptive={params.get('ADAPTIVE_PERSISTENCE_ENABLED')} fast_threshold={params.get('ADAPTIVE_PERSISTENCE_FAST_THRESHOLD')}
"""

    prompt += """
## YOUR TASK
Based on today's data, answer these specific questions:

1. REGIME ACCURACY: Were the regime signals aligned with what actually happened in the market today? If the engine was in STRONG_SELL but the market moved strongly directionally, the regime was wrong — what should change?

2. ENTRY EFFICIENCY: How many entry attempts failed and why? If credit floor is blocking most attempts, should CONDOR_MIN_CREDIT_PCT_OF_WIDTH be lowered? If DTE is blocking, should DTE windows change?

3. EXIT QUALITY: For trades that closed today, did they exit at the right time? Stop-outs that could have been avoided? Profit targets that were too conservative?

4. PARAMETER SUGGESTIONS: Based on today's data only, suggest specific numeric changes to parameters. Format each suggestion as:
   PARAMETER_NAME: current_value -> suggested_value | reason: [one sentence]

5. WATCH LIST FOR TOMORROW: What specific conditions should the operator watch for tomorrow given today's data?

6. CONFIDENCE: Rate your confidence in each suggestion (HIGH/MEDIUM/LOW) and explain what additional data would increase confidence.

Be specific and quantitative. Avoid generic advice. Every suggestion must reference specific numbers from today's data.
"""

    return prompt


# ─────────────────────────────────────────────────────────────────────
# Cumulative analysis
# ─────────────────────────────────────────────────────────────────────

def generate_cumulative_prompt(db_path: str, lookback_days: int = 30) -> str:
    """
    Generate a cumulative analysis prompt covering the last N days.
    Run weekly or when you have 20+ trades.
    """
    conn = sqlite3.connect(db_path)
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

    # All trades in window
    trades = conn.execute(f"""
        SELECT strategy_name, regime_at_entry, net_pnl,
               entry_vix, days_to_expiry_at_entry,
               vol_score, edge_score, trend_score, flow_score,
               composite_score_at_entry, exit_reason,
               total_credit_received, realized_pnl_percent
        FROM closed_trades
        WHERE entry_timestamp >= '{cutoff}'
        AND exit_timestamp IS NOT NULL
        ORDER BY entry_timestamp
    """).fetchall()

    trade_cols = [
        "strategy_name", "regime_at_entry", "net_pnl",
        "entry_vix", "days_to_expiry_at_entry",
        "vol_score", "edge_score", "trend_score", "flow_score",
        "composite_score_at_entry", "exit_reason",
        "total_credit_received", "realized_pnl_percent"
    ]
    trades_df_data = [dict(zip(trade_cols, t)) for t in trades]

    # Strategy attribution
    attribution = {}
    for t in trades_df_data:
        key = f"{t['strategy_name']}|{t['regime_at_entry']}"
        if key not in attribution:
            attribution[key] = {"count": 0, "pnl": [], "wins": 0}
        attribution[key]["count"] += 1
        attribution[key]["pnl"].append(t["net_pnl"])
        if t["net_pnl"] > 0:
            attribution[key]["wins"] += 1

    attribution_summary = {}
    for key, data in attribution.items():
        pnls = data["pnl"]
        attribution_summary[key] = {
            "count": data["count"],
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "win_rate": round(data["wins"] / data["count"] * 100, 1),
            "total_pnl": round(sum(pnls), 2),
        }

    # Exit reason distribution
    exit_reasons = {}
    for t in trades_df_data:
        r = t["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Module score vs outcome correlation (simple)
    module_analysis = {}
    for module in ["vol_score", "edge_score", "trend_score", "flow_score", "composite_score_at_entry"]:
        pairs = [(t[module], t["net_pnl"]) for t in trades_df_data
                 if t.get(module) is not None and t.get("net_pnl") is not None]
        if len(pairs) >= 10:
            scores = [p[0] for p in pairs]
            pnls = [p[1] for p in pairs]
            # Simple correlation
            n = len(pairs)
            mean_s = sum(scores) / n
            mean_p = sum(pnls) / n
            cov = sum((s - mean_s) * (p - mean_p) for s, p in pairs) / n
            std_s = (sum((s - mean_s) ** 2 for s in scores) / n) ** 0.5
            std_p = (sum((p - mean_p) ** 2 for p in pnls) / n) ** 0.5
            corr = cov / (std_s * std_p) if std_s > 0 and std_p > 0 else 0
            module_analysis[module] = {
                "correlation_with_pnl": round(corr, 3),
                "sample_size": n,
                "avg_when_positive": round(
                    sum(p for s, p in pairs if s > 0) /
                    max(1, sum(1 for s, _ in pairs if s > 0)), 2
                ),
                "avg_when_negative": round(
                    sum(p for s, p in pairs if s < 0) /
                    max(1, sum(1 for s, _ in pairs if s < 0)), 2
                ),
            }

    conn.close()

    total_pnl = sum(t["net_pnl"] for t in trades_df_data)
    win_rate = (
        sum(1 for t in trades_df_data if t["net_pnl"] > 0) /
        len(trades_df_data) * 100
        if trades_df_data else 0
    )

    prompt = f"""You are performing a cumulative performance analysis of a NIFTY options algo trading engine.
Analysis period: last {lookback_days} days | Total trades: {len(trades_df_data)}

## CUMULATIVE PERFORMANCE
- Total Net P&L: ₹{total_pnl:,.0f}
- Win Rate: {win_rate:.1f}%
- Avg P&L per Trade: ₹{total_pnl/max(1,len(trades_df_data)):,.0f}

## STRATEGY × REGIME ATTRIBUTION
{json.dumps(attribution_summary, indent=2)}

## EXIT REASON DISTRIBUTION
{json.dumps(exit_reasons, indent=2)}

## MODULE SCORE PREDICTIVE POWER
(Correlation between module score and trade P&L)
{json.dumps(module_analysis, indent=2)}

## CURRENT PARAMETERS
{json.dumps(get_current_parameters(), indent=2)}

## YOUR TASK FOR CUMULATIVE ANALYSIS

1. WHICH STRATEGIES ARE WORKING: Based on the attribution table, which strategy × regime combinations have positive expectancy? Which are negative? Should any be disabled?

2. MODULE EFFECTIVENESS: Based on the correlation analysis, which modules are actually predictive of P&L? If flow_score correlation is near zero, should WEIGHT_FLOW be reduced to 0?

3. EXIT REASON DIAGNOSIS:
   - If STOP_LOSS exits dominate: stops are too tight or strategy selection is wrong
   - If TIME_EXIT dominates: positions are being held too long without hitting targets
   - If PROFIT_TARGET dominates: this is healthy, but are targets too conservative?

4. PARAMETER CHANGES RANKED BY IMPACT: List the top 3 parameter changes that would most improve performance, with specific numeric values and the data that supports each change.

5. WHAT IS STILL UNKNOWN: What questions cannot be answered with this data? What additional data would you need?

Format parameter suggestions as:
CHANGE #N: PARAMETER_NAME: current -> suggested
DATA SUPPORT: [specific numbers from the analysis above]
EXPECTED IMPACT: [what should improve and by how much]
RISK: [what could go wrong with this change]
CONFIDENCE: HIGH/MEDIUM/LOW
"""

    return prompt


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily decision journal for LLM tuning")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="Trading date (YYYY-MM-DD)")
    parser.add_argument("--export-prompt", action="store_true",
                        help="Write LLM prompt to file")
    parser.add_argument("--cumulative", action="store_true",
                        help="Generate cumulative analysis prompt")
    parser.add_argument("--lookback", type=int, default=30,
                        help="Lookback days for cumulative analysis")
    parser.add_argument("--output-dir", default="journals",
                        help="Directory for output files")
    parser.add_argument("--init", action="store_true",
                        help="Create all required tables and exit")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    db_path = config.STATE_DB

    # IMP-02: --init creates all tables and exits
    if args.init:
        init_all_tables(db_path)
        print(f"All tables initialised in {db_path}")
        print("You can now run the engine and decision_journal.py will work.")
        return

    # Ensure logging tables exist
    ensure_logging_tables(db_path)

    if args.cumulative:
        logger.info(f"Generating cumulative analysis for last {args.lookback} days")
        prompt = generate_cumulative_prompt(db_path, args.lookback)
        prompt_file = os.path.join(
            args.output_dir,
            f"cumulative_prompt_{date.today().isoformat()}.txt"
        )
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        logger.info(f"Cumulative prompt written to: {prompt_file}")
        print(f"\nCumulative prompt saved to: {prompt_file}")
        print("Paste this into your LLM for weekly parameter review.")
        return

    # Daily journal
    trading_date = args.date
    logger.info(f"Assembling daily journal for {trading_date}")

    journal = assemble_daily_journal(trading_date, db_path)

    # Save JSON journal
    journal_file = os.path.join(
        args.output_dir,
        f"journal_{trading_date}.json"
    )
    with open(journal_file, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, default=str)
    logger.info(f"Journal saved: {journal_file}")

    # Generate and save LLM prompt
    if args.export_prompt or True:  # always export
        prompt = generate_llm_prompt(journal)
        prompt_file = os.path.join(
            args.output_dir,
            f"llm_prompt_{trading_date}.txt"
        )
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        logger.info(f"LLM prompt saved: {prompt_file}")

    # Print summary
    stats = journal.get("daily_statistics", {})
    print(f"\n{'='*60}")
    print(f"Daily Journal: {trading_date}")
    print(f"{'='*60}")
    print(f"Trades opened:  {stats.get('trades_opened', 0)}")
    print(f"Trades closed:  {stats.get('trades_closed', 0)}")
    print(f"Net P&L:        ₹{stats.get('total_net_pnl', 0):,.0f}")
    print(f"Win rate:       {stats.get('win_rate', 'N/A')}%")
    print(f"Build attempts: {stats.get('build_attempts', 0)}")
    print(f"Build results:  {stats.get('build_results', {})}")
    if stats.get("credit_gap"):
        print(f"Credit gap:     {stats['credit_gap']:.1f} pts (floor blocking trades)")
    print(f"\nFiles written:")
    print(f"  Journal: {journal_file}")
    print(f"  Prompt:  {prompt_file}")
    print(f"\nPaste the prompt file into Claude/GPT-4 for parameter suggestions.")


if __name__ == "__main__":
    main()