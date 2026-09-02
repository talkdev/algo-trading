#!/usr/bin/env python3
"""
decision_journal.py

Offline NIFTY-options decision journal and AI-analysis generator.

ENHANCED VERSION — additional sections:
  - Regime cycle timeline with per-cycle raw scores
  - Entry attempt detail with leg-level fill data
  - Intraday P&L timeline
  - Data quality timeline (spot/VIX/IV over time)
  - Cost breakdown per trade
  - Composite score evolution (text chart)
  - Gate block frequency analysis
  - WS health timeline
  - Per-leg slippage analysis
  - Strategy builder failure taxonomy

This script ONLY:
    - reads SQLite data and log files;
    - calculates descriptive statistics;
    - calculates optional offline ML diagnostics;
    - writes JSON and an LLM prompt.

This script NEVER:
    - modifies config.py;
    - modifies trading parameters;
    - places, modifies or cancels orders;
    - changes positions;
    - changes capital state;
    - automatically applies AI recommendations.

Optional ML dependency:
    python -m pip install scikit-learn

Examples:
    python decision_journal.py
    python decision_journal.py --date 2026-09-02
    python decision_journal.py --lookback 90
    python decision_journal.py --include-raw
    python decision_journal.py --no-ml
    python decision_journal.py --init
    python decision_journal.py --log data/audit_log_2026-09-02.log
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pytz


# ---------------------------------------------------------------------
# Import configuration
# ---------------------------------------------------------------------

sys.path.insert(
    0,
    os.path.dirname(os.path.abspath(__file__)),
)

import config


IST = pytz.timezone(
    getattr(config, "TZ", "Asia/Kolkata")
)

LOGGER = logging.getLogger("decision_journal")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)


# ---------------------------------------------------------------------
# Optional ML imports
# ---------------------------------------------------------------------

try:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        accuracy_score,
        brier_score_loss,
        log_loss,
        roc_auc_score,
    )
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

JOURNAL_VERSION = "5.0"  # bumped from 4.0

ML_MIN_SAMPLES     = 30
ML_MIN_OOS_SAMPLES = 10
ML_MIN_CLASS_COUNT = 5
ML_MAX_SPLITS      = 5

BASE_FEATURES = (
    "entry_vix",
    "entry_spot",
    "days_to_expiry_at_entry",
    "vol_score",
    "edge_score",
    "trend_score",
    "flow_score",
    "composite_score_at_entry",
    "max_risk",
    "total_credit_received",
)

FAILURE_FIELDS = (
    "build_result",
    "build_failure_reason",
    "pretrade_fail_reason",
    "execution_result",
)

# Log line pattern for audit log parsing
_LOG_PATTERN = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'
    r' \| (\w+)\s*'
    r'\| ([\w_]+)\s*'
    r'\| (.+)$'
)


# ---------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------

def ist_now() -> datetime:
    return datetime.now(IST)


def ist_today() -> date:
    return ist_now().date()


def ist_iso_now() -> str:
    return ist_now().isoformat()


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
        if not math.isfinite(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def json_default(value: Any) -> str:
    return str(value)


def mean_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    if not values:
        return None
    return round(statistics.mean(values), 6)


def median_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    if not values:
        return None
    return round(statistics.median(values), 6)


def std_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    if len(values) < 2:
        return None
    try:
        return round(statistics.stdev(values), 6)
    except statistics.StatisticsError:
        return None


def min_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    return min(values) if values else None


def max_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    return max(values) if values else None


def percentile(
    values: Iterable[float],
    fraction: float,
) -> Optional[float]:
    values = sorted(values)
    if not values:
        return None
    fraction = max(0.0, min(1.0, fraction))
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(values[lower], 6)
    result = (
        values[lower]
        + (values[upper] - values[lower]) * (position - lower)
    )
    return round(result, 6)


def numeric_values(
    rows: Iterable[Dict[str, Any]],
    field: str,
) -> List[float]:
    result = []
    for row in rows:
        value = safe_float(row.get(field))
        if value is not None:
            result.append(value)
    return result


def parse_ist_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return IST.localize(parsed)
        return parsed.astimezone(IST)
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp_date_expression(column: str) -> str:
    return f"substr({column}, 1, 10)"


# ---------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------

def table_exists(
    conn: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(
    conn: sqlite3.Connection,
    table_name: str,
) -> set:
    if not table_exists(conn, table_name):
        return set()
    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()
    return {row[1] for row in rows}


def select_existing(
    conn: sqlite3.Connection,
    table_name: str,
    requested: Iterable[str],
    where_sql: str = "",
    parameters: tuple = (),
    order_sql: str = "",
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    existing = table_columns(conn, table_name)
    if not existing:
        return []
    selected = [c for c in requested if c in existing]
    if not selected:
        return []
    sql = f"SELECT {', '.join(selected)} FROM {table_name}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    if order_sql:
        sql += f" ORDER BY {order_sql}"
    if limit:
        sql += f" LIMIT {limit}"
    try:
        cursor = conn.execute(sql, parameters)
        names  = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        LOGGER.warning("SQLite query failed for %s: %s", table_name, exc)
        return []


def ensure_journal_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS journal_generation_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at  TEXT NOT NULL,
                trading_date  TEXT NOT NULL,
                journal_file  TEXT,
                prompt_file   TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Configuration snapshot
# ---------------------------------------------------------------------

def get_current_parameters() -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for name in sorted(dir(config)):
        if name.startswith("_"):
            continue
        try:
            value = getattr(config, name)
        except Exception:
            continue
        if isinstance(value, (str, int, float, bool)):
            result[name] = value
    return result


# ---------------------------------------------------------------------
# Log file parser
# ---------------------------------------------------------------------

def parse_audit_log(log_path: str) -> List[Dict[str, Any]]:
    """
    Parse an audit log file into structured entries.
    Returns list of {ts, ts_str, level, module, message}.
    """
    entries = []
    if not log_path or not os.path.isfile(log_path):
        return entries
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.rstrip("\n")
                m = _LOG_PATTERN.match(line)
                if m:
                    try:
                        ts = datetime.strptime(
                            m.group(1).strip(),
                            "%Y-%m-%d %H:%M:%S",
                        )
                        ts = IST.localize(ts)
                    except ValueError:
                        ts = None
                    entries.append({
                        "ts":      ts,
                        "ts_str":  m.group(1).strip(),
                        "level":   m.group(2).strip(),
                        "module":  m.group(3).strip(),
                        "message": m.group(4).strip(),
                    })
                elif entries:
                    entries[-1]["message"] += " " + line.strip()
    except Exception as exc:
        LOGGER.warning("Log parse error %s: %s", log_path, exc)
    return entries


def find_log_for_date(trading_date: str, data_dir: str) -> Optional[str]:
    """Find the audit log file for a given trading date."""
    candidates = [
        os.path.join(data_dir, f"audit_log_{trading_date}.log"),
        os.path.join(data_dir, f"audit_{trading_date}.log"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Try latest log if date-specific not found
    log_dir = Path(data_dir)
    logs = sorted(log_dir.glob("audit_log_*.log"), reverse=True)
    if logs:
        return str(logs[0])
    return None


# ---------------------------------------------------------------------
# NEW: Regime cycle timeline from log
# ---------------------------------------------------------------------

def get_regime_cycle_timeline(
    log_entries: List[Dict[str, Any]],
    trading_date: str,
) -> List[Dict[str, Any]]:
    """
    Extract per-cycle regime scores from audit log.
    Provides the full intraday timeline of composite scores
    and module scores that SQLite aggregates hide.
    """
    cycles  = []
    current: Dict[str, Any] = {}

    for e in log_entries:
        if e["module"] != "regime_engine":
            continue
        msg = e["message"]
        ts  = e["ts_str"]

        if "Regime refresh started" in msg:
            current = {"ts": ts}

        elif msg.startswith("Vol:"):
            m = re.search(r"score=([-\d.]+)", msg)
            if m:
                current["vol_raw"] = float(m.group(1))
            m2 = re.search(r"T_spread[^\s]*\s+([-+\d.]+)%%", msg)
            if m2:
                current["term_spread_pct"] = float(m2.group(1))
            m3 = re.search(r"warming\s+(\d+)/(\d+)", msg)
            if m3:
                current["skew_warmup_days"]     = int(m3.group(1))
                current["skew_warmup_required"] = int(m3.group(2))

        elif msg.startswith("Edge:"):
            m = re.search(r"score=([-\d.]+)", msg)
            if m:
                current["edge_raw"] = float(m.group(1))
            m2 = re.search(r"IV_atm\s+([\d.]+)%", msg)
            if m2:
                current["iv_atm_pct"] = float(m2.group(1))
            m3 = re.search(r"RV\d+\s+([\d.]+)%", msg)
            if m3:
                current["rv_pct"] = float(m3.group(1))
            m4 = re.search(r"=\s*\+([\d.]+)\s*->", msg)
            if m4:
                current["vrp_pp"] = float(m4.group(1))
            m5 = re.search(r"z=([-\d.]+)", msg)
            if m5:
                current["edge_zscore"] = float(m5.group(1))
            if "RICH" in msg:
                current["edge_tag"] = "RICH"
            elif "CHEAP" in msg:
                current["edge_tag"] = "CHEAP"
            elif "FAIR" in msg:
                current["edge_tag"] = "FAIR"
            elif "ESTIMATED" in msg:
                current["edge_tag"] = "ESTIMATED_RV"

        elif msg.startswith("Trend:"):
            m = re.search(r"score=([-\d.]+)", msg)
            if m:
                current["trend_raw"] = float(m.group(1))
            m2 = re.search(r"ADX\s+([\d.]+)", msg)
            if m2:
                current["adx"] = float(m2.group(1))
            m3 = re.search(r"slope\s+([-\d.]+)%", msg)
            if m3:
                current["ema_slope_pct"] = float(m3.group(1))
            if "bearish" in msg:
                current["trend_direction"] = "BEARISH"
            elif "range-bound" in msg:
                current["trend_direction"] = "RANGE_BOUND"
            elif "trending" in msg:
                current["trend_direction"] = "TRENDING"

        elif msg.startswith("Flow:"):
            m = re.search(r"score=([-\d.]+)", msg)
            if m:
                current["flow_raw"] = float(m.group(1))
            m2 = re.search(r"Net_dOI\(15m\)\s+([-\d,]+)", msg)
            if m2:
                current["net_oi_delta"] = safe_float(
                    m2.group(1).replace(",", "")
                )
            if "WIDENING" in msg:
                current["spread_state"] = "WIDENING"
            elif "CONTRACTING" in msg:
                current["spread_state"] = "CONTRACTING"
            elif "FLAT" in msg:
                current["spread_state"] = "FLAT"
            if "fear premium" in msg:
                current["flow_signal"] = "FEAR_PREMIUM"
            elif "complacency" in msg:
                current["flow_signal"] = "COMPLACENCY"
            elif "mixed" in msg:
                current["flow_signal"] = "MIXED"

        elif msg.startswith("Regime:") and "composite=" in msg:
            m = re.search(
                r"Regime:\s+(\S+)\s+composite=([-\d.]+)\s+persist=(\d+)",
                msg,
            )
            if m:
                current["regime"]    = m.group(1)
                current["composite"] = float(m.group(2))
                current["persist"]   = int(m.group(3))
                cycles.append(dict(current))
                current = {}

        # Persistence confirmations
        elif "Persistence confirmed" in msg or "Persistence unconfirmed" in msg:
            confirmed = "confirmed" in msg
            for mod in ["vol", "edge", "trend", "flow"]:
                if f"{mod}=" in msg:
                    m = re.search(rf"{mod}=([-\d.]+)", msg)
                    if m:
                        current[f"{mod}_confirmed"] = float(m.group(1))
                    current[f"{mod}_persist_ok"] = confirmed

    return cycles


# ---------------------------------------------------------------------
# NEW: Intraday market data timeline from log
# ---------------------------------------------------------------------

def get_market_data_timeline(
    log_entries: List[Dict[str, Any]],
    trading_date: str,
) -> List[Dict[str, Any]]:
    """
    Extract intraday spot/VIX/IV readings from audit log.
    Provides the full price timeline for the session.
    """
    timeline = []
    for e in log_entries:
        if e["module"] != "data_manager":
            continue
        msg = e["message"]
        if msg.startswith("spot=") and "vix=" in msg:
            m = re.search(r"spot=([\d.]+)\s+vix=([\d.]+)", msg)
            if m:
                timeline.append({
                    "ts":   e["ts_str"],
                    "spot": float(m.group(1)),
                    "vix":  float(m.group(2)),
                })
        if "IV_ATM:" in msg:
            m = re.search(r"IV_ATM:.*?\(([\d.]+)%\)", msg)
            if m and timeline:
                timeline[-1]["iv_atm_pct"] = float(m.group(1))
        if "IV=" in msg and "RV=" in msg and "spread=" in msg:
            m = re.search(
                r"IV=([\d.]+)\s+RV=([\d.]+)\s+spread=([\d.]+)", msg
            )
            if m and timeline:
                timeline[-1]["iv_decimal"]     = float(m.group(1))
                timeline[-1]["rv_decimal"]     = float(m.group(2))
                timeline[-1]["iv_rv_spread"]   = float(m.group(3))
        if "Net flow:" in msg:
            m = re.search(r"Net flow:\s+([-\d.]+)", msg)
            if m and timeline:
                timeline[-1]["net_flow"] = float(m.group(1))
        if "Spread ratio:" in msg:
            m = re.search(r"Spread ratio:\s+([\d.]+)", msg)
            if m and timeline:
                timeline[-1]["spread_ratio"] = float(m.group(1))
    return timeline


# ---------------------------------------------------------------------
# NEW: WS health timeline from log
# ---------------------------------------------------------------------

def get_ws_health_timeline(
    log_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Extract WebSocket health events from audit log.
    """
    events      = []
    error_count = 0
    fail_count  = 0
    silence_count = 0

    for e in log_entries:
        if e["module"] != "data_manager":
            continue
        msg = e["message"]
        ts  = e["ts_str"]

        if "WebSocket connected" in msg:
            events.append({"ts": ts, "event": "CONNECTED"})
        elif "WS error attempt" in msg:
            error_count += 1
            m = re.search(r"WS error attempt (\d+): (.+)", msg)
            events.append({
                "ts":      ts,
                "event":   "ERROR",
                "attempt": int(m.group(1)) if m else None,
                "reason":  m.group(2)[:80] if m else msg[:80],
            })
        elif "All WS reconnect attempts failed" in msg:
            fail_count += 1
            events.append({"ts": ts, "event": "ALL_FAILED"})
        elif "WS silent" in msg:
            silence_count += 1
            m = re.search(r"WS silent (\d+)s", msg)
            events.append({
                "ts":      ts,
                "event":   "SILENT",
                "seconds": int(m.group(1)) if m else None,
            })
        elif "WS reconnected" in msg:
            events.append({"ts": ts, "event": "RECONNECTED"})
        elif "reconnect suppressed" in msg:
            events.append({"ts": ts, "event": "SUPPRESSED_OUTSIDE_HOURS"})
        elif "backing off reconnect" in msg:
            events.append({"ts": ts, "event": "BACKOFF"})

    # Error reason frequency
    reasons = Counter(
        e.get("reason", "")[:60]
        for e in events
        if e["event"] == "ERROR"
    )

    connected_periods = [e for e in events if e["event"] == "CONNECTED"]
    operating_mode = "WS_AND_REST" if connected_periods else "REST_ONLY"

    return {
        "total_events":         len(events),
        "connect_count":        len(connected_periods),
        "error_count":          error_count,
        "all_failed_count":     fail_count,
        "silence_count":        silence_count,
        "operating_mode":       operating_mode,
        "error_reasons":        dict(reasons.most_common(5)),
        "timeline":             events[:50],  # cap for JSON size
        "assessment": (
            "DEGRADED — WS failed repeatedly, REST-only mode"
            if fail_count > 2 else
            "OK — minor WS interruptions, REST fallback active"
            if error_count > 0 else
            "HEALTHY"
        ),
    }


# ---------------------------------------------------------------------
# NEW: Gate block analysis from log
# ---------------------------------------------------------------------

def get_gate_block_analysis(
    log_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Detailed analysis of entry gate blocks.
    Counts every block reason and tracks time distribution.
    """
    blocks     = []
    passes     = []
    block_by_hour: Dict[int, int] = defaultdict(int)

    for e in log_entries:
        if e["module"] != "strategy_engine":
            continue
        msg = e["message"]
        ts  = e["ts"]

        if "Entry gate BLOCKED:" in msg:
            reason = msg.split("Entry gate BLOCKED:")[-1].strip()
            key    = reason.split("(")[0].split(":")[0].strip()
            blocks.append({
                "ts":     e["ts_str"],
                "reason": key,
                "full":   reason[:120],
            })
            if ts:
                block_by_hour[ts.hour] += 1

        elif "Entry gate PASSED:" in msg:
            passes.append({
                "ts":        e["ts_str"],
                "composite": None,
                "regime":    None,
            })
            m = re.search(r"composite=([-\d.]+)", msg)
            if m and passes:
                passes[-1]["composite"] = float(m.group(1))
            m2 = re.search(r"regime=(\S+)", msg)
            if m2 and passes:
                passes[-1]["regime"] = m2.group(1)

    block_reasons = Counter(b["reason"] for b in blocks)

    # Time distribution of blocks
    hour_dist = {
        f"{h:02d}:00-{h:02d}:59": count
        for h, count in sorted(block_by_hour.items())
    }

    # Identify the primary blocker
    primary = block_reasons.most_common(1)
    primary_reason = primary[0][0] if primary else "N/A"
    primary_count  = primary[0][1] if primary else 0

    return {
        "total_blocks":      len(blocks),
        "total_passes":      len(passes),
        "pass_rate_pct":     round(
            len(passes) / max(1, len(blocks) + len(passes)) * 100, 2
        ),
        "block_reasons":     dict(block_reasons.most_common()),
        "primary_blocker":   primary_reason,
        "primary_count":     primary_count,
        "hour_distribution": hour_dist,
        "passes":            passes,
        "vega_gate_blocks":  block_reasons.get(
            "Pre-trade: vega above max", 0
        ),
        "after_exec_end_blocks": block_reasons.get(
            "after EXEC_END", 0
        ),
    }


# ---------------------------------------------------------------------
# NEW: Leg-level fill and slippage analysis from log
# ---------------------------------------------------------------------

def get_leg_fill_analysis(
    log_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Extract per-leg fill prices and slippage from audit log.
    """
    fills = []

    for e in log_entries:
        if e["module"] != "strategy_engine":
            continue
        msg = e["message"]
        if "Paper fill:" not in msg and "fill:" not in msg.lower():
            continue

        m = re.search(
            r"(?:Paper fill|fill):\s+(\w+)\s+(\w+)\s+"
            r"strike=([\d.]+)\s+expiry=([\d-]+)\s+"
            r"ref=([\d.]+)\s+fill=([\d.]+)",
            msg,
        )
        if m:
            ref  = float(m.group(5))
            fill = float(m.group(6))
            fills.append({
                "ts":        e["ts_str"],
                "action":    m.group(1),
                "opt_type":  m.group(2),
                "strike":    float(m.group(3)),
                "expiry":    m.group(4),
                "ref_price": ref,
                "fill_price":fill,
                "slippage":  round(abs(fill - ref), 2),
                "slippage_pct": round(
                    abs(fill - ref) / ref * 100, 3
                ) if ref > 0 else None,
            })

    if not fills:
        return {
            "total_fills":      0,
            "note":             "No fills recorded (no trades executed today)",
        }

    slippages = [f["slippage"] for f in fills]
    sell_fills = [f for f in fills if f["action"] == "SELL"]
    buy_fills  = [f for f in fills if f["action"] == "BUY"]

    return {
        "total_fills":          len(fills),
        "sell_fills":           len(sell_fills),
        "buy_fills":            len(buy_fills),
        "total_slippage_pts":   round(sum(slippages), 2),
        "avg_slippage_pts":     mean_or_none(slippages),
        "max_slippage_pts":     max_or_none(slippages),
        "slippage_by_action": {
            "SELL": mean_or_none([f["slippage"] for f in sell_fills]),
            "BUY":  mean_or_none([f["slippage"] for f in buy_fills]),
        },
        "fill_details":         fills,
    }


# ---------------------------------------------------------------------
# NEW: Intraday P&L timeline from log
# ---------------------------------------------------------------------

def get_pnl_timeline(
    log_entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Extract intraday P&L snapshots from portfolio summary logs.
    """
    snapshots = []
    i = 0
    while i < len(log_entries):
        e = log_entries[i]
        if "PORTFOLIO SUMMARY" in e["message"]:
            snap: Dict[str, Any] = {"ts": e["ts_str"]}
            # Look ahead for the summary fields
            for j in range(i, min(i + 20, len(log_entries))):
                m2 = log_entries[j]["message"]
                patterns = [
                    ("open_positions", r"Open Positions\s*:\s*(\d+)"),
                    ("daily_pnl",      r"Daily P&L.*?:\s*₹([-\d,]+\.?\d*)"),
                    ("weekly_pnl",     r"Weekly P&L.*?:\s*₹([-\d,]+\.?\d*)"),
                    ("capital",        r"Capital\s*:\s*₹([\d,]+\.?\d*)"),
                    ("peak_capital",   r"Peak\s*:\s*₹([\d,]+\.?\d*)"),
                    ("delta",          r"Delta\s*=\s*([-\d.]+)"),
                    ("vega",           r"Vega=Rs([-\d,]+)"),
                    ("theta",          r"Theta=Rs([-\d,]+)"),
                ]
                for key, pat in patterns:
                    mm = re.search(pat, m2)
                    if mm and key not in snap:
                        raw = mm.group(1).replace(",", "")
                        snap[key] = safe_float(raw)
            if "daily_pnl" in snap:
                snapshots.append(snap)
        i += 1
    return snapshots


# ---------------------------------------------------------------------
# NEW: Builder failure taxonomy from log
# ---------------------------------------------------------------------

def get_builder_failure_taxonomy(
    log_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Classify every strategy builder failure by root cause.
    """
    failures: List[Dict[str, Any]] = []

    for e in log_entries:
        if e["module"] != "strategy_engine":
            continue
        msg = e["message"]
        ts  = e["ts_str"]

        if "Pre-trade failed:" in msg or "Build FAILED:" in msg:
            rec: Dict[str, Any] = {"ts": ts, "message": msg[:200]}

            # Classify
            if "vega" in msg.lower():
                rec["category"] = "VEGA_GATE"
            elif "credit" in msg.lower():
                rec["category"] = "CREDIT_TOO_LOW"
            elif "LTP=0" in msg or "ltp" in msg.lower():
                rec["category"] = "LTP_ZERO"
            elif "spread" in msg.lower() and "wide" in msg.lower():
                rec["category"] = "SPREAD_TOO_WIDE"
            elif "margin" in msg.lower():
                rec["category"] = "MARGIN"
            elif "delta" in msg.lower():
                rec["category"] = "DELTA_LIMIT"
            elif "DTE" in msg or "expiry" in msg.lower():
                rec["category"] = "DTE_OR_EXPIRY"
            elif "risk" in msg.lower():
                rec["category"] = "RISK_LIMIT"
            else:
                rec["category"] = "OTHER"

            failures.append(rec)

    categories = Counter(f["category"] for f in failures)

    return {
        "total_failures":    len(failures),
        "by_category":       dict(categories.most_common()),
        "primary_category":  (
            categories.most_common(1)[0][0]
            if categories else "N/A"
        ),
        "vega_gate_count":   categories.get("VEGA_GATE", 0),
        "fix_applied":       categories.get("VEGA_GATE", 0) > 0,
        "fix_note": (
            "FIX-1 applied — vega gate will not block first position tomorrow"
            if categories.get("VEGA_GATE", 0) > 0
            else "No vega gate blocks today"
        ),
        "failure_details":   failures[:20],
    }


# ---------------------------------------------------------------------
# NEW: Cost breakdown analysis
# ---------------------------------------------------------------------

def get_cost_breakdown(
    conn: sqlite3.Connection,
    trading_date: str,
) -> Dict[str, Any]:
    """
    Detailed transaction cost breakdown for closed trades.
    """
    requested = [
        "trade_id",
        "strategy_name",
        "total_credit_received",
        "total_debit_paid",
        "net_premium",
        "realized_pnl",
        "transaction_costs",
        "net_pnl",
        "slippage_total_points",
        "entry_vix",
        "days_to_expiry_at_entry",
    ]

    rows = select_existing(
        conn,
        "closed_trades",
        requested,
        where_sql=(
            f"{timestamp_date_expression('exit_timestamp')} = ? "
            f"OR {timestamp_date_expression('entry_timestamp')} = ?"
        ),
        parameters=(trading_date, trading_date),
    )

    if not rows:
        return {
            "status":       "NO_CLOSED_TRADES",
            "total_costs":  0,
            "total_gross":  0,
            "total_net":    0,
            "cost_drag_pct": None,
        }

    total_costs  = sum(safe_float(r.get("transaction_costs", 0)) or 0 for r in rows)
    total_gross  = sum(safe_float(r.get("realized_pnl", 0)) or 0 for r in rows)
    total_net    = sum(safe_float(r.get("net_pnl", 0)) or 0 for r in rows)
    total_slip   = sum(safe_float(r.get("slippage_total_points", 0)) or 0 for r in rows)
    total_credit = sum(safe_float(r.get("total_credit_received", 0)) or 0 for r in rows)

    cost_drag_pct = (
        round(total_costs / abs(total_gross) * 100, 2)
        if total_gross != 0 else None
    )

    per_trade = []
    for r in rows:
        gross  = safe_float(r.get("realized_pnl", 0)) or 0
        costs  = safe_float(r.get("transaction_costs", 0)) or 0
        net    = safe_float(r.get("net_pnl", 0)) or 0
        credit = safe_float(r.get("total_credit_received", 0)) or 0
        per_trade.append({
            "trade_id":        str(r.get("trade_id", ""))[:8],
            "strategy":        r.get("strategy_name"),
            "gross_pnl":       round(gross, 2),
            "transaction_costs": round(costs, 2),
            "net_pnl":         round(net, 2),
            "slippage_pts":    safe_float(r.get("slippage_total_points", 0)),
            "credit_received": round(credit, 2),
            "cost_as_pct_of_credit": (
                round(costs / credit * 100, 2)
                if credit > 0 else None
            ),
        })

    return {
        "status":           "OK",
        "trade_count":      len(rows),
        "total_gross_pnl":  round(total_gross, 2),
        "total_costs":      round(total_costs, 2),
        "total_net_pnl":    round(total_net, 2),
        "total_slippage_pts": round(total_slip, 2),
        "cost_drag_pct":    cost_drag_pct,
        "avg_cost_per_trade": round(total_costs / len(rows), 2) if rows else 0,
        "per_trade":        per_trade,
        "note": (
            "Transaction costs include brokerage, STT, exchange fee, "
            "SEBI fee, stamp duty, IPFT, GST as per config.py COST_* constants."
        ),
    }


# ---------------------------------------------------------------------
# NEW: Composite score text chart
# ---------------------------------------------------------------------

def get_composite_score_chart(
    cycles: List[Dict[str, Any]],
) -> str:
    """
    Generate a text-based chart of composite score evolution.
    Shows how the composite moved through the day.
    """
    if not cycles:
        return "No regime cycles available."

    composites = [
        (c.get("ts", ""), c.get("composite", 0))
        for c in cycles
        if c.get("composite") is not None
    ]

    if not composites:
        return "No composite scores available."

    # Sample every Nth cycle to keep chart manageable
    n = max(1, len(composites) // 40)
    sampled = composites[::n]

    chart_lines = []
    chart_lines.append(
        f"Composite score evolution ({len(composites)} cycles, "
        f"showing every {n}th):"
    )
    chart_lines.append(
        f"  Range: [{min(v for _, v in composites):.3f}, "
        f"{max(v for _, v in composites):.3f}]"
    )
    chart_lines.append("")

    # Scale: -1.0 to +1.0 mapped to 0-40 chars
    width = 40
    zero_pos = width // 2  # position of 0.0

    for ts_str, val in sampled:
        # Map val from [-1, 1] to [0, width]
        pos = int((val + 1.0) / 2.0 * width)
        pos = max(0, min(width, pos))

        bar = [" "] * (width + 1)
        bar[zero_pos] = "|"  # zero line

        if pos >= zero_pos:
            for i in range(zero_pos, pos + 1):
                bar[i] = "+"
        else:
            for i in range(pos, zero_pos + 1):
                bar[i] = "-"

        bar[pos] = "*"
        time_part = ts_str[11:16] if len(ts_str) >= 16 else ts_str

        chart_lines.append(
            f"  {time_part} [{val:+.3f}] {''.join(bar)}"
        )

    # Thresholds legend
    chart_lines.append("")
    chart_lines.append(
        f"  Thresholds: STRONG_SELL>{config.STRONG_SELL_ENTER:.2f}  "
        f"MILD_SELL>{config.MILD_SELL_ENTER:.2f}  "
        f"NEUTRAL  "
        f"BUY_VOL<{config.MILD_BUY_ENTER:.2f}  "
        f"STRONG_BUY<{config.STRONG_BUY_ENTER:.2f}"
    )

    return "\n".join(chart_lines)


# ---------------------------------------------------------------------
# NEW: Accuracy and signal quality metrics
# ---------------------------------------------------------------------

def get_signal_accuracy_metrics(
    cycles: List[Dict[str, Any]],
    closed_trades: List[Dict[str, Any]],
    entry_attempts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compute signal quality and accuracy metrics.
    """
    if not cycles:
        return {"status": "NO_CYCLES"}

    # Regime stability
    regimes = [c.get("regime", "UNKNOWN") for c in cycles]
    regime_counts = Counter(regimes)
    total = len(regimes)
    dominant = regime_counts.most_common(1)[0] if regime_counts else ("N/A", 0)
    stability_pct = round(dominant[1] / total * 100, 1) if total else 0

    # Regime changes
    changes = 0
    for i in range(1, len(cycles)):
        if cycles[i].get("regime") != cycles[i-1].get("regime"):
            changes += 1

    # Edge signal consistency
    edge_scores = [c.get("edge_raw", 0) for c in cycles if "edge_raw" in c]
    edge_consistent = (
        len(set(int(round(e)) for e in edge_scores)) == 1
        if edge_scores else False
    )

    # VRP quality
    vrp_values = [c.get("vrp_pp") for c in cycles if c.get("vrp_pp") is not None]
    vrp_mean   = mean_or_none(vrp_values)
    vrp_std    = std_or_none(vrp_values)

    # Flow signal quality
    flow_scores = [c.get("flow_raw") for c in cycles if "flow_raw" in c]
    flow_none_count = sum(1 for c in cycles if "flow_raw" not in c)
    flow_none_pct = round(flow_none_count / total * 100, 1) if total else 0

    # Trend consistency
    trend_directions = [
        c.get("trend_direction", "UNKNOWN")
        for c in cycles
        if "trend_direction" in c
    ]
    trend_counts = Counter(trend_directions)

    # Entry accuracy
    total_attempts  = len(entry_attempts)
    successful      = sum(
        1 for a in entry_attempts
        if a.get("build_result") == "SUCCESS"
    )
    pretrade_failed = sum(
        1 for a in entry_attempts
        if a.get("pretrade_passed") == 0
    )

    # Trade outcome vs regime
    regime_outcome: Dict[str, List[float]] = defaultdict(list)
    for trade in closed_trades:
        regime = trade.get("regime_at_entry", "UNKNOWN")
        pnl    = safe_float(trade.get("net_pnl"))
        if pnl is not None:
            regime_outcome[regime].append(pnl)

    regime_win_rates = {}
    for regime, pnls in regime_outcome.items():
        wins = sum(1 for p in pnls if p > 0)
        regime_win_rates[regime] = {
            "trades":   len(pnls),
            "wins":     wins,
            "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
            "avg_pnl":  mean_or_none(pnls),
        }

    return {
        "regime_stability": {
            "dominant_regime":  dominant[0],
            "dominant_pct":     f"{stability_pct}%",
            "regime_changes":   changes,
            "change_rate_per_hour": round(changes / max(1, total / 60), 2),
            "assessment": (
                "STABLE"   if stability_pct > 80 else
                "MODERATE" if stability_pct > 60 else
                "UNSTABLE"
            ),
        },
        "edge_signal": {
            "consistent":    edge_consistent,
            "mean_score":    mean_or_none(edge_scores),
            "vrp_mean_pp":   vrp_mean,
            "vrp_std_pp":    vrp_std,
            "vrp_cv":        (
                round(vrp_std / vrp_mean, 3)
                if vrp_mean and vrp_std and vrp_mean != 0
                else None
            ),
            "assessment": (
                "STRONG_CONSISTENT" if edge_consistent and vrp_mean and vrp_mean > 3
                else "MODERATE" if vrp_mean and vrp_mean > 1
                else "WEAK"
            ),
        },
        "flow_signal": {
            "none_pct":        f"{flow_none_pct}%",
            "mean_score":      mean_or_none(flow_scores),
            "assessment": (
                "UNRELIABLE — too many None readings"
                if flow_none_pct > 50
                else "ACTIVE"
            ),
        },
        "trend_signal": {
            "direction_counts": dict(trend_counts),
            "dominant_direction": (
                trend_counts.most_common(1)[0][0]
                if trend_counts else "N/A"
            ),
        },
        "entry_accuracy": {
            "total_attempts":    total_attempts,
            "successful":        successful,
            "pretrade_failed":   pretrade_failed,
            "success_rate":      (
                f"{round(successful/total_attempts*100,1)}%"
                if total_attempts else "N/A"
            ),
        },
        "regime_outcome_association": regime_win_rates,
    }


# ---------------------------------------------------------------------
# Existing data loaders (kept from v4.0, enhanced)
# ---------------------------------------------------------------------

def get_regime_cycles(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    requested = [
        "timestamp", "spot", "vix", "iv_atm", "rv_20d",
        "skew", "forward_iv", "adx", "ema_slope_pct",
        "raw_vol", "raw_edge", "raw_trend", "raw_flow",
        "conf_vol", "conf_edge", "conf_trend", "conf_flow",
        "weight_vol", "weight_edge", "weight_trend", "weight_flow",
        "composite_score", "confirmed_regime", "regime_changed",
        "persistence_count", "entry_gate_passed",
        "entry_gate_blocked_reason", "active_expiry",
        "active_expiry_dte",
        # New columns from PATCH-RE-3
        "term_score", "skew_score", "fwd_iv_is_vix_proxy",
        "iv_atm_pct", "rv_pct", "vrp_pp", "rv_is_estimated",
        "adx_score", "slope_score", "slope_pct", "spot_above_ema",
        "net_oi_delta", "spread_ratio_val", "flow_dte",
    ]
    return select_existing(
        conn, "regime_cycle_log", requested,
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp",
    )


def get_regime_history(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    requested = [
        "timestamp", "vol_score", "edge_score", "trend_score",
        "flow_score", "composite_score", "raw_regime",
        "confirmed_regime", "persistence_count", "macro_override",
    ]
    return select_existing(
        conn, "regime_history", requested,
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp",
    )


def get_entry_attempts(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    requested = [
        "id", "timestamp", "strategy_name", "regime_at_attempt",
        "composite_score", "build_result", "build_failure_reason",
        "expiry_date", "dte", "net_credit", "min_credit_required",
        "wing_width", "short_call_strike", "short_put_strike",
        "expected_move", "pretrade_passed", "pretrade_fail_reason",
        "execution_result", "lots_requested", "lots_filled",
        "actual_credit", "vix_at_entry", "vix_adaptive_mult",
        "max_risk_used", "trade_id",
    ]
    return select_existing(
        conn, "entry_attempt_log", requested,
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp, id",
    )


def get_closed_trades(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    requested = [
        "trade_id", "strategy_name", "regime_at_entry",
        "regime_at_exit", "entry_timestamp", "exit_timestamp",
        "holding_days", "entry_spot", "exit_spot", "entry_vix",
        "exit_vix", "realized_pnl", "transaction_costs", "net_pnl",
        "realized_pnl_percent", "exit_reason", "slippage_total_points",
        "composite_score_at_entry", "vol_score", "edge_score",
        "trend_score", "flow_score", "days_to_expiry_at_entry",
        "expiry_date", "max_risk", "total_credit_received",
        "total_debit_paid", "net_premium", "legs_summary",
    ]
    columns = table_columns(conn, "closed_trades")
    if not columns:
        return []
    conditions = []
    parameters = []
    if "entry_timestamp" in columns:
        conditions.append(
            f"{timestamp_date_expression('entry_timestamp')} = ?"
        )
        parameters.append(trading_date)
    if "exit_timestamp" in columns:
        conditions.append(
            f"{timestamp_date_expression('exit_timestamp')} = ?"
        )
        parameters.append(trading_date)
    if not conditions:
        return []
    return select_existing(
        conn, "closed_trades", requested,
        where_sql=" OR ".join(conditions),
        parameters=tuple(parameters),
        order_sql="entry_timestamp",
    )


def get_trailing_closed_trades(
    conn: sqlite3.Connection,
    lookback_days: int,
) -> List[Dict[str, Any]]:
    requested = [
        "trade_id", "strategy_name", "regime_at_entry",
        "regime_at_exit", "entry_timestamp", "exit_timestamp",
        "holding_days", "entry_spot", "exit_spot", "entry_vix",
        "exit_vix", "realized_pnl", "transaction_costs", "net_pnl",
        "realized_pnl_percent", "exit_reason", "slippage_total_points",
        "composite_score_at_entry", "vol_score", "edge_score",
        "trend_score", "flow_score", "days_to_expiry_at_entry",
        "expiry_date", "max_risk", "total_credit_received",
        "total_debit_paid", "net_premium",
    ]
    cutoff = (
        ist_today() - timedelta(days=lookback_days)
    ).isoformat()
    return select_existing(
        conn, "closed_trades", requested,
        where_sql=(
            "exit_timestamp IS NOT NULL "
            "AND substr(exit_timestamp, 1, 10) >= ?"
        ),
        parameters=(cutoff,),
        order_sql="entry_timestamp",
    )


def deduplicate_trades(
    trades: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    result = []
    seen   = set()
    for trade in trades:
        trade_id = trade.get("trade_id")
        if not trade_id:
            result.append(trade)
            continue
        if trade_id in seen:
            continue
        seen.add(trade_id)
        result.append(trade)
    return result


def get_open_positions(
    conn: sqlite3.Connection,
) -> List[Dict[str, Any]]:
    requested = [
        "trade_id", "strategy_name", "regime_at_entry",
        "entry_timestamp", "entry_spot", "entry_vix",
        "expiry_date", "days_to_expiry", "total_credit",
        "total_debit", "net_premium", "max_risk",
        "stop_loss", "profit_target", "status", "legs_json",
    ]
    return select_existing(
        conn, "open_positions", requested,
        where_sql="status = 'OPEN'",
        order_sql="entry_timestamp",
    )


def get_circuit_breakers(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    requested = [
        "timestamp", "level", "trigger", "action",
        "daily_pnl", "drawdown", "regime",
    ]
    return select_existing(
        conn, "circuit_breaker_log", requested,
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp",
    )


def get_capital_state(
    conn: sqlite3.Connection,
) -> Dict[str, Any]:
    requested = [
        "current_capital", "peak_capital", "weekly_pnl",
        "daily_pnl", "cb_level_2_active", "cb_level_3_active",
        "cb_level_4_active", "kill_switch_active",
        "daily_trading_halted",
    ]
    rows = select_existing(
        conn, "engine_capital_state", requested,
        where_sql="id = 1",
    )
    return rows[0] if rows else {}


def get_order_log(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    """NEW: Read order log for the trading date."""
    requested = [
        "timestamp", "trade_id", "order_id", "instrument_key",
        "action", "option_type", "strike", "expiry", "qty",
        "order_type", "price", "fill_price", "status",
        "slippage", "paper_trade",
    ]
    return select_existing(
        conn, "order_log", requested,
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp",
    )


def get_market_state_history(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    """NEW: Read market state snapshots for the trading date."""
    requested = [
        "timestamp", "spot", "vix", "iv_atm", "rv_20d",
        "skew", "adx", "ema_50", "composite_score", "regime",
    ]
    return select_existing(
        conn, "market_state", requested,
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp",
    )


# ---------------------------------------------------------------------
# Existing analysis functions (kept from v4.0)
# ---------------------------------------------------------------------

def classify_failure(attempt: Dict[str, Any]) -> str:
    text = " ".join(
        str(attempt.get(field) or "").lower()
        for field in FAILURE_FIELDS
    )
    if not text.strip():
        return "UNKNOWN"
    if any(w in text for w in ("vega", "gamma", "greek")):
        return "GREEK_LIMIT"
    if "delta" in text:
        return "DELTA_OR_STRIKE"
    if any(w in text for w in ("credit", "premium")):
        return "CREDIT"
    if any(w in text for w in ("spread", "liquidity", "quote")):
        return "LIQUIDITY"
    if any(w in text for w in ("dte", "expiry")):
        return "DTE_OR_EXPIRY"
    if any(w in text for w in ("margin", "capital", "risk")):
        return "RISK_OR_MARGIN"
    if any(w in text for w in ("execution", "order", "fill")):
        return "EXECUTION"
    if any(w in text for w in ("ltp", "price", "stale")):
        return "PRICE_DATA"
    if "pretrade" in text:
        return "UNSPECIFIED_PRETRADE"
    return str(attempt.get("build_result") or "UNKNOWN").upper()


def entry_statistics(
    attempts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    categories = Counter()
    strategies = Counter()
    credits    = []
    dtes       = []

    for attempt in attempts:
        categories[classify_failure(attempt)] += 1
        strategies[str(attempt.get("strategy_name") or "UNKNOWN")] += 1
        credit = safe_float(attempt.get("net_credit"))
        if credit is not None:
            credits.append(credit)
        dte = safe_float(attempt.get("dte"))
        if dte is not None:
            dtes.append(dte)

    return {
        "attempt_count":       len(attempts),
        "failure_categories":  dict(categories),
        "strategy_counts":     dict(strategies),
        "failure_reason_coverage": sum(
            1 for a in attempts
            if a.get("pretrade_fail_reason") or a.get("build_failure_reason")
        ),
        "credit_points": {
            "count":  len(credits),
            "mean":   mean_or_none(credits),
            "median": median_or_none(credits),
            "minimum": min_or_none(credits),
            "maximum": max_or_none(credits),
        },
        "dte": {
            "count":        len(dtes),
            "mean":         mean_or_none(dtes),
            "median":       median_or_none(dtes),
            "distribution": dict(Counter(int(v) for v in dtes)),
        },
    }


def regime_statistics(
    cycles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    regimes    = [str(r.get("confirmed_regime")) for r in cycles if r.get("confirmed_regime")]
    counts     = Counter(regimes)
    total      = len(regimes)
    transitions = Counter()
    previous   = None
    for regime in regimes:
        if previous is not None and regime != previous:
            transitions[f"{previous}->{regime}"] += 1
        previous = regime

    module_quality = {}
    for field in ("raw_vol", "raw_edge", "raw_trend", "raw_flow"):
        observations = numeric_values(cycles, field)
        missing      = len(cycles) - len(observations)
        module_quality[field] = {
            "observations": len(observations),
            "missing":      missing,
            "missing_pct":  round(missing / max(1, len(cycles)) * 100, 2),
            "positive":     sum(1 for v in observations if v > 0),
            "negative":     sum(1 for v in observations if v < 0),
            "zero":         sum(1 for v in observations if v == 0),
            "mean":         mean_or_none(observations),
            "median":       median_or_none(observations),
        }

    composites = numeric_values(cycles, "composite_score")
    spots      = numeric_values(cycles, "spot")
    vix_values = numeric_values(cycles, "vix")
    adx_values = numeric_values(cycles, "adx")
    spot_change = round(spots[-1] - spots[0], 4) if len(spots) >= 2 else None

    # NEW: gate pass rate from cycle log
    gate_passed = sum(
        1 for r in cycles if r.get("entry_gate_passed")
    )
    gate_blocked_reasons = Counter(
        r.get("entry_gate_blocked_reason") or "UNKNOWN"
        for r in cycles
        if not r.get("entry_gate_passed")
        and r.get("entry_gate_blocked_reason")
    )

    # NEW: sub-score detail aggregates
    vrp_values  = numeric_values(cycles, "vrp_pp")
    term_scores = numeric_values(cycles, "term_score")
    skew_scores = numeric_values(cycles, "skew_score")
    adx_scores  = numeric_values(cycles, "adx_score")
    slope_scores= numeric_values(cycles, "slope_score")

    return {
        "cycle_count": len(cycles),
        "regime_distribution_pct": {
            k: round(v / max(1, total) * 100, 2)
            for k, v in counts.items()
        },
        "regime_changes":    sum(transitions.values()),
        "transitions":       dict(transitions),
        "module_quality":    module_quality,
        "composite": {
            "mean":    mean_or_none(composites),
            "median":  median_or_none(composites),
            "std":     std_or_none(composites),
            "p10":     percentile(composites, 0.10),
            "p90":     percentile(composites, 0.90),
            "minimum": min_or_none(composites),
            "maximum": max_or_none(composites),
        },
        "market": {
            "spot_first":  spots[0]  if spots else None,
            "spot_last":   spots[-1] if spots else None,
            "spot_change": spot_change,
            "vix_mean":    mean_or_none(vix_values),
            "vix_min":     min_or_none(vix_values),
            "vix_max":     max_or_none(vix_values),
            "adx_mean":    mean_or_none(adx_values),
            "adx_min":     min_or_none(adx_values),
            "adx_max":     max_or_none(adx_values),
        },
        "gate_analysis": {
            "gate_passed_count":  gate_passed,
            "gate_blocked_count": len(cycles) - gate_passed,
            "pass_rate_pct":      round(gate_passed / max(1, len(cycles)) * 100, 2),
            "blocked_reasons":    dict(gate_blocked_reasons.most_common(10)),
        },
        "sub_score_detail": {
            "vrp_pp":     {"mean": mean_or_none(vrp_values),   "range": (min_or_none(vrp_values),   max_or_none(vrp_values))},
            "term_score": {"mean": mean_or_none(term_scores),  "range": (min_or_none(term_scores),  max_or_none(term_scores))},
            "skew_score": {"mean": mean_or_none(skew_scores),  "range": (min_or_none(skew_scores),  max_or_none(skew_scores))},
            "adx_score":  {"mean": mean_or_none(adx_scores),   "range": (min_or_none(adx_scores),   max_or_none(adx_scores))},
            "slope_score":{"mean": mean_or_none(slope_scores), "range": (min_or_none(slope_scores), max_or_none(slope_scores))},
        },
    }


def trade_statistics(
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    trades  = deduplicate_trades(trades)
    closed  = [t for t in trades if t.get("exit_timestamp")]
    pnls    = [v for v in (safe_float(t.get("net_pnl")) for t in closed) if v is not None]
    winners = [v for v in pnls if v > 0]
    losers  = [v for v in pnls if v <= 0]

    gross_wins   = sum(winners)
    gross_losses = abs(sum(losers))
    profit_factor = (
        round(gross_wins / gross_losses, 4)
        if gross_losses > 0 else None
    )

    exits    = Counter(str(t.get("exit_reason") or "UNKNOWN") for t in closed)
    grouped: Dict[str, List[float]] = defaultdict(list)
    for trade in closed:
        name = str(trade.get("strategy_name") or "UNKNOWN")
        pnl  = safe_float(trade.get("net_pnl"))
        if pnl is not None:
            grouped[name].append(pnl)

    by_strategy = {}
    for name, values in grouped.items():
        wins = [v for v in values if v > 0]
        by_strategy[name] = {
            "trades":         len(values),
            "total_net_pnl":  round(sum(values), 2),
            "average_net_pnl":mean_or_none(values),
            "median_net_pnl": median_or_none(values),
            "win_rate_pct":   round(len(wins) / max(1, len(values)) * 100, 2),
        }

    # NEW: holding period analysis
    holding_days = [
        safe_float(t.get("holding_days"))
        for t in closed
        if safe_float(t.get("holding_days")) is not None
    ]

    # NEW: exit reason profitability
    exit_pnl: Dict[str, List[float]] = defaultdict(list)
    for trade in closed:
        reason = str(trade.get("exit_reason") or "UNKNOWN")
        pnl    = safe_float(trade.get("net_pnl"))
        if pnl is not None:
            exit_pnl[reason].append(pnl)
    exit_profitability = {
        reason: {
            "count":   len(pnls),
            "avg_pnl": mean_or_none(pnls),
            "win_rate": round(
                sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1
            ) if pnls else 0,
        }
        for reason, pnls in exit_pnl.items()
    }

    return {
        "trades_seen":      len(trades),
        "trades_closed":    len(closed),
        "pnl_observations": len(pnls),
        "total_net_pnl":    round(sum(pnls), 2),
        "average_net_pnl":  mean_or_none(pnls),
        "median_net_pnl":   median_or_none(pnls),
        "average_winner":   mean_or_none(winners),
        "average_loser":    mean_or_none(losers),
        "win_rate_pct": (
            round(len(winners) / len(pnls) * 100, 2) if pnls else None
        ),
        "profit_factor":    profit_factor,
        "exit_reasons":     dict(exits),
        "exit_profitability": exit_profitability,
        "by_strategy":      by_strategy,
        "holding_period": {
            "mean_days":   mean_or_none(holding_days),
            "min_days":    min_or_none(holding_days),
            "max_days":    max_or_none(holding_days),
        },
    }


# ---------------------------------------------------------------------
# NIFTY buckets
# ---------------------------------------------------------------------

def vix_bucket(value: Optional[float]) -> str:
    if value is None:
        return "UNKNOWN"
    if value < 12:
        return "VIX_BELOW_12"
    if value < 16:
        return "VIX_12_TO_16"
    if value < 22:
        return "VIX_16_TO_22"
    return "VIX_22_PLUS"


def dte_bucket(value: Optional[float]) -> str:
    if value is None:
        return "UNKNOWN"
    if value <= 1:
        return "DTE_0_TO_1"
    if value <= 3:
        return "DTE_2_TO_3"
    if value <= 6:
        return "DTE_4_TO_6"
    return "DTE_7_PLUS"


def weekday_bucket(value: Any) -> str:
    parsed = parse_ist_timestamp(value)
    if parsed is None:
        return "UNKNOWN"
    names = ("MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY")
    return names[parsed.weekday()]


def grouped_trade_statistics(
    trades: List[Dict[str, Any]],
    selector: Callable[[Dict[str, Any]], str],
) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[selector(trade)].append(trade)
    return {k: trade_statistics(v) for k, v in groups.items()}


def nifty_trade_buckets(
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "by_vix":     grouped_trade_statistics(
            trades,
            lambda t: vix_bucket(safe_float(t.get("entry_vix")))
        ),
        "by_dte":     grouped_trade_statistics(
            trades,
            lambda t: dte_bucket(safe_float(t.get("days_to_expiry_at_entry")))
        ),
        "by_weekday": grouped_trade_statistics(
            trades,
            lambda t: weekday_bucket(t.get("entry_timestamp"))
        ),
        "by_regime":  grouped_trade_statistics(
            trades,
            lambda t: str(t.get("regime_at_entry") or "UNKNOWN")
        ),
    }


# ---------------------------------------------------------------------
# Module/outcome relationships
# ---------------------------------------------------------------------

def correlation(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    num    = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x  = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y  = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 5)


def module_outcome_analysis(
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    fields = (
        "vol_score", "edge_score", "trend_score",
        "flow_score", "composite_score_at_entry",
    )
    result = {}
    for field in fields:
        pairs = [
            (safe_float(t.get(field)), safe_float(t.get("net_pnl")))
            for t in trades
            if safe_float(t.get(field)) is not None
            and safe_float(t.get("net_pnl")) is not None
        ]
        if len(pairs) < 20:
            result[field] = {
                "status":      "INSUFFICIENT_SAMPLE",
                "sample_size": len(pairs),
            }
            continue
        scores = [p[0] for p in pairs]
        pnls   = [p[1] for p in pairs]
        result[field] = {
            "status":      "DESCRIPTIVE_ONLY",
            "sample_size": len(pairs),
            "correlation_with_net_pnl": correlation(scores, pnls),
            "average_pnl_when_score_positive": mean_or_none(
                [pnl for s, pnl in pairs if s > 0]
            ),
            "average_pnl_when_score_negative": mean_or_none(
                [pnl for s, pnl in pairs if s < 0]
            ),
        }
    return result


# ---------------------------------------------------------------------
# ML (unchanged from v4.0)
# ---------------------------------------------------------------------

def build_ml_dataset(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    usable = []
    strategy_names = sorted({
        str(t.get("strategy_name") or "UNKNOWN") for t in trades
    })
    for trade in trades:
        pnl = safe_float(trade.get("net_pnl"))
        if pnl is None:
            continue
        row: Dict[str, float] = {}
        available = 0
        mapping = {
            "entry_vix":                  "entry_vix",
            "entry_spot":                 "entry_spot",
            "days_to_expiry_at_entry":    "days_to_expiry_at_entry",
            "vol_score":                  "vol_score",
            "edge_score":                 "edge_score",
            "trend_score":                "trend_score",
            "flow_score":                 "flow_score",
            "composite_score_at_entry":   "composite_score_at_entry",
            "max_risk":                   "max_risk",
            "total_credit_received":      "total_credit_received",
        }
        for fname, sname in mapping.items():
            v = safe_float(trade.get(sname))
            row[fname] = v if v is not None else float("nan")
            if v is not None:
                available += 1
        if available == 0:
            continue
        strategy = str(trade.get("strategy_name") or "UNKNOWN")
        for name in strategy_names:
            row[f"strategy__{name}"] = 1.0 if strategy == name else 0.0
        usable.append((row, 1 if pnl > 0 else 0))

    if not usable:
        return {"status": "NO_USABLE_TRADES", "features": [], "labels": [], "feature_names": []}

    feature_names = sorted(usable[0][0].keys())
    matrix = [[row.get(n, float("nan")) for n in feature_names] for row, _ in usable]
    labels = [label for _, label in usable]
    return {"status": "READY", "features": matrix, "labels": labels, "feature_names": feature_names}


def make_model(model_name: str):
    if model_name == "logistic_regression":
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler",  StandardScaler()),
            ("model",   LogisticRegression(max_iter=2000, class_weight="balanced", random_state=42)),
        ])
    if model_name == "gradient_boosting":
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model",   GradientBoostingClassifier(n_estimators=80, learning_rate=0.05, max_depth=2, random_state=42)),
        ])
    raise ValueError(f"Unsupported model: {model_name}")


def model_parameters(model, feature_names, model_name):
    try:
        estimator = model.named_steps["model"]
        values = (
            estimator.coef_[0]
            if model_name == "logistic_regression"
            else estimator.feature_importances_
        )
        return {n: round(float(v), 6) for n, v in zip(feature_names, values)}
    except Exception:
        return {}


def calibration_bins(labels, probabilities, bin_count=5):
    bins = [[] for _ in range(bin_count)]
    for label, prob in zip(labels, probabilities):
        prob  = max(0.0, min(0.999999, float(prob)))
        index = min(bin_count - 1, int(prob * bin_count))
        bins[index].append((prob, int(label)))
    result = []
    for index, observations in enumerate(bins):
        low  = index / bin_count
        high = (index + 1) / bin_count
        predicted = statistics.mean(v for v, _ in observations) if observations else None
        actual    = statistics.mean(l for _, l in observations) if observations else None
        result.append({
            "range":          f"{low:.1f}-{high:.1f}",
            "count":          len(observations),
            "mean_predicted": round(predicted, 6) if predicted is not None else None,
            "actual_rate":    round(actual, 6)    if actual    is not None else None,
        })
    return result


def calibration_report(labels, probabilities):
    if not labels:
        return {"status": "NO_DATA", "sample_size": 0}
    try:
        brier = round(brier_score_loss(labels, probabilities), 6)
        loss  = round(log_loss(labels, probabilities, labels=[0, 1]), 6)
    except Exception:
        brier = loss = None
    return {
        "status":           "DESCRIPTIVE",
        "sample_size":      len(labels),
        "brier_score":      brier,
        "log_loss":         loss,
        "reliability_bins": calibration_bins(labels, probabilities),
    }


def model_in_sample(model_name, dataset):
    if not SKLEARN_AVAILABLE:
        return {"status": "SKLEARN_NOT_INSTALLED", "model": model_name}
    labels   = dataset["labels"]
    features = dataset["features"]
    if len(labels) < ML_MIN_SAMPLES:
        return {"status": "INSUFFICIENT_SAMPLE", "model": model_name, "sample_size": len(labels), "minimum_required": ML_MIN_SAMPLES}
    counts = Counter(labels)
    if len(counts) < 2:
        return {"status": "ONE_CLASS_ONLY", "model": model_name, "class_counts": dict(counts)}
    if min(counts.values()) < ML_MIN_CLASS_COUNT:
        return {"status": "CLASS_IMBALANCE", "model": model_name, "class_counts": dict(counts)}
    try:
        model = make_model(model_name)
        model.fit(features, labels)
        probs = model.predict_proba(features)[:, 1]
        preds = [1 if v >= 0.5 else 0 for v in probs]
        return {
            "status":      "IN_SAMPLE_DESCRIPTIVE_ONLY",
            "model":       model_name,
            "sample_size": len(labels),
            "class_counts":dict(counts),
            "accuracy":    round(accuracy_score(labels, preds), 6),
            "auc":         round(roc_auc_score(labels, probs), 6) if len(set(labels)) > 1 else None,
            "calibration": calibration_report(labels, [float(v) for v in probs]),
            "parameters":  model_parameters(model, dataset["feature_names"], model_name),
        }
    except Exception as exc:
        return {"status": "MODEL_ERROR", "model": model_name, "error": str(exc)}


def model_walk_forward(model_name, dataset):
    if not SKLEARN_AVAILABLE:
        return {"status": "SKLEARN_NOT_INSTALLED", "model": model_name}
    labels   = dataset["labels"]
    features = dataset["features"]
    if len(labels) < ML_MIN_SAMPLES:
        return {"status": "INSUFFICIENT_SAMPLE", "model": model_name, "sample_size": len(labels), "minimum_required": ML_MIN_SAMPLES}
    if len(set(labels)) < 2:
        return {"status": "ONE_CLASS_ONLY", "model": model_name}
    counts = Counter(labels)
    if min(counts.values()) < ML_MIN_CLASS_COUNT:
        return {"status": "CLASS_IMBALANCE", "model": model_name, "class_counts": dict(counts)}
    split_count = min(ML_MAX_SPLITS, max(2, len(labels) // 10))
    if split_count < 2:
        return {"status": "INSUFFICIENT_SPLITS", "model": model_name}
    splitter = TimeSeriesSplit(n_splits=split_count)
    actual_all = []
    prob_all   = []
    pred_all   = []
    fold_results = []
    for fold, (train_idx, test_idx) in enumerate(splitter.split(features), 1):
        train_f = [features[i] for i in train_idx]
        test_f  = [features[i] for i in test_idx]
        train_l = [labels[i]   for i in train_idx]
        test_l  = [labels[i]   for i in test_idx]
        if len(set(train_l)) < 2:
            fold_results.append({"fold": fold, "status": "TRAIN_ONE_CLASS", "train_size": len(train_idx), "test_size": len(test_idx)})
            continue
        try:
            model = make_model(model_name)
            model.fit(train_f, train_l)
            probs = model.predict_proba(test_f)[:, 1]
            preds = [1 if v >= 0.5 else 0 for v in probs]
            fold_results.append({
                "fold":       fold,
                "status":     "OK",
                "train_size": len(train_idx),
                "test_size":  len(test_idx),
                "accuracy":   round(accuracy_score(test_l, preds), 6),
                "auc":        round(roc_auc_score(test_l, probs), 6) if len(set(test_l)) > 1 else None,
            })
            actual_all.extend(test_l)
            prob_all.extend(float(v) for v in probs)
            pred_all.extend(preds)
        except Exception as exc:
            fold_results.append({"fold": fold, "status": "FOLD_ERROR", "error": str(exc)})
    if len(actual_all) < ML_MIN_OOS_SAMPLES:
        return {"status": "INSUFFICIENT_OOS_SAMPLE", "model": model_name, "sample_size": len(labels), "oos_sample_size": len(actual_all), "folds": fold_results}
    return {
        "status":           "OUT_OF_SAMPLE_DESCRIPTIVE",
        "model":            model_name,
        "sample_size":      len(labels),
        "oos_sample_size":  len(actual_all),
        "oos_accuracy":     round(accuracy_score(actual_all, pred_all), 6),
        "oos_auc":          round(roc_auc_score(actual_all, prob_all), 6) if len(set(actual_all)) > 1 else None,
        "oos_calibration":  calibration_report(actual_all, prob_all),
        "folds":            fold_results,
    }


def run_ml_diagnostics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    trades  = deduplicate_trades(trades)
    dataset = build_ml_dataset(trades)
    if dataset["status"] != "READY":
        return {
            "status":           dataset["status"],
            "sklearn_available":SKLEARN_AVAILABLE,
            "outcome_labels":   {"positive_net_pnl": 0, "non_positive_net_pnl": 0, "observations": 0},
        }
    labels  = dataset["labels"]
    results = {}
    for model_name in ("logistic_regression", "gradient_boosting"):
        results[model_name] = {
            "in_sample":    model_in_sample(model_name, dataset),
            "walk_forward": model_walk_forward(model_name, dataset),
        }
    return {
        "status":           "COMPLETED_OR_LIMITED",
        "sklearn_available":SKLEARN_AVAILABLE,
        "sample_size":      len(labels),
        "class_counts":     dict(Counter(labels)),
        "feature_names":    dataset["feature_names"],
        "models":           results,
        "limitations": [
            "Models predict positive net P&L, not guaranteed target hits.",
            "Trade-level data may contain selection bias.",
            "In-sample metrics are not evidence of live profitability.",
            "Time-series walk-forward results need larger samples.",
            "Calibration is unreliable with very small samples.",
            "No parameter is automatically selected or changed.",
        ],
    }


def parameter_selection_report(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    trades      = deduplicate_trades(trades)
    sample_size = len(trades)
    status      = "INSUFFICIENT_DATA" if sample_size < ML_MIN_SAMPLES else "RESEARCH_READY_NOT_SELECTED"
    return {
        "status":                       status,
        "sample_size":                  sample_size,
        "automatic_tuning":             False,
        "parameter_changes_selected":   [],
        "reason": (
            "The journal cannot select parameters from observed trades alone. "
            "Counterfactual historical replays under alternative configurations are required."
        ),
        "required_data": [
            "Historical NIFTY option-chain bid/ask data",
            "Exact decision-time market features",
            "Alternative parameter replay results",
            "Realistic slippage and charges",
            "Chronological out-of-sample results",
        ],
    }


# ---------------------------------------------------------------------
# Existing patch readers (kept from v4.0)
# ---------------------------------------------------------------------

def get_position_monitor_log(
    conn: sqlite3.Connection,
    trading_date: str,
) -> List[Dict[str, Any]]:
    requested = [
        "timestamp", "trade_id", "strategy_name",
        "current_premium", "stop_loss", "profit_target",
        "unrealized_pnl", "stop_breach_ticks",
        "distance_to_call_pct", "distance_to_put_pct",
        "partial_taken", "action_taken", "spot", "vix",
    ]
    return select_existing(
        conn, "position_monitor_log", requested,
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp",
    )


def get_entry_gate_summary(
    conn: sqlite3.Connection,
    trading_date: str,
) -> Dict[str, Any]:
    rows = select_existing(
        conn, "regime_cycle_log",
        ["timestamp", "entry_gate_passed", "entry_gate_blocked_reason",
         "composite_score", "confirmed_regime", "vix", "iv_atm", "adx"],
        where_sql=f"{timestamp_date_expression('timestamp')} = ?",
        parameters=(trading_date,),
        order_sql="timestamp",
    )
    if not rows:
        return {"status": "NO_DATA"}
    passed  = [r for r in rows if r.get("entry_gate_passed")]
    blocked = [r for r in rows if not r.get("entry_gate_passed")]
    reason_counts = Counter(
        r.get("entry_gate_blocked_reason") or "UNKNOWN"
        for r in blocked
    )
    return {
        "total_cycles":       len(rows),
        "gate_passed_count":  len(passed),
        "gate_blocked_count": len(blocked),
        "blocked_reasons":    dict(reason_counts),
        "pass_rate_pct":      round(len(passed) / max(1, len(rows)) * 100, 2),
    }


def get_pretrade_failure_summary(
    conn: sqlite3.Connection,
    trading_date: str,
) -> Dict[str, Any]:
    rows = select_existing(
        conn, "entry_attempt_log",
        ["timestamp", "pretrade_fail_reason", "build_failure_reason",
         "net_credit", "wing_width", "min_credit_required",
         "max_risk_used", "vix_at_entry", "composite_score", "dte"],
        where_sql=(
            f"{timestamp_date_expression('timestamp')} = ? "
            f"AND pretrade_passed = 0"
        ),
        parameters=(trading_date,),
        order_sql="timestamp",
    )
    if not rows:
        return {"status": "NO_PRETRADE_FAILURES"}
    reason_counts = Counter(
        r.get("pretrade_fail_reason") or r.get("build_failure_reason") or "UNKNOWN"
        for r in rows
    )
    credits = [
        safe_float(r.get("net_credit"))
        for r in rows
        if safe_float(r.get("net_credit")) is not None
    ]
    unknown_n = reason_counts.get("UNKNOWN", 0)
    return {
        "total_failures":    len(rows),
        "failure_reasons":   dict(reason_counts),
        "credit_at_failure": {
            "mean": mean_or_none(credits),
            "min":  min_or_none(credits),
            "max":  max_or_none(credits),
        },
        "unknown_reason_count": unknown_n,
        "unknown_reason_pct":   round(unknown_n / max(1, len(rows)) * 100, 2),
    }


# ---------------------------------------------------------------------
# Build journal
# ---------------------------------------------------------------------

def build_journal(
    trading_date: str,
    db_path: str,
    lookback_days: int,
    run_ml: bool,
    include_raw: bool,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:

    conn = sqlite3.connect(db_path)

    try:
        cycles           = get_regime_cycles(conn, trading_date)
        history          = get_regime_history(conn, trading_date)
        attempts         = get_entry_attempts(conn, trading_date)
        trades_today     = deduplicate_trades(get_closed_trades(conn, trading_date))
        trailing_trades  = deduplicate_trades(get_trailing_closed_trades(conn, lookback_days))
        open_positions   = get_open_positions(conn)
        circuit_breakers = get_circuit_breakers(conn, trading_date)
        capital_state    = get_capital_state(conn)
        monitor_log      = get_position_monitor_log(conn, trading_date)
        entry_gate_summary   = get_entry_gate_summary(conn, trading_date)
        pretrade_summary     = get_pretrade_failure_summary(conn, trading_date)
        order_log            = get_order_log(conn, trading_date)
        market_state_history = get_market_state_history(conn, trading_date)
        cost_breakdown       = get_cost_breakdown(conn, trading_date)
    finally:
        conn.close()

    # Parse audit log for enhanced sections
    log_entries: List[Dict[str, Any]] = []
    if log_path:
        log_entries = parse_audit_log(log_path)
    else:
        # Try to find it automatically
        data_dir = os.path.dirname(db_path)
        auto_log = find_log_for_date(trading_date, data_dir)
        if auto_log:
            log_entries = parse_audit_log(auto_log)
            LOGGER.info("Auto-detected log: %s", auto_log)

    # Enhanced sections from log
    log_regime_cycles  = get_regime_cycle_timeline(log_entries, trading_date)
    market_timeline    = get_market_data_timeline(log_entries, trading_date)
    ws_health          = get_ws_health_timeline(log_entries)
    gate_block_detail  = get_gate_block_analysis(log_entries)
    leg_fills          = get_leg_fill_analysis(log_entries)
    pnl_timeline       = get_pnl_timeline(log_entries)
    builder_taxonomy   = get_builder_failure_taxonomy(log_entries)
    composite_chart    = get_composite_score_chart(log_regime_cycles or cycles)
    signal_accuracy    = get_signal_accuracy_metrics(
        log_regime_cycles or cycles,
        trades_today,
        attempts,
    )

    now = ist_now()

    market_close = getattr(config, "MARKET_CLOSE", None)
    is_after_market_close = (
        now.time() >= market_close if market_close is not None else False
    )

    regime_stats = regime_statistics(cycles)
    entry_stats  = entry_statistics(attempts)
    trade_stats  = trade_statistics(trades_today)

    ml_results = (
        run_ml_diagnostics(trailing_trades)
        if run_ml
        else {"status": "DISABLED_BY_USER", "sklearn_available": SKLEARN_AVAILABLE}
    )

    # Data quality summary
    dq_summary = {
        "regime_cycles_in_db":       len(cycles),
        "regime_cycles_in_log":      len(log_regime_cycles),
        "entry_attempts_in_db":      len(attempts),
        "daily_closed_trades":       len(trades_today),
        "trailing_closed_trades":    len(trailing_trades),
        "order_log_entries":         len(order_log),
        "market_state_snapshots":    len(market_state_history),
        "market_timeline_points":    len(market_timeline),
        "pnl_timeline_points":       len(pnl_timeline),
        "log_entries_parsed":        len(log_entries),
        "log_file_used":             log_path or "auto-detected",
        "entry_failure_reason_coverage": entry_stats.get("failure_reason_coverage", 0),
        "warning": (
            "No profitability conclusion is valid without closed-trade outcome data."
        ),
    }

    journal: Dict[str, Any] = {
        "journal_version":      JOURNAL_VERSION,
        "journal_type":         "NIFTY_OPTIONS_OFFLINE_RESEARCH_JOURNAL",
        "trading_date":         trading_date,
        "generated_at":         ist_iso_now(),
        "timezone":             str(IST),
        "report_status": (
            "EOD_COMPLETE_OR_POST_CLOSE"
            if is_after_market_close
            else "INTRADAY_SNAPSHOT"
        ),
        "engine_version":       "arena-algo-trading",
        "automatic_live_tuning":False,

        # --- Parameters ---
        "parameters":           get_current_parameters(),

        # --- Data quality ---
        "data_quality":         dq_summary,

        # --- Market context ---
        "market_context":       regime_stats.get("market", {}),

        # --- Regime analysis ---
        "regime_statistics":    regime_stats,

        # --- NEW: Intraday regime cycle timeline ---
        "regime_cycle_timeline": {
            "source":       "audit_log" if log_regime_cycles else "sqlite",
            "cycle_count":  len(log_regime_cycles or cycles),
            "cycles":       (log_regime_cycles or cycles)[:200],  # cap
            "composite_chart": composite_chart,
        },

        # --- NEW: Intraday market data timeline ---
        "market_data_timeline": {
            "point_count": len(market_timeline),
            "spot_range": (
                (
                    min(p["spot"] for p in market_timeline if "spot" in p),
                    max(p["spot"] for p in market_timeline if "spot" in p),
                )
                if market_timeline else None
            ),
            "vix_range": (
                (
                    min(p["vix"] for p in market_timeline if "vix" in p),
                    max(p["vix"] for p in market_timeline if "vix" in p),
                )
                if market_timeline else None
            ),
            "iv_atm_pct_range": (
                (
                    min(p["iv_atm_pct"] for p in market_timeline if "iv_atm_pct" in p),
                    max(p["iv_atm_pct"] for p in market_timeline if "iv_atm_pct" in p),
                )
                if any("iv_atm_pct" in p for p in market_timeline) else None
            ),
            "timeline": market_timeline,
        },

        # --- NEW: WS health timeline ---
        "ws_health":            ws_health,

        # --- Entry analysis ---
        "entry_statistics":     entry_stats,

        # --- NEW: Gate block detail ---
        "gate_block_detail":    gate_block_detail,

        # --- NEW: Builder failure taxonomy ---
        "builder_failure_taxonomy": builder_taxonomy,

        # --- NEW: Leg fill analysis ---
        "leg_fill_analysis":    leg_fills,

        # --- NEW: Intraday P&L timeline ---
        "pnl_timeline":         pnl_timeline,

        # --- Trade analysis ---
        "trade_statistics":     trade_stats,
        "nifty_trade_buckets":  nifty_trade_buckets(trades_today),

        # --- NEW: Cost breakdown ---
        "cost_breakdown":       cost_breakdown,

        # --- Regime outcome ---
        "regime_outcome_analysis": (
            {
                "status": "NO_TRADES",
                "note":   "Regime accuracy cannot be measured without closed trades.",
            }
            if not trades_today
            else {
                "status": "TRADE_OUTCOME_ASSOCIATION",
                "note":   "Associates entry regimes with outcomes; not ground-truth accuracy.",
            }
        ),

        # --- Module outcome ---
        "module_outcome_analysis": module_outcome_analysis(trades_today),

        # --- NEW: Signal accuracy metrics ---
        "signal_accuracy_metrics": signal_accuracy,

        # --- ML ---
        "ml_diagnostics":       ml_results,
        "parameter_selection":  parameter_selection_report(trailing_trades),

        # --- Capital & risk ---
        "capital_state":        capital_state,
        "open_positions":       open_positions,
        "circuit_breakers":     circuit_breakers,

        # --- Order log ---
        "order_log_summary": {
            "total_orders":   len(order_log),
            "filled_orders":  sum(1 for o in order_log if o.get("status") in ("complete", "filled", "COMPLETE")),
            "paper_orders":   sum(1 for o in order_log if o.get("paper_trade")),
            "by_action":      dict(Counter(o.get("action") for o in order_log)),
            "by_strategy":    dict(Counter(o.get("trade_id", "")[:8] for o in order_log)),
        },

        # --- Market state history ---
        "market_state_history": {
            "snapshot_count": len(market_state_history),
            "first":          market_state_history[0]  if market_state_history else None,
            "last":           market_state_history[-1] if market_state_history else None,
        },

        # --- Existing patch sections ---
        "entry_gate_summary":   entry_gate_summary,
        "pretrade_summary":     pretrade_summary,
        "position_monitor_summary": {
            "total_events":  len(monitor_log),
            "actions":       dict(Counter(r.get("action_taken", "UNKNOWN") for r in monitor_log)),
            "stop_fires":    sum(1 for r in monitor_log if r.get("action_taken") == "STOP_FIRED"),
            "profit_hits":   sum(1 for r in monitor_log if r.get("action_taken") == "PROFIT_TARGET"),
        },
    }

    if include_raw:
        journal["raw_data"] = {
            "regime_cycles":    cycles,
            "regime_history":   history,
            "entry_attempts":   attempts,
            "trades_today":     trades_today,
            "trailing_trades":  trailing_trades,
            "order_log":        order_log,
            "monitor_log":      monitor_log,
            "market_timeline":  market_timeline,
        }
    else:
        journal["raw_data"] = {
            "included":             False,
            "reason":               "Use --include-raw to include full rows.",
            "last_regime_cycles":   cycles[-10:],
            "last_entry_attempts":  attempts[-10:],
            "last_pnl_snapshots":   pnl_timeline[-5:],
        }

    return journal


# ---------------------------------------------------------------------
# Enhanced LLM prompt
# ---------------------------------------------------------------------

def build_llm_prompt(journal: Dict[str, Any]) -> str:
    compact = {
        "trading_date":              journal["trading_date"],
        "report_status":             journal["report_status"],
        "automatic_live_tuning":     False,
        "parameters":                journal["parameters"],
        "data_quality":              journal["data_quality"],
        "market_context":            journal["market_context"],
        "regime_statistics":         journal["regime_statistics"],
        "regime_cycle_timeline_summary": {
            "cycle_count":    journal["regime_cycle_timeline"]["cycle_count"],
            "composite_chart":journal["regime_cycle_timeline"]["composite_chart"],
        },
        "market_data_timeline_summary": {
            "spot_range":     journal["market_data_timeline"]["spot_range"],
            "vix_range":      journal["market_data_timeline"]["vix_range"],
            "iv_atm_pct_range":journal["market_data_timeline"]["iv_atm_pct_range"],
        },
        "ws_health":                 journal["ws_health"],
        "entry_statistics":          journal["entry_statistics"],
        "gate_block_detail":         journal["gate_block_detail"],
        "builder_failure_taxonomy":  journal["builder_failure_taxonomy"],
        "leg_fill_analysis":         journal["leg_fill_analysis"],
        "pnl_timeline_summary": {
            "point_count": len(journal["pnl_timeline"]),
            "first":       journal["pnl_timeline"][0]  if journal["pnl_timeline"] else None,
            "last":        journal["pnl_timeline"][-1] if journal["pnl_timeline"] else None,
        },
        "trade_statistics":          journal["trade_statistics"],
        "cost_breakdown":            journal["cost_breakdown"],
        "nifty_trade_buckets":       journal["nifty_trade_buckets"],
        "signal_accuracy_metrics":   journal["signal_accuracy_metrics"],
        "regime_outcome_analysis":   journal["regime_outcome_analysis"],
        "module_outcome_analysis":   journal["module_outcome_analysis"],
        "ml_diagnostics":            journal["ml_diagnostics"],
        "parameter_selection":       journal["parameter_selection"],
        "capital_state":             journal["capital_state"],
        "circuit_breakers":          journal["circuit_breakers"],
        "entry_gate_summary":        journal.get("entry_gate_summary", {}),
        "pretrade_summary":          journal.get("pretrade_summary", {}),
        "position_monitor_summary":  journal.get("position_monitor_summary", {}),
        "order_log_summary":         journal.get("order_log_summary", {}),
    }

    return f"""
You are reviewing an offline research journal for a NIFTY-options
algorithmic trading engine.

Report date: {journal["trading_date"]}

This is analysis only. You are not authorized to:
- modify config.py;
- change live parameters;
- place, cancel or modify orders;
- invent missing trades;
- invent missing P&L;
- treat in-sample results as proof;
- treat model feature importance as causation;
- recommend live tuning from insufficient data.

The human operator will manually review every proposal.

NIFTY-specific rules:
1. Respect the configured NIFTY expiry calendar (Tuesday expiry).
2. Analyse Tuesday expiry and expiry-gamma risk separately.
3. Separate Monday, Tuesday, Wednesday, Thursday and Friday.
4. Separate VIX below 12, 12-16, 16-22 and 22+.
5. Separate DTE 0-1, 2-3, 4-6 and 7+.
6. Consider NIFTY overnight gaps and event risk.
7. Consider ATM IV, RV, skew, term structure and trend.
8. Treat OI flow as a delayed proxy, not proof of buying or selling.
9. Use net P&L after all costs and slippage.
10. Do not tune from zero or very small trade samples.
11. Prefer chronological walk-forward results.
12. Do not call a regime label accurate without a forward market label.
13. Do not assume atomic basket routing exists.
14. Do not increase position size solely because India VIX is low.
15. If failure reasons are generic, report them as unknown.
16. If this is an intraday snapshot, do not describe it as completed EOD.
17. Analyse the composite score chart for regime stability.
18. Analyse gate block frequency — distinguish time-of-day blocks
    from structural blocks (vega gate, credit gate, etc.).
19. Analyse WS health — distinguish token expiry from genuine
    connectivity issues.
20. Analyse leg-level slippage — flag if slippage exceeds 2pts/leg.
21. Analyse cost drag — flag if costs exceed 20% of gross P&L.
22. Analyse builder failure taxonomy — identify the primary blocker.

Return exactly these sections:

A. DATA QUALITY AND LOG COMPLETENESS
B. REGIME BEHAVIOUR AND COMPOSITE SCORE EVOLUTION
C. MODULE SIGNAL ANALYSIS (vol/edge/trend/flow separately)
D. ENTRY GATE ANALYSIS (time-of-day vs structural blocks)
E. BUILDER FAILURE ANALYSIS (taxonomy and primary cause)
F. TRADE EXECUTION ANALYSIS (fills, slippage, leg detail)
G. INTRADAY P&L TIMELINE
H. COST BREAKDOWN AND DRAG
I. TRADE PROFITABILITY
J. WEBSOCKET AND DATA QUALITY
K. LOGISTIC REGRESSION
L. GRADIENT BOOSTING
M. WALK-FORWARD VALIDATION
N. PROBABILITY CALIBRATION
O. OUT-OF-SAMPLE PARAMETER SELECTION
P. NIFTY-SPECIFIC RISKS
Q. TOMORROW WATCH LIST
R. FINAL ACTION

For every proposed parameter change, use exactly:

PARAMETER:
CURRENT VALUE:
PROPOSED VALUE:
DATA SUPPORT:
OUT-OF-SAMPLE SUPPORT:
EXPECTED BENEFIT:
RISK:
SAMPLE SIZE:
CONFIDENCE: HIGH / MEDIUM / LOW
ACTION: HOLD / PAPER_TEST / MANUAL_REVIEW

Rules:
- If there are no closed trades, final action must be HOLD.
- If fewer than 30 usable chronological trades, do not approve performance-based tuning.
- If only in-sample ML evidence exists, output HOLD.
- Do not lower a credit floor merely to create trades.
- Do not weaken Greek/risk gates merely to increase frequency.
- Do not increase lot size without cost-adjusted out-of-sample evidence.
- Separate instrumentation fixes from trading-parameter changes.
- If vega gate blocked all entries, confirm FIX-1 is applied and note it.
- If WS was HTTP 403, confirm token refresh is required and note it.

Structured journal:

{json.dumps(compact, indent=2, default=json_default)}
""".strip()


# ---------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------

def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, default=json_default),
        encoding="utf-8",
    )


def log_generation(
    db_path: str,
    trading_date: str,
    journal_path: Path,
    prompt_path: Path,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO journal_generation_log (
                generated_at, trading_date, journal_file, prompt_file
            ) VALUES (?, ?, ?, ?)
            """,
            (ist_iso_now(), trading_date, str(journal_path), str(prompt_path)),
        )
        conn.commit()
    finally:
        conn.close()


def print_summary(
    journal: Dict[str, Any],
    journal_path: Path,
    prompt_path: Path,
) -> None:
    trade_stats = journal["trade_statistics"]
    entry_stats = journal["entry_statistics"]
    ml_results  = journal["ml_diagnostics"]
    dq          = journal["data_quality"]
    ws          = journal["ws_health"]
    bt          = journal["builder_failure_taxonomy"]
    gbd         = journal["gate_block_detail"]

    print()
    print("=" * 78)
    print(f"NIFTY OPTIONS DECISION JOURNAL v{journal['journal_version']} — {journal['trading_date']}")
    print("=" * 78)
    print(f"Report status         : {journal['report_status']}")
    print(f"Log entries parsed    : {dq.get('log_entries_parsed', 0):,}")
    print(f"Regime cycles (DB)    : {dq.get('regime_cycles_in_db', 0)}")
    print(f"Regime cycles (log)   : {dq.get('regime_cycles_in_log', 0)}")
    print(f"Entry attempts        : {dq.get('entry_attempts_in_db', 0)}")
    print(f"Closed trades         : {trade_stats['trades_closed']}")
    print(f"Net P&L               : Rs {trade_stats['total_net_pnl']:,.2f}")
    print(f"Gate blocks total     : {gbd.get('total_blocks', 0)}")
    print(f"Primary gate blocker  : {gbd.get('primary_blocker', 'N/A')} "
          f"({gbd.get('primary_count', 0)}x)")
    print(f"Builder failures      : {bt.get('total_failures', 0)}")
    print(f"Primary build failure : {bt.get('primary_category', 'N/A')}")
    print(f"Vega gate blocks      : {bt.get('vega_gate_count', 0)}")
    print(f"Fix-1 applied note    : {bt.get('fix_note', 'N/A')}")
    print(f"WS health             : {ws.get('assessment', 'N/A')}")
    print(f"WS error count        : {ws.get('error_count', 0)}")
    print(f"ML diagnostics        : {ml_results.get('status', 'UNKNOWN')}")
    print(f"Automatic tuning      : DISABLED")
    print()

    # Print composite chart
    chart = journal.get("regime_cycle_timeline", {}).get("composite_chart", "")
    if chart:
        print("COMPOSITE SCORE CHART:")
        print(chart[:2000])  # cap output
        print()

    print("Files written:")
    print(f"  JSON   : {journal_path}")
    print(f"  Prompt : {prompt_path}")
    print()
    print("No engine configuration or trading state was changed.")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline NIFTY-options journal and AI analysis generator"
    )
    parser.add_argument(
        "--date", default=ist_today().isoformat(),
        help="Trading date YYYY-MM-DD",
    )
    parser.add_argument(
        "--db", default=config.STATE_DB,
        help="SQLite state database path",
    )
    parser.add_argument(
        "--output-dir", default="journals",
        help="Output directory",
    )
    parser.add_argument(
        "--lookback", type=int, default=30,
        help="Trailing closed-trade lookback in days",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Create journal-owned support tables and exit",
    )
    parser.add_argument(
        "--no-ml", action="store_true",
        help="Disable optional offline ML diagnostics",
    )
    parser.add_argument(
        "--include-raw", action="store_true",
        help="Include full raw rows in JSON",
    )
    parser.add_argument(
        "--log", default=None,
        help="Path to audit log file (auto-detected if not specified)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    if args.lookback < 1:
        print("--lookback must be at least 1", file=sys.stderr)
        return 1

    db_path = str(args.db)
    if not os.path.isfile(db_path):
        print(f"SQLite database not found: {db_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ensure_journal_table(db_path)

    if args.init:
        print(f"Journal support table initialized in {db_path}")
        return 0

    # Resolve log path
    log_path = args.log
    if not log_path:
        data_dir = os.path.dirname(db_path)
        log_path = find_log_for_date(args.date, data_dir)
        if log_path:
            LOGGER.info("Auto-detected log file: %s", log_path)
        else:
            LOGGER.warning(
                "No audit log found for %s in %s — "
                "log-based sections will be empty. "
                "Use --log to specify the path.",
                args.date, data_dir,
            )

    LOGGER.info("Generating NIFTY journal for %s", args.date)

    journal = build_journal(
        trading_date=args.date,
        db_path=db_path,
        lookback_days=args.lookback,
        run_ml=not args.no_ml,
        include_raw=args.include_raw,
        log_path=log_path,
    )

    journal_path = output_dir / f"journal_{args.date}.json"
    prompt_path  = output_dir / f"llm_prompt_{args.date}.txt"

    write_json(journal_path, journal)
    prompt_path.write_text(build_llm_prompt(journal), encoding="utf-8")

    log_generation(db_path, args.date, journal_path, prompt_path)
    print_summary(journal, journal_path, prompt_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())