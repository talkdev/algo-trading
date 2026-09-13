# eod_report.py
# NIFTY Intraday Options Engine v3.0
# End-of-day forensic report generator.
# Reads all stored data from database and generates a comprehensive
# Markdown report with 40 sections covering every aspect of the trading day.

from __future__ import annotations

import glob
import json
import statistics
import sqlite3
import csv
from pathlib import Path
from datetime import datetime, date as _date_cls, timedelta
from typing import Optional, List, Dict, Any, Tuple

BASE_DIR   = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "reports"


# ─────────────────────────────────────────────────────────────────────────────
# ENV LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_env_simple(path: Path) -> dict:
    """Load key=value pairs from env.txt without importing core."""
    env: dict = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_ENV             = load_env_simple(BASE_DIR / "env.txt")
DB_PATH          = Path(_ENV.get("DB_PATH", "data/nifty_algo_v3.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
LOG_DIR          = Path(_ENV.get("LOG_DIR", "logs"))
if not LOG_DIR.is_absolute():
    LOG_DIR = BASE_DIR / LOG_DIR

# v7: these defaults must agree with the ones core.load_config() ships, or
# this report describes a different engine from the one that traded. They did
# not: LOT_SIZE defaulted to 75 (the pre-January-2026 NIFTY lot; the engine
# has used 65 since the January 2026 series and core.py defaults to 65),
# EXCHANGE_TXN_RATE to 0.0003552 against the engine's 0.0003553, STT to the
# superseded 0.10% against the 0.15% that applies from 1 April 2026, and the
# daily loss limit to 2% against the engine's 4%. env.txt holds only the
# Upstox token by design, so in practice these defaults were the values in
# force - which made cost_per_lot_rupees wrong by 15% before the metric
# itself was even computed correctly.
LOT_SIZE              = int(_ENV.get("NIFTY_LOT_SIZE", "65") or 65)
STARTING_CAPITAL      = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1_000_000)
MAX_DAILY_LOSS_PCT    = float(_ENV.get("MAX_DAILY_LOSS_PCT", "0.04") or 0.04)
BROKERAGE_PER_ORDER   = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)
EXCHANGE_TXN_RATE     = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003553") or 0.0003553)
STT_OPTIONS_SELL      = float(_ENV.get("STT_OPTIONS_SELL", "0.0015") or 0.0015)


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open read-only SQLite connection."""
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}.")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cols = {
            row[1] for row in
            conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        return column in cols
    except Exception:
        return False


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[dict]:
    """Execute query and return list of dicts."""
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def q1(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict]:
    """Execute query and return first row as dict."""
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCH FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_session_state(conn: sqlite3.Connection, d: str) -> Optional[dict]:
    if not table_exists(conn, "session_state"):
        return None
    return q1(conn, "SELECT * FROM session_state WHERE trading_date=?", (d,))


def fetch_cycle_log(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "cycle_log"):
        return []
    return q(conn,
        "SELECT * FROM cycle_log WHERE trading_date=? ORDER BY cycle_id", (d,))


def fetch_strategy_decisions(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "strategy_decisions"):
        return []
    return q(conn,
        "SELECT * FROM strategy_decisions WHERE trading_date=? ORDER BY decision_id",
        (d,))


def fetch_positions(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "positions"):
        return []
    return q(conn,
        "SELECT * FROM positions WHERE trading_date=? ORDER BY entry_time", (d,))


def fetch_position_legs(conn: sqlite3.Connection, position_ids: List[str]) -> List[dict]:
    if not position_ids or not table_exists(conn, "position_legs"):
        return []
    placeholders = ",".join("?" for _ in position_ids)
    return q(conn,
        f"SELECT * FROM position_legs WHERE position_id IN ({placeholders}) "
        f"ORDER BY position_id, leg_id",
        tuple(position_ids))


def fetch_trade_entries(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "trade_entries"):
        return []
    return q(conn,
        "SELECT * FROM trade_entries WHERE trading_date=? ORDER BY entry_time", (d,))


def fetch_trade_exits(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "trade_exits"):
        return []
    return q(conn,
        """SELECT te.* FROM trade_exits te
           JOIN positions p ON te.position_id = p.position_id
           WHERE p.trading_date=? ORDER BY te.exit_time""",
        (d,))


def fetch_daily_summary(conn: sqlite3.Connection, d: str) -> Optional[dict]:
    if not table_exists(conn, "daily_summary"):
        return None
    return q1(conn, "SELECT * FROM daily_summary WHERE trading_date=?", (d,))


def fetch_regime_decisions(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "regime_decisions"):
        return []
    return q(conn,
        "SELECT * FROM regime_decisions WHERE date=? ORDER BY timestamp", (d,))


def fetch_calibration_state(conn: sqlite3.Connection) -> Optional[dict]:
    if not table_exists(conn, "calibration_state"):
        return None
    return q1(conn,
        "SELECT * FROM calibration_state WHERE is_valid=1 "
        "ORDER BY calibrated_at DESC LIMIT 1")


def fetch_calibration_drift(conn: sqlite3.Connection) -> List[dict]:
    if not table_exists(conn, "calibration_state"):
        return []
    return q(conn,
        "SELECT * FROM calibration_state ORDER BY calibrated_at DESC LIMIT 20")


def fetch_option_chain_snapshot(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "option_chain_snapshot"):
        return []
    return q(conn,
        "SELECT * FROM option_chain_snapshot "
        "WHERE trading_date=? ORDER BY capture_time, strike, option_type",
        (d,))


def fetch_api_call_log(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "api_call_log"):
        return []
    return q(conn,
        "SELECT * FROM api_call_log WHERE call_time LIKE ? ORDER BY call_time",
        (f"{d}%",))


def fetch_audit_log_db(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "audit_log"):
        return []
    return q(conn,
        "SELECT * FROM audit_log WHERE log_time LIKE ? ORDER BY log_time",
        (f"{d}%",))


def fetch_audit_log_file_lines(log_dir: Path, d: str) -> List[str]:
    """Read audit log lines for the given date from rotating log files."""
    lines: List[str] = []
    if not log_dir.exists():
        return lines
    for filepath in sorted(glob.glob(str(log_dir / "nifty_algo_audit.log*"))):
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith(d):
                        lines.append(line.rstrip("\n"))
        except Exception:
            continue
    lines.sort(key=lambda l: l[:19])
    return lines


def fetch_prior_days_summary(
    conn: sqlite3.Connection, d: str, n: int = 10
) -> List[dict]:
    if not table_exists(conn, "daily_summary"):
        return []
    return q(conn,
        "SELECT * FROM daily_summary WHERE trading_date < ? "
        "ORDER BY trading_date DESC LIMIT ?",
        (d, n))


def fetch_intraday_candles(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "intraday_candles"):
        return []
    return q(conn,
        "SELECT candle_time, open, high, low, close, volume "
        "FROM intraday_candles WHERE trading_date=? AND interval_min=1 "
        "ORDER BY candle_time",
        (d,))


def fetch_cumulative_performance(
    conn: sqlite3.Connection, d: str, lookback_days: int = 90
) -> List[dict]:
    if not table_exists(conn, "daily_summary"):
        return []
    cutoff = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return q(conn,
        "SELECT * FROM daily_summary WHERE trading_date >= ? AND trading_date <= ? "
        "ORDER BY trading_date",
        (cutoff, d))


def fetch_vix_history_today(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "vix_history"):
        return []
    return q(conn,
        "SELECT * FROM vix_history WHERE date=? ORDER BY timestamp", (d,))


def fetch_market_snapshots_today(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "market_snapshots"):
        return []
    return q(conn,
        "SELECT * FROM market_snapshots WHERE date=? ORDER BY timestamp", (d,))


def fetch_phantom_trades_today(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "phantom_trades"):
        return []
    return q(conn,
        "SELECT * FROM phantom_trades WHERE trading_date=? ORDER BY block_time", (d,))


def fetch_exit_quality_today(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "exit_quality_log"):
        return []
    return q(conn,
        "SELECT * FROM exit_quality_log WHERE trading_date=? ORDER BY exit_time", (d,))


def fetch_regime_accuracy_today(conn: sqlite3.Connection, d: str) -> List[dict]:
    if not table_exists(conn, "regime_accuracy_scores"):
        return []
    return q(conn,
        "SELECT * FROM regime_accuracy_scores WHERE trading_date=? ORDER BY score_id",
        (d,))


# ─────────────────────────────────────────────────────────────────────────────
# COMPUTATION FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def compute_candle_statistics(candles: List[dict]) -> dict:
    """Compute statistics from 1-minute candle bars."""
    if not candles:
        return {}
    closes  = [float(c["close"]) for c in candles if c.get("close")]
    highs   = [float(c["high"])  for c in candles if c.get("high")]
    lows    = [float(c["low"])   for c in candles if c.get("low")]
    volumes = [int(c["volume"])  for c in candles if c.get("volume") is not None]
    if not closes:
        return {}
    ranges = [
        float(c["high"]) - float(c["low"])
        for c in candles
        if c.get("high") and c.get("low")
    ]
    result = {
        "total_1min_bars":   len(candles),
        "first_bar_time":    candles[0]["candle_time"] if candles else None,
        "last_bar_time":     candles[-1]["candle_time"] if candles else None,
        "open":              closes[0],
        "close":             closes[-1],
        "high":              max(highs) if highs else None,
        "low":               min(lows)  if lows  else None,
        "total_volume":      sum(volumes) if volumes else 0,
        "avg_bar_range_pts": round(statistics.mean(ranges), 3) if ranges else None,
        "max_bar_range_pts": round(max(ranges), 3) if ranges else None,
        "zero_volume_bars":  sum(1 for v in volumes if (v or 0) == 0),
        "volume_note":       "NSE index volume always 0 via Upstox API",
    }
    if len(closes) >= 2:
        result["net_change_pts"] = round(closes[-1] - closes[0], 2)
        result["net_change_pct"] = round((closes[-1] - closes[0]) / closes[0] * 100, 3)
    return result


def compute_equity_curve(cumulative_days: List[dict]) -> dict:
    """Compute equity curve metrics from daily summary rows."""
    if not cumulative_days:
        return {}
    pnls    = [float(d.get("net_pnl_rupees") or 0) for d in cumulative_days]
    capital = [float(d.get("capital_end") or 0) for d in cumulative_days
               if d.get("capital_end")]
    wins    = sum(1 for p in pnls if p > 0)
    losses  = sum(1 for p in pnls if p < 0)
    total_pnl    = sum(pnls)
    gross_wins   = sum(p for p in pnls if p > 0)
    gross_losses = abs(sum(p for p in pnls if p < 0))

    max_dd = 0.0
    if len(capital) >= 2:
        peak = capital[0]
        for c in capital:
            if c > peak:
                peak = c
            dd = peak - c
            if dd > max_dd:
                max_dd = dd

    return {
        "total_trading_days":   len(cumulative_days),
        "profitable_days":      wins,
        "loss_days":            losses,
        "flat_days":            len(cumulative_days) - wins - losses,
        "day_win_rate_pct":     round(wins / len(cumulative_days) * 100, 1)
                                if cumulative_days else 0,
        "total_pnl_rupees":     round(total_pnl, 2),
        "avg_daily_pnl_rupees": round(total_pnl / len(cumulative_days), 2)
                                if cumulative_days else 0,
        "gross_wins_rupees":    round(gross_wins, 2),
        "gross_losses_rupees":  round(gross_losses, 2),
        "profit_factor":        round(gross_wins / gross_losses, 3)
                                if gross_losses > 0 else None,
        "max_drawdown_rupees":  round(max_dd, 2),
        "capital_start":        capital[0]  if capital else None,
        "capital_end":          capital[-1] if capital else None,
        "total_return_pct":     round((capital[-1] - capital[0]) / capital[0] * 100, 3)
                                if len(capital) >= 2 and capital[0] > 0 else 0,
        "daily_pnl_series": [
            {
                "date":    d.get("trading_date"),
                "pnl":     d.get("net_pnl_rupees"),
                "capital": d.get("capital_end"),
            }
            for d in cumulative_days
        ],
    }


def compute_vrp_statistics(cycle_rows: List[dict]) -> dict:
    """Compute VRP statistics from cycle log rows."""
    vrps = [
        float(c.get("vrp_smoothed") or c.get("vrp_raw") or 0)
        for c in cycle_rows
        if (c.get("vrp_smoothed") or c.get("vrp_raw")) is not None
    ]
    ivs  = [float(c["atm_iv_pct"]) for c in cycle_rows if c.get("atm_iv_pct")]
    rvs  = [float(c["parkinson_rv_pct"]) for c in cycle_rows if c.get("parkinson_rv_pct")]

    if not vrps:
        return {}

    result = {
        "vrp_mean":             round(statistics.mean(vrps), 3),
        "vrp_min":              round(min(vrps), 3),
        "vrp_max":              round(max(vrps), 3),
        "vrp_stdev":            round(statistics.stdev(vrps), 3) if len(vrps) > 1 else 0,
        "vrp_positive_cycles":  sum(1 for v in vrps if v > 0),
        "vrp_negative_cycles":  sum(1 for v in vrps if v <= 0),
        "vrp_rich_cycles":      sum(1 for v in vrps if v > 3.0),
        "vrp_very_rich_cycles": sum(1 for v in vrps if v > 4.5),
        "vrp_cheap_cycles":     sum(1 for v in vrps if v < 0),
        "total_vrp_cycles":     len(vrps),
    }
    if ivs:
        result["atm_iv_open_pct"]  = round(ivs[0], 3)
        result["atm_iv_close_pct"] = round(ivs[-1], 3)
        result["atm_iv_mean_pct"]  = round(statistics.mean(ivs), 3)
        result["atm_iv_min_pct"]   = round(min(ivs), 3)
        result["atm_iv_max_pct"]   = round(max(ivs), 3)
        result["iv_crush_pct"]     = round(ivs[0] - ivs[-1], 3) if len(ivs) > 1 else 0
    if rvs:
        result["parkinson_rv_mean_pct"] = round(statistics.mean(rvs), 3)
        result["parkinson_rv_min_pct"]  = round(min(rvs), 3)
        result["parkinson_rv_max_pct"]  = round(max(rvs), 3)
    return result


def compute_vix_profile(vix_rows: List[dict]) -> dict:
    """Compute VIX intraday profile."""
    if not vix_rows:
        return {}
    vals = [float(r["vix_value"]) for r in vix_rows if r.get("vix_value")]
    if not vals:
        return {}
    return {
        "vix_open":       round(vals[0], 2),
        "vix_close":      round(vals[-1], 2),
        "vix_high":       round(max(vals), 2),
        "vix_low":        round(min(vals), 2),
        "vix_range":      round(max(vals) - min(vals), 2),
        "vix_change_pct": round((vals[-1] - vals[0]) / vals[0] * 100, 2)
                          if vals[0] > 0 else 0,
        "readings_count": len(vals),
        "pct_above_20":   round(sum(1 for v in vals if v > 20) / len(vals) * 100, 1),
        "pct_below_14":   round(sum(1 for v in vals if v < 14) / len(vals) * 100, 1),
        "pct_below_12":   round(sum(1 for v in vals if v < 12) / len(vals) * 100, 1),
    }


def compute_vrp_curve(cycle_rows: List[dict]) -> List[dict]:
    """Build VRP intraday curve from cycle log."""
    curve = []
    for c in cycle_rows:
        vrp = c.get("vrp_smoothed") or c.get("vrp_raw")
        if vrp is not None:
            curve.append({
                "time":              str(c.get("cycle_time", ""))[:19],
                "vrp_smoothed":      round(float(vrp), 3),
                "vrp_raw":           round(float(c.get("vrp_raw") or vrp), 3),
                "atm_iv_pct":        round(float(c["atm_iv_pct"]), 3)
                                     if c.get("atm_iv_pct") else None,
                "parkinson_rv_pct":  round(float(c["parkinson_rv_pct"]), 3)
                                     if c.get("parkinson_rv_pct") else None,
                "vol_regime":        c.get("vol_regime"),
                "iv_behavior":       c.get("iv_behavior"),
                "day_move_used_pct": c.get("day_move_used_pct"),
            })
    return curve


def compute_or_analysis(
    session_state: Optional[dict],
    cycle_rows:    List[dict],
    trade_entries: List[dict],
) -> dict:
    """Analyse opening range and its relationship to entries."""
    if not session_state:
        return {"or_computed": False}
    or_high = session_state.get("or_high")
    or_low  = session_state.get("or_low")
    or_width = session_state.get("or_width")
    if not or_high or not or_low:
        return {"or_computed": False}

    spots = [float(c["spot"]) for c in cycle_rows if c.get("spot")]
    return {
        "or_computed":             True,
        "or_high":                 or_high,
        "or_low":                  or_low,
        "or_width_pts":            or_width,
        "or_condition":            session_state.get("or_condition"),
        "or_width_pct":            round(or_width / ((or_high + or_low) / 2) * 100, 3)
                                   if or_width else None,
        "entries_above_or":        sum(1 for t in trade_entries
                                       if t.get("entry_spot") and
                                       float(t["entry_spot"]) > or_high),
        "entries_below_or":        sum(1 for t in trade_entries
                                       if t.get("entry_spot") and
                                       float(t["entry_spot"]) < or_low),
        "entries_in_or":           sum(1 for t in trade_entries
                                       if t.get("entry_spot") and
                                       or_low <= float(t["entry_spot"]) <= or_high),
        "max_excursion_above_pts": round(max(
            (float(s) - or_high for s in spots if float(s) > or_high), default=0
        ), 1),
        "max_excursion_below_pts": round(max(
            (or_low - float(s) for s in spots if float(s) < or_low), default=0
        ), 1),
    }


def compute_intraday_spot_profile(cycle_rows: List[dict]) -> dict:
    """Compute intraday NIFTY spot profile from cycle log."""
    spots = [
        (str(c.get("cycle_time", "")), float(c["spot"]))
        for c in cycle_rows
        if c.get("spot") is not None
    ]
    if not spots:
        return {}
    spot_values = [s[1] for s in spots]
    result = {
        "open":           spot_values[0],
        "close":          spot_values[-1],
        "high":           max(spot_values),
        "low":            min(spot_values),
        "range_pts":      round(max(spot_values) - min(spot_values), 2),
        "range_pct":      round((max(spot_values) - min(spot_values)) / spot_values[0] * 100, 3),
        "net_change_pts": round(spot_values[-1] - spot_values[0], 2),
        "net_change_pct": round((spot_values[-1] - spot_values[0]) / spot_values[0] * 100, 3),
        "direction":      "UP" if spot_values[-1] > spot_values[0]
                          else ("DOWN" if spot_values[-1] < spot_values[0] else "FLAT"),
    }
    if len(spot_values) >= 6:
        mid = len(spot_values) // 2
        fh  = max(spot_values[:mid]) - min(spot_values[:mid])
        sh  = max(spot_values[mid:]) - min(spot_values[mid:])
        result["first_half_range_pts"]             = round(fh, 2)
        result["second_half_range_pts"]            = round(sh, 2)
        result["volatility_expansion_second_half"] = sh > fh
    return result


def compute_regime_distribution(
    regime_decisions: List[dict], cycle_rows: List[dict]
) -> dict:
    """Compute regime distribution for the day."""
    dist: dict = {}
    for r in regime_decisions:
        fr = r.get("final_regime") or "UNKNOWN"
        dist[fr] = dist.get(fr, 0) + 1
    if not dist:
        for c in cycle_rows:
            fr = c.get("final_regime") or c.get("action_taken") or "UNKNOWN"
            if fr not in ("SIGNAL_ONLY", None):
                dist[fr] = dist.get(fr, 0) + 1
    return dict(sorted(dist.items(), key=lambda x: -x[1]))


def compute_regime_timeline(
    regime_decisions: List[dict], cycle_rows: List[dict]
) -> List[dict]:
    """Build regime timeline from decisions and cycle log."""
    timeline = []
    for r in regime_decisions:
        timeline.append({
            "time":              r.get("timestamp", ""),
            "source":            "regime_engine",
            "vol_regime":        r.get("vol_regime"),
            "price_regime":      r.get("price_regime"),
            "positioning_regime":r.get("positioning_regime"),
            "final_regime":      r.get("final_regime"),
            "confidence_level":  r.get("confidence_level"),
            "confidence_score":  r.get("confidence_score"),
            "size_multiplier":   r.get("size_multiplier"),
            "block_new_entries": r.get("block_new_entries"),
            "adx_15":            r.get("adx_15"),
            "ema_structure":     r.get("ema_structure"),
            "vrp_smoothed":      r.get("vrp_smoothed"),
            "iv_behavior":       None,
            "day_move_used_pct": r.get("day_move_used_pct"),
            "calibration_tier":  r.get("calibration_tier"),
            "notes":             r.get("notes"),
        })
    for c in cycle_rows:
        if c.get("final_regime"):
            timeline.append({
                "time":              c.get("cycle_time", ""),
                "source":            "cycle_log",
                "vol_regime":        c.get("vol_regime"),
                "price_regime":      c.get("price_regime"),
                "positioning_regime":c.get("positioning_regime"),
                "final_regime":      c.get("final_regime"),
                "confidence_level":  c.get("confidence_level"),
                "confidence_score":  c.get("confidence_score"),
                "size_multiplier":   c.get("size_multiplier"),
                "block_new_entries": c.get("block_new_entries"),
                "adx_15":            c.get("adx_15"),
                "ema_structure":     c.get("ema_structure"),
                "vrp_smoothed":      c.get("vrp_smoothed"),
                "iv_behavior":       c.get("iv_behavior"),
                "day_move_used_pct": c.get("day_move_used_pct"),
                "calibration_tier":  None,
                "notes":             c.get("no_trade_reason"),
            })
    timeline.sort(key=lambda x: str(x.get("time", "")))
    return timeline


def compute_calibration_summary(calibration_row: Optional[dict]) -> dict:
    """Build calibration summary from calibration_state row."""
    if not calibration_row:
        return {"status": "No calibration data found", "calibration_tier": 0}
    return {
        "calibration_tier":           calibration_row.get("calibration_tier", 0),
        "is_valid":                   bool(calibration_row.get("is_valid")),
        "n_trading_days":             calibration_row.get("n_trading_days"),
        "n_tuesday_expiries":         calibration_row.get("n_tuesday_expiries"),
        "calibrated_at":              calibration_row.get("calibrated_at"),
        "vrp_sell_threshold":         calibration_row.get("vrp_sell_threshold"),
        "vrp_fair_threshold":         calibration_row.get("vrp_fair_threshold"),
        "vix_p25":                    calibration_row.get("vix_p25"),
        "vix_p50":                    calibration_row.get("vix_p50"),
        "vix_p75":                    calibration_row.get("vix_p75"),
        "vix_p90":                    calibration_row.get("vix_p90"),
        "pcr_bullish_threshold":      calibration_row.get("pcr_bullish_threshold"),
        "pcr_bearish_threshold":      calibration_row.get("pcr_bearish_threshold"),
        "skew_bearish_threshold":     calibration_row.get("skew_bearish_threshold"),
        "oi_buildup_threshold":       calibration_row.get("oi_buildup_threshold"),
        "oi_wall_strong_cal":         calibration_row.get("oi_wall_strong_cal"),
        "straddle_ratio_sell":        calibration_row.get("straddle_ratio_sell"),
        "day_size_tuesday":           calibration_row.get("day_size_tuesday"),
        "day_size_monday":            calibration_row.get("day_size_monday"),
        "day_size_wednesday":         calibration_row.get("day_size_wednesday"),
        "day_size_thursday":          calibration_row.get("day_size_thursday"),
        "day_size_friday":            calibration_row.get("day_size_friday"),
        "signal_weight_vrp":          calibration_row.get("signal_weight_vrp"),
        "signal_weight_price":        calibration_row.get("signal_weight_price"),
        "signal_weight_positioning":  calibration_row.get("signal_weight_positioning"),
        "phantom_false_negative_rate":calibration_row.get("phantom_false_negative_rate"),
        "exit_quality_score":         calibration_row.get("exit_quality_score"),
        "regime_accuracy_score":      calibration_row.get("regime_accuracy_score"),
        "notes":                      calibration_row.get("notes"),
        "tier_description": {
            0: "Tier 0 — NIFTY 2026 defaults (< 5 trading days)",
            1: "Tier 1 — VIX percentiles from live data (5-19 days)",
            2: "Tier 2 — Full calibration (20-59 days)",
            3: "Tier 3 — Robust calibration with signal weights (60+ days)",
        }.get(calibration_row.get("calibration_tier", 0), "Unknown"),
    }


def aggregate_no_trade_reasons(decisions: List[dict]) -> dict:
    """Count no-trade reasons from strategy decisions."""
    counts: dict = {}
    for d in decisions:
        if d.get("action") == "NO_TRADE":
            r = d.get("reason") or "unknown"
            counts[r] = counts.get(r, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def compute_slippage_analysis(
    trade_entries: List[dict],
    trade_exits:   List[dict],
    position_legs: List[dict],
) -> dict:
    """Compute slippage analysis from position legs."""
    if not trade_entries:
        return {}
    n = len(trade_entries)

    # Compute actual slippage from quoted mid vs fill price
    actual_slips = []
    for leg in position_legs:
        qme = float(leg.get("quoted_mid_at_entry") or 0)
        ep  = float(leg.get("entry_price") or 0)
        qmx = float(leg.get("quoted_mid_at_exit") or 0)
        xp  = float(leg.get("exit_price") or 0)
        if qme > 0 and ep > 0:
            actual_slips.append(abs(ep - qme))
        if qmx > 0 and xp > 0:
            actual_slips.append(abs(xp - qmx))

    total_costs = sum(float(e.get("total_costs_rupees") or 0) for e in trade_exits)

    # v7: "per lot" has to mean per LOT TRADED. The old expression divided by
    # (number of trades x lot size), which is neither: it halved on every
    # 2-lot trade, so the metric moved with position size instead of with
    # cost, and it was computed on a 75-unit lot the engine has not traded
    # since January 2026. Lots are summed over the entries that actually have
    # an exit, because total_costs above is summed over the exits.
    exited_ids = {
        str(e.get("trade_id") or e.get("position_id") or "") for e in trade_exits
    }
    total_lots = 0
    for entry in trade_entries:
        key = str(entry.get("trade_id") or entry.get("position_id") or "")
        if exited_ids and key not in exited_ids:
            continue
        try:
            total_lots += int(float(entry.get("final_lots") or 0))
        except (TypeError, ValueError):
            pass
    if total_lots <= 0:
        total_lots = n
    total_units = total_lots * LOT_SIZE

    return {
        "total_trades":                n,
        "total_lots_traded":           total_lots,
        "total_actual_costs_rupees":   round(total_costs, 2),
        "avg_actual_costs_rupees":     round(total_costs / n, 2) if n else 0,
        "cost_per_lot_rupees":         round(total_costs / total_lots, 2)
                                       if total_lots else 0,
        "cost_per_unit_rupees":        round(total_costs / total_units, 2)
                                       if total_units else 0,
        "avg_actual_slippage_pts":     round(statistics.mean(actual_slips), 3)
                                       if actual_slips else None,
        "max_actual_slippage_pts":     round(max(actual_slips), 3)
                                       if actual_slips else None,
        "total_slippage_observations": len(actual_slips),
    }


def compute_iv_crush_per_trade(
    entry:      dict,
    exit_row:   Optional[dict],
    cycle_rows: List[dict],
) -> dict:
    """Compute IV crush for a single trade."""
    if not entry or not exit_row:
        return {}
    entry_iv = entry.get("entry_atm_iv") or entry.get("entry_vrp")
    if not entry_iv or float(entry_iv) <= 0:
        return {}
    exit_time = str(exit_row.get("exit_time", ""))
    exit_cycles = [
        c for c in cycle_rows
        if str(c.get("cycle_time", "")) >= exit_time
    ]
    if not exit_cycles:
        return {}
    exit_iv_raw = (
        exit_cycles[0].get("atm_iv_pct") or
        exit_cycles[0].get("vrp_smoothed")
    )
    if not exit_iv_raw:
        return {}
    entry_iv_f = float(entry_iv)
    exit_iv_f  = float(exit_iv_raw)
    crush = (entry_iv_f - exit_iv_f) / entry_iv_f * 100.0 if entry_iv_f > 0 else 0.0
    return {
        "entry_iv":     round(entry_iv_f, 4),
        "exit_iv":      round(exit_iv_f, 4),
        "iv_crush_pct": round(crush, 2),
        "direction":    "CRUSH" if crush > 5 else ("EXPAND" if crush < -5 else "STABLE"),
    }


def compute_exit_quality_summary(exit_quality_rows: List[dict]) -> dict:
    """Summarise exit quality for the day."""
    if not exit_quality_rows:
        return {"total_exits": 0}
    total     = len(exit_quality_rows)
    premature = sum(1 for r in exit_quality_rows if r.get("was_exit_premature"))
    late      = sum(1 for r in exit_quality_rows if r.get("was_exit_late"))

    improvements = [
        float(r["pnl_15min_after_exit"]) - float(r["exit_pnl_rupees"])
        for r in exit_quality_rows
        if r.get("pnl_15min_after_exit") is not None
        and r.get("exit_pnl_rupees") is not None
    ]

    priority_names = {
        1: "DELTA_BREACH", 2: "SPOT_PROXIMITY", 3: "PRICE_STOP",
        4: "PROFIT_LOCK",  5: "CHEAP_BUYBACK",  6: "TIME_TARGET",
        7: "HARD_EXIT",
    }

    return {
        "total_exits":            total,
        "premature_exits":        premature,
        "late_exits":             late,
        "premature_rate_pct":     round(premature / total * 100, 1) if total else 0,
        "avg_improvement_15min":  round(statistics.mean(improvements), 2)
                                  if improvements else None,
        "exit_quality_score":     round(100 - premature / total * 100, 1) if total else None,
        "by_priority": {
            str(pri): {
                "name":  priority_names.get(pri, f"P{pri}"),
                "count": sum(1 for r in exit_quality_rows
                             if int(r.get("exit_priority_fired") or 0) == pri),
            }
            for pri in range(1, 8)
        },
    }


def compute_phantom_summary(phantom_rows: List[dict]) -> dict:
    """Summarise phantom trades for the day."""
    if not phantom_rows:
        return {"total": 0}
    total     = len(phantom_rows)
    would_win = sum(1 for r in phantom_rows if r.get("would_have_been_profitable"))
    fnr       = round(would_win / total * 100, 1) if total > 0 else 0.0
    avg_credit = None
    credits = [float(r["credit_would_be"]) for r in phantom_rows
               if r.get("credit_would_be")]
    if credits:
        avg_credit = round(statistics.mean(credits), 2)
    return {
        "total":                    total,
        "would_have_won":           would_win,
        "false_negative_rate_pct":  fnr,
        "avg_credit_would_be":      avg_credit,
        "interpretation": (
            "Threshold too tight — lower VRP sell threshold"
            if fnr > 30 else (
                "Threshold may be too low"
                if fnr < 10 and total >= 5 else
                "Threshold appropriate"
            )
        ),
    }


def compute_regime_accuracy_summary(accuracy_rows: List[dict]) -> dict:
    """Summarise regime accuracy for the day."""
    if not accuracy_rows:
        return {"total": 0}
    total  = len(accuracy_rows)
    scores = [float(r["score_value"]) for r in accuracy_rows
              if r.get("score_value") is not None]
    vol_correct   = sum(1 for r in accuracy_rows if r.get("was_vol_correct") == 1)
    price_correct = sum(1 for r in accuracy_rows if r.get("was_price_correct") == 1)
    final_correct = sum(1 for r in accuracy_rows if r.get("was_final_correct") == 1)
    vol_n   = sum(1 for r in accuracy_rows if r.get("was_vol_correct")   is not None)
    price_n = sum(1 for r in accuracy_rows if r.get("was_price_correct") is not None)
    final_n = sum(1 for r in accuracy_rows if r.get("was_final_correct") is not None)
    return {
        "total_decisions":        total,
        "avg_score":              round(statistics.mean(scores), 3) if scores else None,
        "vol_regime_accuracy":    round(vol_correct / vol_n * 100, 1)   if vol_n   else None,
        "price_regime_accuracy":  round(price_correct / price_n * 100, 1) if price_n else None,
        "final_regime_accuracy":  round(final_correct / final_n * 100, 1) if final_n else None,
    }


def compute_adx_profile(cycle_rows: List[dict]) -> dict:
    """Compute ADX profile from cycle log."""
    adx_vals = [float(c["adx_15"]) for c in cycle_rows if c.get("adx_15")]
    if not adx_vals:
        return {}
    return {
        "adx_open":            round(adx_vals[0], 2),
        "adx_close":           round(adx_vals[-1], 2),
        "adx_mean":            round(statistics.mean(adx_vals), 2),
        "adx_max":             round(max(adx_vals), 2),
        "adx_min":             round(min(adx_vals), 2),
        "trending_cycles":     sum(1 for v in adx_vals if v > 25),
        "strong_trend_cycles": sum(1 for v in adx_vals if v > 35),
        "flat_cycles":         sum(1 for v in adx_vals if v < 20),
    }


def compute_pcr_profile(cycle_rows: List[dict]) -> dict:
    """Compute PCR profile from cycle log."""
    pcrs = [float(c["pcr"]) for c in cycle_rows if c.get("pcr")]
    if not pcrs:
        return {}
    return {
        "pcr_open":                 round(pcrs[0], 3),
        "pcr_close":                round(pcrs[-1], 3),
        "pcr_mean":                 round(statistics.mean(pcrs), 3),
        "pcr_min":                  round(min(pcrs), 3),
        "pcr_max":                  round(max(pcrs), 3),
        "pcr_change_open_to_close": round(pcrs[-1] - pcrs[0], 3),
        "extreme_fear_cycles":      sum(1 for p in pcrs if p > 1.5),
        "extreme_greed_cycles":     sum(1 for p in pcrs if p < 0.7),
        "neutral_cycles":           sum(1 for p in pcrs if 0.8 <= p <= 1.3),
    }


def compute_skew_profile(cycle_rows: List[dict]) -> dict:
    """Compute skew profile from cycle log."""
    skews = [float(c["skew_ratio"]) for c in cycle_rows if c.get("skew_ratio")]
    if not skews:
        return {}
    return {
        "skew_open":              round(skews[0], 3),
        "skew_close":             round(skews[-1], 3),
        "skew_mean":              round(statistics.mean(skews), 3),
        "skew_min":               round(min(skews), 3),
        "skew_max":               round(max(skews), 3),
        "fear_skew_cycles":       sum(1 for s in skews if s > 3.0),
        "complacent_skew_cycles": sum(1 for s in skews if s < 0.95),
    }


def summarize_api_calls(api_rows: List[dict]) -> dict:
    """Summarise API call log."""
    if not api_rows:
        return {"total_calls": 0}
    by_category:    dict  = {}
    errors:         list  = []
    response_times: list  = []
    rate_limited    = 0

    for r in api_rows:
        cat = r.get("category") or "unknown"
        by_category[cat] = by_category.get(cat, 0) + 1
        if r.get("error_message"):
            errors.append(r)
        if r.get("rate_limited"):
            rate_limited += 1
        if r.get("response_time_ms") is not None:
            response_times.append(float(r["response_time_ms"]))

    return {
        "total_calls":        len(api_rows),
        "by_category":        by_category,
        "error_count":        len(errors),
        "rate_limited_count": rate_limited,
        "avg_response_ms":    round(statistics.mean(response_times), 1)
                              if response_times else None,
        "p95_response_ms":    round(
            sorted(response_times)[int(len(response_times) * 0.95)], 1
        ) if len(response_times) > 5 else None,
        "max_response_ms":    round(max(response_times), 1) if response_times else None,
        "slow_calls_over_2s": sum(1 for t in response_times if t > 2000),
        "errors_sample":      errors[:20],
    }


def summarize_option_chain(chain_rows: List[dict]) -> dict:
    """Summarise option chain snapshot."""
    if not chain_rows:
        return {"total_rows": 0}
    capture_times = sorted(set(r["capture_time"] for r in chain_rows
                               if r.get("capture_time")))
    strikes       = sorted(set(float(r["strike"]) for r in chain_rows
                               if r.get("strike") is not None))
    zero_bid_ask  = sum(1 for r in chain_rows
                        if (r.get("bid") or 0) == 0 and (r.get("ask") or 0) == 0)
    spreads = [
        float(r["ask"]) - float(r["bid"])
        for r in chain_rows
        if (r.get("bid") or 0) > 0 and (r.get("ask") or 0) > 0
    ]
    ivs = [float(r["iv"]) for r in chain_rows if r.get("iv") and float(r["iv"]) > 0]
    call_ois = [int(r["oi"]) for r in chain_rows
                if r.get("option_type") == "call" and r.get("oi")]
    put_ois  = [int(r["oi"]) for r in chain_rows
                if r.get("option_type") == "put"  and r.get("oi")]
    total_call_oi = sum(call_ois)
    total_put_oi  = sum(put_ois)
    return {
        "total_rows":           len(chain_rows),
        "unique_capture_times": len(capture_times),
        "unique_strikes":       len(strikes),
        "strike_range":         [min(strikes), max(strikes)] if strikes else None,
        "zero_bid_ask_count":   zero_bid_ask,
        "zero_bid_ask_pct":     round(zero_bid_ask / len(chain_rows) * 100, 1)
                                if chain_rows else 0,
        "avg_spread":           round(statistics.mean(spreads), 3) if spreads else None,
        "max_spread":           round(max(spreads), 3) if spreads else None,
        "avg_iv_pct":           round(statistics.mean(ivs) * 100, 2)
                                if ivs and ivs[0] < 2.0 else
                                round(statistics.mean(ivs), 2) if ivs else None,
        "total_call_oi":        total_call_oi,
        "total_put_oi":         total_put_oi,
        "chain_pcr":            round(total_put_oi / total_call_oi, 3)
                                if total_call_oi > 0 else None,
        "first_capture":        capture_times[0]  if capture_times else None,
        "last_capture":         capture_times[-1] if capture_times else None,
    }


def detect_anomalies(
    session_state:       Optional[dict],
    cycle_rows:          List[dict],
    api_summary:         dict,
    daily_summary:       Optional[dict],
    decisions:           List[dict],
    trade_exits:         List[dict],
    vrp_stats:           dict,
    spot_profile:        dict,
    regime_decisions:    List[dict],
    calibration_summary: dict,
    vix_profile:         dict,
    or_analysis:         dict,
    phantom_summary:     dict,
    exit_quality_sum:    dict,
    regime_accuracy_sum: dict,
) -> List[str]:
    """Detect anomalies and generate flags for the EOD report."""
    flags: List[str] = []

    # Session state flags
    if session_state:
        if session_state.get("daily_halted"):
            flags.append(
                f"[FLAG] Daily trading halted: "
                f"{session_state.get('last_stop_reason')}"
            )
        if session_state.get("circuit_breaker_suspected"):
            flags.append("[FLAG] Circuit breaker suspected today")
        if session_state.get("vix_spike_detected"):
            flags.append("[FLAG] VIX spike detected today")
        if not session_state.get("or_computed"):
            flags.append("[FLAG] Opening range never computed")
        if not session_state.get("session_initialized"):
            flags.append("[FLAG] Session never initialized — opening IV baseline missing")

    # Cycle data quality
    if cycle_rows:
        missing_vrp  = sum(1 for c in cycle_rows
                           if c.get("vrp_smoothed") is None and c.get("vrp_raw") is None)
        missing_spot = sum(1 for c in cycle_rows if c.get("spot") is None)
        unknown_vol  = sum(1 for c in cycle_rows if c.get("vol_regime") == "UNKNOWN")
        no_regime    = sum(1 for c in cycle_rows if not c.get("final_regime"))
        stale_chain  = sum(1 for c in cycle_rows if c.get("chain_stale"))

        if missing_vrp > len(cycle_rows) * 0.3:
            flags.append(
                f"[FLAG] {missing_vrp}/{len(cycle_rows)} cycles had missing VRP data"
            )
        if missing_spot > 0:
            flags.append(f"[FLAG] {missing_spot} cycles had missing spot price")
        if unknown_vol > 3:
            flags.append(
                f"[FLAG] {unknown_vol} cycles had vol_regime=UNKNOWN"
            )
        if no_regime > len(cycle_rows) * 0.5:
            flags.append(
                f"[FLAG] {no_regime}/{len(cycle_rows)} cycles had no final_regime"
            )
        if stale_chain > len(cycle_rows) * 0.2:
            flags.append(
                f"[FLAG] {stale_chain}/{len(cycle_rows)} cycles had stale chain data"
            )

    # API health
    if api_summary.get("error_count", 0) > 5:
        flags.append(f"[FLAG] {api_summary['error_count']} API errors today")
    if api_summary.get("rate_limited_count", 0) > 0:
        flags.append(
            f"[FLAG] {api_summary['rate_limited_count']} API calls rate-limited"
        )

    # Stop losses
    if daily_summary and (daily_summary.get("stops_fired") or 0) >= 2:
        flags.append(
            f"[FLAG] {daily_summary['stops_fired']} stop-losses fired today"
        )

    # VRP negative
    if vrp_stats.get("vrp_negative_cycles", 0) > 0 and vrp_stats.get("total_vrp_cycles", 0) > 0:
        neg_pct = vrp_stats["vrp_negative_cycles"] / vrp_stats["total_vrp_cycles"] * 100
        if neg_pct > 30:
            flags.append(
                f"[FLAG] VRP negative in {neg_pct:.0f}% of cycles — "
                f"IV below realized vol"
            )

    # Large intraday move
    if spot_profile.get("range_pct", 0) > 1.5:
        flags.append(
            f"[FLAG] NIFTY moved {spot_profile.get('range_pct', 0):.2f}% intraday "
            f"— high-move day"
        )
    if spot_profile.get("range_pct", 0) < 0.3:
        flags.append(
            f"[FLAG] NIFTY moved only {spot_profile.get('range_pct', 0):.2f}% "
            f"— very low move day"
        )

    # VIX range
    if vix_profile.get("vix_range", 0) > 5:
        flags.append(
            f"[FLAG] VIX range today: {vix_profile.get('vix_range')} "
            f"— high volatility day"
        )

    # Wide OR
    if or_analysis.get("or_computed") and or_analysis.get("or_width_pct", 0) > 0.75:
        flags.append(
            f"[FLAG] Very wide OR: {or_analysis.get('or_width_pct')}% "
            f"— dangerous for premium selling"
        )

    # No-trade reasons
    no_trade_reasons = aggregate_no_trade_reasons(decisions)
    _ok_reasons = {
        "position_already_open_single_position_engine",
        "max_concurrent_positions_reached",
    }
    if no_trade_reasons:
        top_reason, top_count = next(iter(no_trade_reasons.items()))
        total_decisions = len(decisions)
        if total_decisions and top_count > total_decisions * 0.5:
            if top_reason not in _ok_reasons:
                flags.append(
                    f"[FLAG] Dominant no-trade reason: '{top_reason}' "
                    f"{top_count}/{total_decisions} "
                    f"({top_count/total_decisions*100:.0f}%)"
                )
            else:
                flags.append(
                    f"[OK] Single position engine: '{top_reason}' "
                    f"{top_count}/{total_decisions} — expected"
                )

    # Stop exits
    stop_exits = [e for e in trade_exits if e.get("exit_reason") == "CLOSE_STOP"]
    if len(stop_exits) >= 2:
        flags.append(f"[FLAG] {len(stop_exits)} stop-loss exits today")

    # Regime decisions
    if not regime_decisions:
        flags.append("[FLAG] No regime_decisions rows — regime engine not persisting")
    else:
        abort_decisions = [r for r in regime_decisions
                           if r.get("final_regime") == "ABORT"]
        if abort_decisions:
            flags.append(
                f"[FLAG] {len(abort_decisions)} ABORT regime decisions today"
            )
        blocked = [r for r in regime_decisions if r.get("block_new_entries")]
        if blocked:
            flags.append(
                f"[FLAG] {len(blocked)} cycles had block_new_entries=True"
            )

    # Calibration
    cal_tier  = calibration_summary.get("calibration_tier", 0)
    cal_valid = calibration_summary.get("is_valid", False)
    if not cal_valid:
        flags.append(
            f"[INFO] Calibration tier={cal_tier} — using defaults. "
            f"Need 20 trading days for Tier 2."
        )
    else:
        flags.append(
            f"[OK] Calibration tier={cal_tier} VALID — thresholds data-derived"
        )

    # Phantom trade analysis
    if phantom_summary.get("total", 0) > 0:
        fnr = phantom_summary.get("false_negative_rate_pct", 0)
        if fnr > 30:
            flags.append(
                f"[FLAG] Phantom FNR={fnr:.1f}% — VRP threshold too tight, "
                f"good trades being blocked"
            )
        elif fnr < 10 and phantom_summary.get("total", 0) >= 5:
            flags.append(
                f"[OK] Phantom FNR={fnr:.1f}% — VRP threshold appropriate"
            )

    # Exit quality
    if exit_quality_sum.get("total_exits", 0) > 0:
        avg_imp = exit_quality_sum.get("avg_improvement_15min")
        if avg_imp is not None and avg_imp > 500:
            flags.append(
                f"[FLAG] Exits Rs{avg_imp:.0f} too early on average "
                f"— time targets may need adjustment"
            )

    # Regime accuracy
    if regime_accuracy_sum.get("total_decisions", 0) > 0:
        avg_score = regime_accuracy_sum.get("avg_score")
        if avg_score is not None and avg_score < 0.50:
            flags.append(
                f"[FLAG] Regime accuracy score={avg_score:.3f} — "
                f"regime engine needs calibration"
            )

    if not flags:
        flags.append("[OK] No major anomalies detected")

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# MARKDOWN HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def md_kv(d: Optional[dict]) -> str:
    """Render a dict as a Markdown key-value list."""
    if not d:
        return "_(none)_\n"
    lines = []
    for k, v in d.items():
        if v is None:
            v = "N/A"
        if isinstance(v, float):
            v = f"{v:.4f}"
        elif isinstance(v, dict):
            v = json.dumps(v, default=str)
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines) + "\n"


def md_table(
    rows:     List[dict],
    columns:  List[str],
    max_rows: int = 80,
) -> str:
    """Render a list of dicts as a Markdown table."""
    if not rows:
        return "_(no data)_\n"
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for r in rows[:max_rows]:
        vals = []
        for c in columns:
            v = r.get(c, "") if isinstance(r, dict) else ""
            if v is None:
                v = ""
            if isinstance(v, float):
                v = f"{v:.4f}"
            vals.append(
                str(v).replace("|", "\\|").replace("\n", " ")[:120]
            )
        lines.append("| " + " | ".join(vals) + " |")
    out = "\n".join(lines) + "\n"
    if len(rows) > max_rows:
        out += f"\n_... {len(rows) - max_rows} more rows omitted._\n"
    return out


def filter_log_lines_by_level(lines: List[str], levels: set) -> List[str]:
    """Filter log lines by level (WARNING, ERROR, CRITICAL)."""
    out = []
    for line in lines:
        parts = line.split("|")
        if len(parts) >= 2 and parts[1].strip() in levels:
            out.append(line)
    return out


def build_master_timeline(
    cycle_rows:       List[dict],
    decisions:        List[dict],
    trade_entries:    List[dict],
    trade_exits:      List[dict],
    regime_decisions: List[dict],
    audit_lines:      List[str],
) -> List[tuple]:
    """Build unified master timeline from all event sources."""
    events: List[tuple] = []

    for c in cycle_rows:
        events.append((
            str(c.get("cycle_time") or ""),
            "CYCLE",
            (
                f"spot={c.get('spot')} vix={c.get('vix')} "
                f"vrp_s={c.get('vrp_smoothed')} "
                f"vol={c.get('vol_regime')} price={c.get('price_regime')} "
                f"pos={c.get('positioning_regime')} "
                f"final={c.get('final_regime')} conf={c.get('confidence_level')} "
                f"size={c.get('size_multiplier')} "
                f"iv_beh={c.get('iv_behavior')} "
                f"day_move={c.get('day_move_used_pct')} "
                f"action={c.get('action_taken')} pnl={c.get('daily_pnl_net')}"
            ),
        ))

    for r in regime_decisions:
        events.append((
            str(r.get("timestamp") or ""),
            "REGIME",
            (
                f"{r.get('final_regime')} conf={r.get('confidence_level')} "
                f"({r.get('confidence_score')}) "
                f"size={r.get('size_multiplier')} "
                f"vol={r.get('vol_regime')} price={r.get('price_regime')} "
                f"pos={r.get('positioning_regime')} "
                f"block={r.get('block_new_entries')} "
                f"tier={r.get('calibration_tier')} notes={r.get('notes')}"
            ),
        ))

    for d in decisions:
        events.append((
            str(d.get("decision_time") or ""),
            "DECISION",
            f"{d.get('action')} {d.get('strategy_name') or ''} — {d.get('reason')}",
        ))

    for t in trade_entries:
        events.append((
            str(t.get("entry_time") or ""),
            "ENTRY",
            (
                f"{t.get('strategy_name')} lots={t.get('final_lots')} "
                f"credit={t.get('entry_credit')} vrp={t.get('entry_vrp_smoothed')} "
                f"vol={t.get('vol_regime_at_entry')} price={t.get('price_regime_at_entry')} "
                f"conf={t.get('confidence_level_at_entry')} "
                f"dte={t.get('actual_dte')} borderline={t.get('is_borderline_sell')}"
            ),
        ))

    for e in trade_exits:
        events.append((
            str(e.get("exit_time") or ""),
            "EXIT",
            (
                f"{e.get('strategy_name')} reason={e.get('exit_reason')} "
                f"priority={e.get('exit_priority')} ({e.get('exit_priority_name')}) "
                f"hold={e.get('hold_minutes')}min "
                f"net_pnl={e.get('net_pnl_rupees')} result={e.get('result')}"
            ),
        ))

    for line in filter_log_lines_by_level(audit_lines, {"WARNING", "ERROR", "CRITICAL"}):
        parts = line.split("|")
        ts    = parts[0].strip() if parts else ""
        msg   = "|".join(parts[2:]).strip() if len(parts) > 2 else line
        level = parts[1].strip() if len(parts) > 1 else "LOG"
        events.append((ts, f"LOG:{level}", msg))

    events.sort(key=lambda x: x[0])
    return events


# ─────────────────────────────────────────────────────────────────────────────
# MAIN REPORT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(target_date: str) -> None:
    """
    Generate comprehensive EOD forensic report for target_date.
    Writes Markdown report and JSON raw data exports.
    """
    # Validate date format
    datetime.strptime(target_date, "%Y-%m-%d")

    conn = get_connection(DB_PATH)

    # ── Fetch all data ────────────────────────────────────────────────────
    session_state      = fetch_session_state(conn, target_date)
    cycle_rows         = fetch_cycle_log(conn, target_date)
    decisions          = fetch_strategy_decisions(conn, target_date)
    positions          = fetch_positions(conn, target_date)
    position_ids       = [p["position_id"] for p in positions]
    legs               = fetch_position_legs(conn, position_ids)
    trade_entries      = fetch_trade_entries(conn, target_date)
    trade_exits        = fetch_trade_exits(conn, target_date)
    daily_summary      = fetch_daily_summary(conn, target_date)
    chain_rows         = fetch_option_chain_snapshot(conn, target_date)
    api_rows           = fetch_api_call_log(conn, target_date)
    audit_db_rows      = fetch_audit_log_db(conn, target_date)
    audit_file_lines   = fetch_audit_log_file_lines(LOG_DIR, target_date)
    prior_days_summary = fetch_prior_days_summary(conn, target_date, n=10)
    intraday_candles   = fetch_intraday_candles(conn, target_date)
    cumulative_days    = fetch_cumulative_performance(conn, target_date, lookback_days=90)
    regime_decisions   = fetch_regime_decisions(conn, target_date)
    calibration_row    = fetch_calibration_state(conn)
    calibration_drift  = fetch_calibration_drift(conn)
    vix_history_today  = fetch_vix_history_today(conn, target_date)
    market_snaps_today = fetch_market_snapshots_today(conn, target_date)
    phantom_rows       = fetch_phantom_trades_today(conn, target_date)
    exit_quality_rows  = fetch_exit_quality_today(conn, target_date)
    regime_accuracy_rows = fetch_regime_accuracy_today(conn, target_date)

    conn.close()

    # ── Build lookups ─────────────────────────────────────────────────────
    exits_by_position: Dict[str, dict] = {e["position_id"]: e for e in trade_exits}
    legs_by_position:  Dict[str, list] = {}
    for leg in legs:
        legs_by_position.setdefault(leg["position_id"], []).append(leg)

    # ── Compute all metrics ───────────────────────────────────────────────
    no_trade_reasons    = aggregate_no_trade_reasons(decisions)
    strategies_used     = {}
    for t in trade_entries:
        strategies_used[t.get("strategy_name", "UNKNOWN")] = \
            strategies_used.get(t.get("strategy_name", "UNKNOWN"), 0) + 1

    chain_summary       = summarize_option_chain(chain_rows)
    api_summary         = summarize_api_calls(api_rows)
    vrp_stats           = compute_vrp_statistics(cycle_rows)
    spot_profile        = compute_intraday_spot_profile(cycle_rows)
    adx_profile         = compute_adx_profile(cycle_rows)
    pcr_profile         = compute_pcr_profile(cycle_rows)
    skew_profile        = compute_skew_profile(cycle_rows)
    vrp_curve           = compute_vrp_curve(cycle_rows)
    candle_stats        = compute_candle_statistics(intraday_candles)
    equity_curve        = compute_equity_curve(cumulative_days)
    regime_timeline     = compute_regime_timeline(regime_decisions, cycle_rows)
    regime_dist         = compute_regime_distribution(regime_decisions, cycle_rows)
    calibration_summary = compute_calibration_summary(calibration_row)
    vix_profile         = compute_vix_profile(vix_history_today)
    or_analysis         = compute_or_analysis(session_state, cycle_rows, trade_entries)
    slippage_analysis   = compute_slippage_analysis(trade_entries, trade_exits, legs)
    phantom_summary     = compute_phantom_summary(phantom_rows)
    exit_quality_sum    = compute_exit_quality_summary(exit_quality_rows)
    regime_accuracy_sum = compute_regime_accuracy_summary(regime_accuracy_rows)

    # P&L summary
    net_pnl     = round(sum(float(e.get("net_pnl_rupees") or 0) for e in trade_exits), 2)
    gross_pnl   = round(sum(float(e.get("gross_pnl_rupees") or 0) for e in trade_exits), 2)
    total_costs = round(sum(float(e.get("total_costs_rupees") or 0) for e in trade_exits), 2)
    wins        = sum(1 for e in trade_exits if e.get("result") == "WIN")
    losses      = sum(1 for e in trade_exits if e.get("result") == "LOSS")

    # IV crush per trade
    iv_crush_by_pos: Dict[str, dict] = {}
    for t in trade_entries:
        pid = t.get("position_id")
        iv_crush_by_pos[pid] = compute_iv_crush_per_trade(
            t, exits_by_position.get(pid), cycle_rows
        )

    # Anomaly detection
    anomalies = detect_anomalies(
        session_state, cycle_rows, api_summary, daily_summary,
        decisions, trade_exits, vrp_stats, spot_profile,
        regime_decisions, calibration_summary, vix_profile, or_analysis,
        phantom_summary, exit_quality_sum, regime_accuracy_sum,
    )

    # Master timeline
    timeline = build_master_timeline(
        cycle_rows, decisions, trade_entries, trade_exits,
        regime_decisions, audit_file_lines,
    )
    warning_error_lines = filter_log_lines_by_level(
        audit_file_lines, {"WARNING", "ERROR", "CRITICAL"}
    )

    # ── Build Markdown report ─────────────────────────────────────────────
    md: List[str] = []

    md.append("# NIFTY Intraday Options Engine v3.0 — EOD Forensic Report")
    md.append(
        f"**Target Date:** {target_date}  |  "
        f"**Generated:** {datetime.now().isoformat()}\n"
    )

    md.append("## 0. Engine Architecture Note\n")
    md.append(
        "Regime-based v3.0 engine. Four-dimensional regime classification: "
        "volatility (VRP-based), price (ADX+EMA+ORB), positioning (OI+PCR+skew), "
        "confidence (weighted signal agreement). "
        "Self-calibrating thresholds (Tier 0-3). "
        "7-priority exit system. "
        "Phantom trade tracking for VRP threshold feedback. "
        "Exit quality logging for time target feedback.\n"
    )

    md.append("## 1. Table of Contents\n")
    md.append(
        "2. Executive Summary | 3. Anomalies | 4. Calibration | "
        "5. Session Config | 6. NIFTY Profile | 7. VRP Deep Dive | "
        "8. VRP Curve | 9. ADX Profile | 10. PCR/Skew | 11. OR Analysis | "
        "12. VIX Profile | 13. Regime Timeline | 14. Regime Distribution | "
        "15. Regime Accuracy | 16. Market Data Timeline | 17. Gate Analysis | "
        "18. Strategy Decisions | 19. No-Trade Reasons | 20. Trade Deep Dive | "
        "21. Exit Quality | 22. IV Crush | 23. Slippage | 24. Phantom Trades | "
        "25. P&L Curve | 26. Option Chain | 27. Candle Stats | 28. API Health | "
        "29. Data Quality | 30. Calibration Drift | 31. Cumulative Performance | "
        "32. Prior Days | 33. Audit Warnings | 34. Master Timeline | "
        "35. Daily Summary | 36. LLM Context | 37. Raw Export\n"
    )

    # ── Section 2: Executive Summary ─────────────────────────────────────
    md.append("## 2. Executive Summary\n")
    exec_summary = {
        "Day label":                   session_state.get("day_label") if session_state else "N/A",
        "Day mode":                    session_state.get("day_mode") if session_state else "N/A",
        "VIX regime":                  session_state.get("vix_regime") if session_state else "N/A",
        "OR condition / width":        f"{session_state.get('or_condition')} / "
                                       f"{session_state.get('or_width')}"
                                       if session_state else "N/A",
        "Calibration tier":            calibration_summary.get("calibration_tier"),
        "Calibration valid":           calibration_summary.get("is_valid"),
        "Trades attempted":            len(decisions),
        "Trades executed":             len(trade_entries),
        "Trades closed":               len(trade_exits),
        "Wins / Losses":               f"{wins} / {losses}",
        "Gross P&L (Rs)":              gross_pnl,
        "Total Costs (Rs)":            total_costs,
        "Net P&L (Rs)":                net_pnl,
        "Net P&L pct capital":         f"{round(net_pnl / STARTING_CAPITAL * 100, 3)}%",
        "Daily halted":                session_state.get("daily_halted") if session_state else "N/A",
        "Consecutive stops":           session_state.get("consecutive_stops") if session_state else "N/A",
        "Cycles logged":               len(cycle_rows),
        "Regime decisions":            len(regime_decisions),
        "NIFTY range":                 f"{spot_profile.get('range_pts')}pts "
                                       f"({spot_profile.get('range_pct')}%)"
                                       if spot_profile else "N/A",
        "VRP smoothed mean (pp)":      vrp_stats.get("vrp_mean"),
        "ATM IV open/close":           f"{vrp_stats.get('atm_iv_open_pct')} / "
                                       f"{vrp_stats.get('atm_iv_close_pct')}",
        "IV crush today (pp)":         vrp_stats.get("iv_crush_pct"),
        "VIX open/close":              f"{vix_profile.get('vix_open')} / "
                                       f"{vix_profile.get('vix_close')}",
        "VIX readings today":          len(vix_history_today),
        "Market snapshots today":      len(market_snaps_today),
        "Option chain snapshot rows":  len(chain_rows),
        "API calls made":              len(api_rows),
        "Audit log lines":             len(audit_file_lines),
        "Phantom trades blocked":      phantom_summary.get("total", 0),
        "Phantom FNR":                 f"{phantom_summary.get('false_negative_rate_pct', 0)}%",
        "Exit quality score":          exit_quality_sum.get("exit_quality_score"),
        "Regime accuracy score":       regime_accuracy_sum.get("avg_score"),
    }
    md.append(md_kv(exec_summary))

    # ── Section 3: Anomalies ──────────────────────────────────────────────
    md.append("## 3. Auto-Detected Anomalies\n")
    md.append("\n".join(f"- {f}" for f in anomalies) + "\n")

    # ── Section 4: Calibration ────────────────────────────────────────────
    md.append("## 4. Calibration Status\n")
    md.append(md_kv(calibration_summary))

    # ── Section 5: Session Configuration ─────────────────────────────────
    md.append("## 5. Session Configuration\n")
    md.append(md_kv(session_state) if session_state else "_No session_state row found._\n")

    # ── Section 6: NIFTY Profile ──────────────────────────────────────────
    md.append("## 6. NIFTY Intraday Profile\n")
    md.append(md_kv(spot_profile))

    # ── Section 7: VRP Deep Dive ──────────────────────────────────────────
    md.append("## 7. VRP / Volatility Deep Dive\n")
    md.append(md_kv(vrp_stats))
    md.append(
        "\n**VRP thresholds (NIFTY 2026 calibrated):** "
        "STRONG_SELL > 3.75pp. SELL 2.5-3.75pp. "
        "NEUTRAL 1.5-2.5pp (NO TRADE). CHEAP < 0. "
        "DTE0 threshold = sell_threshold × 0.75. "
        "Parkinson RV uses 375 bars/day annualization.\n"
    )

    # ── Section 8: VRP Curve ──────────────────────────────────────────────
    md.append("## 8. Intraday VRP Curve\n")
    if vrp_curve:
        md.append(md_table(vrp_curve, [
            "time", "vrp_smoothed", "vrp_raw", "atm_iv_pct",
            "parkinson_rv_pct", "vol_regime", "iv_behavior", "day_move_used_pct"
        ], max_rows=100))
    else:
        md.append("_No VRP curve data._\n")

    # ── Section 9: ADX Profile ────────────────────────────────────────────
    md.append("## 9. ADX / Trend Profile\n")
    md.append(md_kv(adx_profile))

    # ── Section 10: PCR / Skew ────────────────────────────────────────────
    md.append("## 10. PCR / Skew / Directional Profile\n")
    md.append(md_kv(pcr_profile))
    md.append(md_kv(skew_profile))

    # ── Section 11: OR Analysis ───────────────────────────────────────────
    md.append("## 11. Opening Range Analysis\n")
    md.append(md_kv(or_analysis))

    # ── Section 12: VIX Profile ───────────────────────────────────────────
    md.append("## 12. VIX Intraday Profile\n")
    md.append(md_kv(vix_profile))

    # ── Section 13: Regime Timeline ───────────────────────────────────────
    md.append("## 13. Regime Engine Timeline\n")
    if regime_timeline:
        md.append(md_table(regime_timeline, [
            "time", "source", "vol_regime", "price_regime",
            "positioning_regime", "final_regime", "confidence_level",
            "confidence_score", "size_multiplier", "block_new_entries",
            "adx_15", "ema_structure", "vrp_smoothed", "iv_behavior",
            "day_move_used_pct", "calibration_tier", "notes",
        ], max_rows=100))
    else:
        md.append("_No regime_decisions rows found._\n")

    # ── Section 14: Regime Distribution ──────────────────────────────────
    md.append("## 14. Regime Distribution Today\n")
    for regime, count in regime_dist.items():
        md.append(f"- **{regime}**: {count} decisions\n")

    # ── Section 15: Regime Accuracy ───────────────────────────────────────
    md.append("## 15. Regime Accuracy Scoring\n")
    md.append(md_kv(regime_accuracy_sum))
    if regime_accuracy_rows:
        md.append("\n**Regime accuracy details:**\n")
        md.append(md_table(regime_accuracy_rows, [
            "vol_regime_classified", "price_regime_classified",
            "final_regime_classified", "was_vol_correct", "was_price_correct",
            "was_final_correct", "nifty_move_2hr_pts", "score_value",
        ], max_rows=20))

    # ── Section 16: Market Data Timeline ─────────────────────────────────
    md.append("## 16. Market Data Timeline (cycle_log)\n")
    md.append(f"Total cycles: {len(cycle_rows)}.\n")
    md.append(md_table(cycle_rows, [
        "cycle_time", "spot", "vix", "vrp_smoothed", "vrp_raw",
        "atm_iv_pct", "parkinson_rv_pct", "adx_15", "adx_condition",
        "vwap_dist_pct", "pcr", "skew_ratio", "or_condition",
        "iv_behavior", "day_move_used_pct", "vol_regime", "price_regime",
        "positioning_regime", "final_regime", "confidence_level",
        "size_multiplier", "block_new_entries", "action_taken",
        "no_trade_reason", "open_positions", "daily_pnl_net",
    ], max_rows=100))

    # ── Section 17: Gate Analysis ─────────────────────────────────────────
    md.append("## 17. Gate Blockage Analysis\n")
    gate_categories = {
        "risk_gates":       ["daily_loss_limit", "max_entries", "max_concurrent",
                             "consecutive_stops", "stop_cooldown", "entry_cooldown"],
        "data_gates":       ["vrp_unknown", "opening_range_not", "or_pending",
                             "chain_unavailable", "chain_stale"],
        "market_condition": ["circuit_breaker", "vix_spike", "iv_expanding",
                             "iv_spiking", "day_move_used"],
        "timing_gates":     ["before_entry_window", "past_entry_window",
                             "hard_exit", "only_"],
        "strategy_gates":   ["no_strategy_for_regime", "params_invalid",
                             "strategy_rules_failed", "credit_"],
        "regime_gates":     ["regime_engine", "ABORT", "NO_TRADE: Choppy",
                             "NO_TRADE: Before", "NO_TRADE: Past", "OBSERVING",
                             "confidence_", "dte_"],
        "vol_gates":        ["VOL_NEUTRAL", "VOL_BUY_OPTIONS", "vol_neutral",
                             "neutral", "buy_options"],
    }
    categorized: dict = {cat: 0 for cat in gate_categories}
    categorized["other"] = 0
    for reason, count in no_trade_reasons.items():
        matched = False
        for cat, keywords in gate_categories.items():
            if any(kw.lower() in reason.lower() for kw in keywords):
                categorized[cat] += count
                matched = True
                break
        if not matched:
            categorized["other"] += count

    total_decisions = len(decisions)
    no_trade_count  = sum(1 for d in decisions if d.get("action") == "NO_TRADE")
    md.append(md_kv({
        "total_cycles":      len(cycle_rows),
        "total_decisions":   total_decisions,
        "no_trade_count":    no_trade_count,
        "enter_count":       total_decisions - no_trade_count,
        "no_trade_rate_pct": round(no_trade_count / total_decisions * 100, 1)
                             if total_decisions > 0 else 0,
        "gate_category_counts": categorized,
    }))
    md.append("\n**Top 10 no-trade reasons:**\n")
    for reason, count in list(no_trade_reasons.items())[:10]:
        md.append(f"- [{count}x] {reason}\n")

    # ── Section 18: Strategy Decisions ───────────────────────────────────
    md.append("## 18. Strategy Decisions\n")
    md.append(md_table(decisions, [
        "decision_time", "action", "strategy_name", "reason"
    ], max_rows=100))

    # ── Section 19: No-Trade Reasons ──────────────────────────────────────
    md.append("## 19. No-Trade Reason Frequency\n")
    if no_trade_reasons:
        for reason, count in no_trade_reasons.items():
            md.append(f"- [{count}x] {reason}\n")
    else:
        md.append("_No NO_TRADE decisions recorded._\n")

    # ── Section 20: Trade Deep Dive ───────────────────────────────────────
    md.append("## 20. Trade Deep Dive\n")
    if not trade_entries:
        md.append("_No trades entered._\n")

    for t in trade_entries:
        pid = t.get("position_id")
        md.append(f"\n### Position `{pid}` — {t.get('strategy_name')}\n")
        md.append(md_kv({
            "Entry time":              t.get("entry_time"),
            "Day label":               t.get("day_label"),
            "Selection reason":        t.get("selection_reason"),
            "Entry spot/VIX/VRP":      f"{t.get('entry_spot')} / "
                                       f"{t.get('entry_vix')} / "
                                       f"{t.get('entry_vrp_smoothed')}",
            "ATM IV at entry":         t.get("entry_atm_iv"),
            "IV behavior at entry":    t.get("iv_behavior"),
            "Day move used at entry":  t.get("day_move_used_at_entry"),
            "Opening straddle":        t.get("opening_straddle_at_entry"),
            "Vol regime at entry":     t.get("vol_regime_at_entry"),
            "Price regime at entry":   t.get("price_regime_at_entry"),
            "Positioning at entry":    t.get("positioning_at_entry"),
            "Final regime at entry":   t.get("final_regime_at_entry"),
            "Confidence":              f"{t.get('confidence_level_at_entry')} "
                                       f"({t.get('confidence_score_at_entry')})",
            "OR condition/width":      f"{t.get('or_condition')} / {t.get('or_width')}",
            "Target expiry/DTE":       f"{t.get('target_expiry')} / {t.get('actual_dte')}",
            "Entry credit":            t.get("entry_credit"),
            "Gross credit":            t.get("gross_credit"),
            "Is borderline sell":      t.get("is_borderline_sell"),
            "Calibration tier":        t.get("calibration_tier_at_entry"),
            "Stop premium":            t.get("stop_premium"),
            "Target premium":          t.get("target_premium"),
            "Price stop pts":          t.get("price_stop_pts"),
            "Final lots":              t.get("final_lots"),
            "Total max risk (Rs)":     t.get("total_max_risk"),
            "Capital at entry":        t.get("capital_at_entry"),
            "Daily P&L at entry":      t.get("daily_pnl_at_entry"),
        }))

        # Legs
        try:
            leg_list = json.loads(t["legs_json"]) if t.get("legs_json") else []
        except Exception:
            leg_list = []
        if leg_list:
            md.append("\n**Legs at entry:**\n")
            md.append(md_table(leg_list, [
                "action", "option_type", "strike", "exec_price",
                "delta", "gamma", "vega", "theta", "iv", "oi"
            ]))

        # Exit
        exit_row = exits_by_position.get(pid)
        if exit_row:
            md.append("\n**Exit:**\n")
            md.append(md_kv({
                "Exit time":           exit_row.get("exit_time"),
                "Exit reason":         exit_row.get("exit_reason"),
                "Exit priority":       f"{exit_row.get('exit_priority')} "
                                       f"({exit_row.get('exit_priority_name')})",
                "Hold time (min)":     exit_row.get("hold_minutes"),
                "Exit premium":        exit_row.get("exit_premium"),
                "Exit spot":           exit_row.get("exit_spot"),
                "Exit VIX":            exit_row.get("exit_vix"),
                "Gross P&L (pts/Rs)":  f"{exit_row.get('gross_pnl_pts')} / "
                                       f"{exit_row.get('gross_pnl_rupees')}",
                "Exit costs (Rs)":     exit_row.get("exit_costs_rupees"),
                "Total costs (Rs)":    exit_row.get("total_costs_rupees"),
                "Net P&L (pts/Rs/%)":  f"{exit_row.get('net_pnl_pts')} / "
                                       f"{exit_row.get('net_pnl_rupees')} / "
                                       f"{exit_row.get('net_pnl_pct')}",
                "Result":              exit_row.get("result"),
                "Profit pct credit":   exit_row.get("profit_pct_of_credit"),
            }))
            crush = iv_crush_by_pos.get(pid, {})
            if crush:
                md.append(md_kv({
                    "IV crush": (
                        f"entry={crush.get('entry_iv')} "
                        f"exit={crush.get('exit_iv')} "
                        f"crush={crush.get('iv_crush_pct')}% "
                        f"[{crush.get('direction')}]"
                    )
                }))
        else:
            md.append("\n**Exit:** _No matching exit row — position may still be open._\n")

    # ── Section 21: Exit Quality ──────────────────────────────────────────
    md.append("## 21. Exit Quality Analysis\n")
    md.append(md_kv(exit_quality_sum))
    if exit_quality_rows:
        md.append("\n**Exit quality details:**\n")
        md.append(md_table(exit_quality_rows, [
            "position_id", "exit_priority_fired", "exit_time",
            "exit_pnl_rupees", "pnl_15min_after_exit",
            "was_exit_premature", "was_exit_late", "optimal_exit_pnl",
        ], max_rows=20))

    # ── Section 22: IV Crush ──────────────────────────────────────────────
    md.append("## 22. IV Crush Per Trade\n")
    for pid, crush in iv_crush_by_pos.items():
        if crush:
            md.append(
                f"- **{str(pid)[:16]}**: "
                f"entry={crush.get('entry_iv')} "
                f"exit={crush.get('exit_iv')} "
                f"crush={crush.get('iv_crush_pct')}% "
                f"[{crush.get('direction')}]\n"
            )

    # ── Section 23: Slippage ──────────────────────────────────────────────
    md.append("## 23. Slippage Analysis\n")
    md.append(md_kv(slippage_analysis))

    # ── Section 24: Phantom Trades ────────────────────────────────────────
    md.append("## 24. Phantom Trade Analysis\n")
    md.append(md_kv(phantom_summary))
    if phantom_rows:
        md.append("\n**Phantom trade details:**\n")
        md.append(md_table(phantom_rows, [
            "block_time", "block_reason", "strategy_would_be",
            "credit_would_be", "vrp_smoothed_at_block", "or_condition",
            "dte_at_block", "simulated_pnl_final", "simulated_result",
            "would_have_been_profitable",
        ], max_rows=20))

    # ── Section 25: P&L Curve ─────────────────────────────────────────────
    md.append("## 25. P&L Curve (intraday)\n")
    pnl_curve = [
        {
            "time":              c.get("cycle_time"),
            "pnl":               c.get("daily_pnl_net"),
            "spot":              c.get("spot"),
            "vrp_smoothed":      c.get("vrp_smoothed"),
            "adx_15":            c.get("adx_15"),
            "vol_regime":        c.get("vol_regime"),
            "final_regime":      c.get("final_regime"),
            "day_move_used_pct": c.get("day_move_used_pct"),
        }
        for c in cycle_rows
        if c.get("daily_pnl_net") is not None
    ]
    if pnl_curve:
        md.append(md_table(pnl_curve, [
            "time", "pnl", "spot", "vrp_smoothed", "adx_15",
            "vol_regime", "final_regime", "day_move_used_pct",
        ], max_rows=100))
    else:
        md.append("_No P&L curve data._\n")

    # ── Section 26: Option Chain ──────────────────────────────────────────
    md.append("## 26. Option Chain Statistics\n")
    md.append(md_kv(chain_summary))
    md.append(f"\n_Full chain ({len(chain_rows)} rows) in raw JSON export._\n")

    # ── Section 27: Candle Stats ──────────────────────────────────────────
    md.append("## 27. Intraday 1-Minute Candle Statistics\n")
    md.append(md_kv(candle_stats) if candle_stats else "_No intraday candle data._\n")
    md.append(f"\n_Total 1-min bars: {len(intraday_candles)}._\n")

    # ── Section 28: API Health ────────────────────────────────────────────
    md.append("## 28. API Call Health\n")
    md.append(md_kv({k: v for k, v in api_summary.items() if k != "errors_sample"}))
    if api_summary.get("errors_sample"):
        md.append("\n**Sample API errors:**\n")
        md.append(md_table(api_summary["errors_sample"], [
            "call_time", "category", "endpoint", "status_code", "error_message"
        ]))

    # ── Section 29: Data Quality ──────────────────────────────────────────
    md.append("## 29. Data Quality Checks\n")
    dq = {
        "Cycles missing spot":              sum(1 for c in cycle_rows
                                                if c.get("spot") is None),
        "Cycles missing VRP":               sum(1 for c in cycle_rows
                                                if c.get("vrp_smoothed") is None
                                                and c.get("vrp_raw") is None),
        "Cycles missing VIX":               sum(1 for c in cycle_rows
                                                if c.get("vix") is None),
        "Cycles missing PCR":               sum(1 for c in cycle_rows
                                                if c.get("pcr") is None),
        "Cycles no final_regime":           sum(1 for c in cycle_rows
                                                if not c.get("final_regime")),
        "Cycles vol_regime=UNKNOWN":        sum(1 for c in cycle_rows
                                                if c.get("vol_regime") == "UNKNOWN"),
        "Cycles trend=OR_PENDING":          sum(1 for c in cycle_rows
                                                if c.get("price_regime") == "OBSERVING"),
        "Cycles chain_stale":               sum(1 for c in cycle_rows
                                                if c.get("chain_stale")),
        "Cycles block_new_entries":         sum(1 for c in cycle_rows
                                                if c.get("block_new_entries")),
        "Regime decisions logged":          len(regime_decisions),
        "Chain rows zero bid/ask":          chain_summary.get("zero_bid_ask_count", 0),
        "Trades no exit row":               sum(1 for t in trade_entries
                                                if t["position_id"] not in exits_by_position),
        "Positions still OPEN":             sum(1 for p in positions
                                                if p.get("status") == "OPEN"),
        "VIX history rows today":           len(vix_history_today),
        "Market snapshots today":           len(market_snaps_today),
        "Phantom trades today":             len(phantom_rows),
        "Exit quality records today":       len(exit_quality_rows),
        "Regime accuracy records today":    len(regime_accuracy_rows),
        "Calibration tier":                 calibration_summary.get("calibration_tier"),
        "Calibration valid":                calibration_summary.get("is_valid"),
    }
    md.append(md_kv(dq))

    # ── Section 30: Calibration Drift ────────────────────────────────────
    md.append("## 30. Calibration Drift Tracking\n")
    if calibration_drift:
        md.append(md_table(calibration_drift, [
            "calibrated_at", "calibration_tier", "is_valid",
            "vrp_sell_threshold", "vrp_fair_threshold",
            "vix_p50", "vix_p75",
            "day_size_tuesday", "day_size_monday",
            "pcr_bullish_threshold",
            "phantom_false_negative_rate", "exit_quality_score",
            "regime_accuracy_score",
        ], max_rows=20))
    else:
        md.append("_No calibration history._\n")

    # ── Section 31: Cumulative Performance ───────────────────────────────
    md.append("## 31. Cumulative Performance (90-day)\n")
    if equity_curve:
        md.append(md_kv({k: v for k, v in equity_curve.items()
                         if k != "daily_pnl_series"}))
        if equity_curve.get("daily_pnl_series"):
            md.append("\n**Daily P&L Series:**\n")
            md.append(md_table(equity_curve["daily_pnl_series"],
                               ["date", "pnl", "capital"], max_rows=90))
    else:
        md.append("_No cumulative data yet._\n")

    # ── Section 32: Prior Days ────────────────────────────────────────────
    md.append("## 32. Prior Days Comparison\n")
    if prior_days_summary:
        md.append(md_table(prior_days_summary, [
            "trading_date", "day_label", "trades_executed",
            "win_rate_pct", "net_pnl_rupees", "net_pnl_pct_capital",
            "vrp_mean", "or_condition", "stops_fired",
            "profit_factor", "capital_end",
            "dominant_vol_regime", "dominant_price_regime",
        ], max_rows=10))
    else:
        md.append("_No prior days data._\n")

    # ── Section 33: Audit Warnings ────────────────────────────────────────
    md.append("## 33. Audit Log — Warnings and Errors\n")
    md.append(
        f"WARNING/ERROR/CRITICAL: {len(warning_error_lines)} of "
        f"{len(audit_file_lines)} total "
        f"({len(audit_db_rows)} in DB)\n\n"
    )
    if warning_error_lines:
        md.append("```\n" + "\n".join(warning_error_lines[:200]) + "\n```\n")
        if len(warning_error_lines) > 200:
            md.append(
                f"_... {len(warning_error_lines) - 200} more lines in raw export._\n"
            )
    else:
        md.append("_No warnings or errors logged today._\n")

    # ── Section 34: Master Timeline ───────────────────────────────────────
    md.append("## 34. Unified Master Timeline\n")
    md.append(md_table(
        [{"time": e[0], "type": e[1], "detail": e[2]} for e in timeline],
        ["time", "type", "detail"],
        max_rows=200,
    ))

    # ── Section 35: Daily Summary ─────────────────────────────────────────
    md.append("## 35. Daily Summary (engine EOD)\n")
    md.append(md_kv(daily_summary) if daily_summary else "_No daily_summary row found._\n")

    # ── Section 36: LLM Context ───────────────────────────────────────────
    md.append("## 36. LLM Analysis Context\n")
    _vrp_curve_vals = [v["vrp_smoothed"] for v in vrp_curve
                       if v.get("vrp_smoothed") is not None]
    _vrp_sell_thresh = calibration_summary.get("vrp_sell_threshold") or 2.0
    _vrp_fair_thresh = calibration_summary.get("vrp_fair_threshold") or 1.2
    _vrp_above_sell  = sum(1 for v in _vrp_curve_vals if v > _vrp_sell_thresh)
    _vrp_below_fair  = sum(1 for v in _vrp_curve_vals if v < _vrp_fair_thresh)
    _vrp_negative    = sum(1 for v in _vrp_curve_vals if v < 0)
    _parkinson_vals  = [float(c.get("parkinson_rv_pct") or 0)
                        for c in cycle_rows if c.get("parkinson_rv_pct")]
    _rv_spike_cycles = sum(
        1 for i in range(1, len(_parkinson_vals))
        if _parkinson_vals[i] > _parkinson_vals[i-1] * 1.5
    ) if len(_parkinson_vals) > 1 else 0
    _rv_floor_cycles = sum(
        1 for v in _parkinson_vals if abs(v - 6.0) < 0.01
    )
    _actual_dte_today    = session_state.get("actual_dte") if session_state else None
    _actual_expiry_today = session_state.get("actual_expiry") if session_state else None
    _tenor_mismatch = (
        _actual_dte_today == 0 and
        _actual_expiry_today is not None and
        _actual_expiry_today != target_date
    )
    _no_trade_by_gate: dict = {}
    for _d in decisions:
        if _d.get("action") == "NO_TRADE":
            _r = str(_d.get("reason") or "unknown")
            if "before_entry" in _r or "past_entry" in _r or "hard_exit" in _r:
                _g = "timing"
            elif "ev_gate" in _r or "params_invalid" in _r:
                _g = "ev_or_params"
            elif "confidence" in _r or "dte_" in _r:
                _g = "regime_quality"
            elif "VOL_NEUTRAL" in _r or "VOL_BUY" in _r or "ABORT" in _r:
                _g = "volatility"
            elif "consecutive" in _r or "daily_loss" in _r or "halt" in _r:
                _g = "risk"
            elif "or_not" in _r or "opening_range" in _r or "chain_stale" in _r:
                _g = "data"
            else:
                _g = "other"
            _no_trade_by_gate[_g] = _no_trade_by_gate.get(_g, 0) + 1

    nifty_context = {
        "engine_type":              "NIFTY intraday options only — no overnight positions",
        "expiry_structure":         "Weekly Tuesday expiry — 0DTE Tuesday, 1DTE Monday",
        "vix_environment_2026":     "Suppressed VIX 11-13 — SUPPRESSED/LOW is normal",
        "primary_strategy":         "Premium selling — Iron Condor (range) / Bull Put / Bear Call (trend)",
        "vrp_sell_threshold":       calibration_summary.get("vrp_sell_threshold"),
        "vrp_fair_threshold":       calibration_summary.get("vrp_fair_threshold"),
        "calibration_tier":         calibration_summary.get("calibration_tier"),
        "calibration_valid":        calibration_summary.get("is_valid"),
        "day_size_tuesday_0dte":    calibration_summary.get("day_size_tuesday"),
        "day_size_monday_1dte":     calibration_summary.get("day_size_monday"),
        "vol_regime_today":         session_state.get("vix_regime") if session_state else None,
        "dominant_vol_regime":      daily_summary.get("dominant_vol_regime") if daily_summary else None,
        "dominant_price_regime":    daily_summary.get("dominant_price_regime") if daily_summary else None,
        "actual_dte_today":         _actual_dte_today,
        "actual_expiry_today":      _actual_expiry_today,
        "tenor_mismatch_detected":  _tenor_mismatch,
        "tenor_mismatch_note":      (
            f"DTE=0 stored but expiry={_actual_expiry_today} != {target_date}"
            if _tenor_mismatch else "ok"
        ),
        "trades_taken_today":       len(trade_entries),
        "trades_blocked_phantom":   len(phantom_rows),
        "phantom_fnr_today":        phantom_summary.get("false_negative_rate_pct"),
        "exit_quality_score_today": exit_quality_sum.get("exit_quality_score"),
        "regime_accuracy_today":    regime_accuracy_sum.get("avg_score"),
        "net_pnl_today":            net_pnl,
        "gross_pnl_today":          gross_pnl,
        "total_costs_today":        total_costs,
        "cost_coverage_ratio":      round(gross_pnl / max(total_costs, 1), 2) if gross_pnl > 0 else None,
        "cost_as_pct_gross":        round(total_costs / gross_pnl * 100, 1) if gross_pnl > 0 else None,
        "opening_straddle_pts":     session_state.get("opening_straddle_pts") if session_state else None,
        "vrp_mean_today":           vrp_stats.get("vrp_mean"),
        "vrp_above_sell_threshold_cycles": _vrp_above_sell,
        "vrp_below_fair_threshold_cycles": _vrp_below_fair,
        "vrp_negative_cycles":      _vrp_negative,
        "rv_spike_cycles":          _rv_spike_cycles,
        "rv_floor_cycles":          _rv_floor_cycles,
        "iv_crush_today":           vrp_stats.get("iv_crush_pct"),
        "atm_iv_open":              vrp_stats.get("atm_iv_open_pct"),
        "atm_iv_close":             vrp_stats.get("atm_iv_close_pct"),
        "parkinson_rv_mean":        vrp_stats.get("parkinson_rv_mean_pct"),
        "or_condition_today":       or_analysis.get("or_condition"),
        "or_width_pts":             or_analysis.get("or_width_pts"),
        "or_width_pct":             or_analysis.get("or_width_pct"),
        "day_move_used_max":        max((float(c.get("day_move_used_pct") or 0) for c in cycle_rows), default=0),
        "abort_cycles_today":       sum(1 for c in cycle_rows if c.get("block_new_entries")),
        "sell_premium_cycles":      sum(1 for c in cycle_rows if c.get("vol_regime") in ("SELL_PREMIUM", "STRONG_SELL_PREMIUM")),
        "strong_sell_cycles":       sum(1 for c in cycle_rows if c.get("vol_regime") == "STRONG_SELL_PREMIUM"),
        "neutral_cycles":           sum(1 for c in cycle_rows if c.get("vol_regime") == "NEUTRAL"),
        "buy_options_cycles":       sum(1 for c in cycle_rows if c.get("vol_regime") == "BUY_OPTIONS"),
        "range_cycles":             sum(1 for c in cycle_rows if c.get("price_regime") == "RANGE"),
        "trending_cycles":          sum(1 for c in cycle_rows if c.get("price_regime") in ("UPTREND", "DOWNTREND", "STRONG_UPTREND", "STRONG_DOWNTREND")),
        "choppy_cycles":            sum(1 for c in cycle_rows if c.get("price_regime") == "CHOPPY"),
        "no_trade_by_gate":         _no_trade_by_gate,
        "total_no_trade_decisions": sum(1 for d in decisions if d.get("action") == "NO_TRADE"),
        "total_enter_decisions":    sum(1 for d in decisions if d.get("action") == "ENTER"),
        "vix_open":                 vix_profile.get("vix_open"),
        "vix_close":                vix_profile.get("vix_close"),
        "vix_range":                vix_profile.get("vix_range"),
        "nifty_range_pts":          spot_profile.get("range_pts"),
        "nifty_range_pct":          spot_profile.get("range_pct"),
        "day_mode":                 session_state.get("day_mode") if session_state else None,
        "gap_direction":            session_state.get("gap_direction") if session_state else None,
        "gap_size_pts":             session_state.get("gap_size_pts") if session_state else None,
        "adx_profile":              adx_profile,
        "pcr_profile":              pcr_profile,
        "calibration_tier":         calibration_summary.get("calibration_tier"),
        "calibration_valid":        calibration_summary.get("is_valid"),
        "vrp_sell_threshold":       _vrp_sell_thresh,
        "vrp_fair_threshold":       _vrp_fair_thresh,
    }
    md.append(md_kv(nifty_context))
    md.append(f"""
**For AI/LLM analysis — NIFTY intraday options engine v3.0 (regime-based) — {target_date}:**

Net P&L: Rs{net_pnl} | Trades: {len(trade_entries)} entered {len(trade_exits)} closed | Win Rate: {round(wins/len(trade_exits)*100,1) if trade_exits else 0}%
VIX: {vix_profile.get('vix_open')} → {vix_profile.get('vix_close')} | VIX range: {vix_profile.get('vix_range')}
OR: {or_analysis.get('or_condition')} {or_analysis.get('or_width_pts')}pts | VRP mean: {vrp_stats.get('vrp_mean')}pp
Calibration: Tier {calibration_summary.get('calibration_tier')} valid={calibration_summary.get('is_valid')}
Regime decisions: {len(regime_decisions)} | Cycles: {len(cycle_rows)} | VRP curve points: {len(vrp_curve)}
Phantom FNR: {phantom_summary.get('false_negative_rate_pct')}% ({phantom_summary.get('total')} blocked)
Exit quality score: {exit_quality_sum.get('exit_quality_score')}
Regime accuracy: {regime_accuracy_sum.get('avg_score')}

Key questions for LLM analysis (regime-based engine v3.0):
1. Was vol regime (SELL_PREMIUM/NEUTRAL) classification accurate given actual market outcome?
2. Was price regime (RANGE/UPTREND/DOWNTREND) classification correct?
3. Was positioning regime (STRONG_RANGE/BULLISH/BEARISH) predictive of which side to sell?
4. Was the confidence score calibrated correctly?
5. Were entry/exit timings optimal given VRP curve?
6. Did IV crush work in favor of the strategy?
7. Were transaction costs proportionate to gross P&L?
8. What calibration improvements would improve future performance?
9. Were there missed opportunities (check phantom trade analysis)?
10. Was stop loss management appropriate given day volatility?
11. Which exit priority fired most? Was it appropriate?
12. Was the straddle ratio at entry predictive of trade outcome?
13. Were ADX signals available during the primary entry window (10:30-13:00)?
14. Did the positioning regime (PCR/skew/OI) correctly predict directional bias?
15. Was the Parkinson RV computation stable or did it show anomalies?
16. Were there false ABORT signals due to data quality issues?
17. Did the engine take all valid trades or were good setups blocked by gates?
18. What was the optimal entry time based on VRP curve richness today?
19. Were transaction costs below 15% of gross credit collected?
20. Is the phantom FNR suggesting the VRP threshold needs adjustment?
21. Are exits via PROFIT_LOCK (priority 4) well-timed?
22. Is the CHEAP_BUYBACK exit (priority 5) adding value?
23. Was the borderline sell trade (if any) profitable?
24. Is the calibration tier appropriate for the data available?
25. Did the day_move_used gate correctly block late entries?
""")

    # ── Section 37: Raw Export Manifest ──────────────────────────────────
    md.append("## 37. Raw Data Export Manifest\n")
    md.append(f"All tables exported to: `eod_report_{target_date}_raw/`\n")

    # ── Write Markdown report ─────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"eod_report_{target_date}.md"
    report_path.write_text("\n".join(md), encoding="utf-8")

    # ── Write raw JSON exports ────────────────────────────────────────────
    raw_dir = OUTPUT_DIR / f"eod_report_{target_date}_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    raw_exports = {
        "session_state":             session_state,
        "cycle_log":                 cycle_rows,
        "strategy_decisions":        decisions,
        "positions":                 positions,
        "position_legs":             legs,
        "trade_entries":             trade_entries,
        "trade_exits":               trade_exits,
        "daily_summary":             daily_summary,
        "option_chain_snapshot":     chain_rows,
        "api_call_log":              api_rows,
        "audit_log_db":              audit_db_rows,
        "audit_log_file_lines":      audit_file_lines,
        "regime_decisions":          regime_decisions,
        "calibration_state":         calibration_row,
        "calibration_drift":         calibration_drift,
        "vix_history_today":         vix_history_today,
        "market_snapshots_today":    market_snaps_today,
        "phantom_trades_today":      phantom_rows,
        "exit_quality_today":        exit_quality_rows,
        "regime_accuracy_today":     regime_accuracy_rows,
        "master_timeline":           [{"time": e[0], "type": e[1], "detail": e[2]}
                                      for e in timeline],
        "anomaly_flags":             anomalies,
        "no_trade_reason_counts":    no_trade_reasons,
        "strategies_used_counts":    strategies_used,
        "chain_summary":             chain_summary,
        "vrp_statistics":            vrp_stats,
        "vrp_curve":                 vrp_curve,
        "spot_profile":              spot_profile,
        "adx_profile":               adx_profile,
        "pcr_profile":               pcr_profile,
        "skew_profile":              skew_profile,
        "pnl_curve":                 pnl_curve if 'pnl_curve' in dir() else [],
        "regime_timeline":           regime_timeline,
        "regime_distribution":       regime_dist,
        "calibration_summary":       calibration_summary,
        "intraday_candles_1min":     intraday_candles,
        "candle_statistics":         candle_stats,
        "cumulative_performance_90d":equity_curve,
        "iv_crush_per_trade":        iv_crush_by_pos,
        "slippage_analysis":         slippage_analysis,
        "phantom_summary":           phantom_summary,
        "exit_quality_summary":      exit_quality_sum,
        "regime_accuracy_summary":   regime_accuracy_sum,
        "or_analysis":               or_analysis,
        "vix_intraday_profile":      vix_profile,
        "api_summary":               {k: v for k, v in api_summary.items()
                                      if k != "errors_sample"},
        "llm_analysis_context": {
            "target_date":        target_date,
            "net_pnl_rupees":     net_pnl,
            "gross_pnl_rupees":   gross_pnl,
            "total_costs_rupees": total_costs,
            "trades_entered":     len(trade_entries),
            "trades_closed":      len(trade_exits),
            "wins":               wins,
            "losses":             losses,
            "win_rate_pct":       round(wins / len(trade_exits) * 100, 1)
                                  if trade_exits else 0,
            "vix_profile":        vix_profile,
            "or_analysis":        or_analysis,
            "calibration_tier":   calibration_summary.get("calibration_tier"),
            "vrp_curve_summary": {
                "count":   len(vrp_curve),
                "min_vrp": min((v["vrp_smoothed"] for v in vrp_curve), default=None),
                "max_vrp": max((v["vrp_smoothed"] for v in vrp_curve), default=None),
                "avg_vrp": round(
                    statistics.mean(v["vrp_smoothed"] for v in vrp_curve), 3
                ) if vrp_curve else None,
            },
            "anomaly_count":      len([f for f in anomalies if "[FLAG]" in f]),
            "phantom_summary":    phantom_summary,
            "exit_quality_sum":   exit_quality_sum,
            "regime_accuracy_sum":regime_accuracy_sum,
            "slippage_analysis":  slippage_analysis,
        },
    }

    for name, data_val in raw_exports.items():
        try:
            (raw_dir / f"{name}.json").write_text(
                json.dumps(data_val, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  Warning: could not write {name}.json: {e}")

    # ── Print summary ─────────────────────────────────────────────────────
    print(f"Report:   {report_path}")
    print(f"Raw data: {raw_dir}/")
    print(
        f"Net P&L: Rs{net_pnl} | "
        f"Trades: {len(trade_entries)} | "
        f"Anomalies: {len([f for f in anomalies if '[FLAG]' in f])}"
    )
    print("\nQuick summary:")
    for k, v in list(exec_summary.items())[:15]:
        print(f"  {k}: {v}")
    print("\nAnomaly flags:")
    for f in anomalies:
        print(f"  {f}")


# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    """
    Self-test for eod_report.py.
    Tests all computation functions with mock data.
    Run: python eod_report.py --test
    """
    print("=" * 60)
    print("EOD REPORT SELF-TEST")
    print("=" * 60)

    # ── Test candle statistics ────────────────────────────────────────────
    print("\n--- Candle Statistics Tests ---")
    mock_candles = [
        {"candle_time": f"09:{15+i:02d}:00", "open": 24000+i, "high": 24010+i,
         "low": 23990+i, "close": 24005+i, "volume": 0}
        for i in range(30)
    ]
    stats = compute_candle_statistics(mock_candles)
    assert stats["total_1min_bars"] == 30, "Should have 30 bars"
    assert stats["high"] > stats["low"], "High should be above low"
    assert "net_change_pts" in stats, "Should have net_change_pts"
    print(f"  Candle stats: {stats['total_1min_bars']} bars, "
          f"range={stats.get('high',0)-stats.get('low',0):.0f}pts")
    print("  [OK] Candle statistics tests passed")

    # ── Test VRP statistics ───────────────────────────────────────────────
    print("\n--- VRP Statistics Tests ---")
    mock_cycles = [
        {"vrp_smoothed": 3.0 + i * 0.1, "vrp_raw": 3.0 + i * 0.1,
         "atm_iv_pct": 12.5, "parkinson_rv_pct": 9.0}
        for i in range(20)
    ]
    vrp_s = compute_vrp_statistics(mock_cycles)
    assert vrp_s["vrp_mean"] > 0, "VRP mean should be positive"
    assert vrp_s["vrp_rich_cycles"] > 0, "Should have rich cycles"
    print(f"  VRP stats: mean={vrp_s['vrp_mean']:.3f} "
          f"rich={vrp_s['vrp_rich_cycles']}")
    print("  [OK] VRP statistics tests passed")

    # ── Test VIX profile ──────────────────────────────────────────────────
    print("\n--- VIX Profile Tests ---")
    mock_vix = [{"vix_value": 11.0 + i * 0.1} for i in range(20)]
    vix_p = compute_vix_profile(mock_vix)
    assert vix_p["vix_open"] == 11.0, f"VIX open should be 11.0, got {vix_p['vix_open']}"
    assert vix_p["vix_close"] > vix_p["vix_open"], "VIX close should be above open"
    assert vix_p["pct_below_14"] == 100.0, "All readings should be below 14"
    print(f"  VIX profile: open={vix_p['vix_open']} close={vix_p['vix_close']}")
    print("  [OK] VIX profile tests passed")

    # ── Test spot profile ─────────────────────────────────────────────────
    print("\n--- Spot Profile Tests ---")
    mock_spot_cycles = [
        {"cycle_time": f"10:{i:02d}:00", "spot": 24000.0 + i * 5}
        for i in range(20)
    ]
    sp = compute_intraday_spot_profile(mock_spot_cycles)
    assert sp["open"] == 24000.0, f"Open should be 24000, got {sp['open']}"
    assert sp["direction"] == "UP", f"Direction should be UP, got {sp['direction']}"
    assert sp["range_pts"] > 0, "Range should be positive"
    print(f"  Spot profile: open={sp['open']} close={sp['close']} "
          f"range={sp['range_pts']}pts dir={sp['direction']}")
    print("  [OK] Spot profile tests passed")

    # ── Test OR analysis ──────────────────────────────────────────────────
    print("\n--- OR Analysis Tests ---")
    mock_session = {
        "or_high": 24100.0, "or_low": 24040.0, "or_width": 60.0,
        "or_condition": "NARROW",
    }
    mock_entries = [
        {"entry_spot": 24120.0},  # above OR
        {"entry_spot": 24070.0},  # in OR
        {"entry_spot": 24020.0},  # below OR
    ]
    or_a = compute_or_analysis(mock_session, [], mock_entries)
    assert or_a["or_computed"] == True, "OR should be computed"
    assert or_a["entries_above_or"] == 1, f"Expected 1 above OR, got {or_a['entries_above_or']}"
    assert or_a["entries_in_or"]   == 1, f"Expected 1 in OR, got {or_a['entries_in_or']}"
    assert or_a["entries_below_or"] == 1, f"Expected 1 below OR, got {or_a['entries_below_or']}"
    print(f"  OR analysis: above={or_a['entries_above_or']} "
          f"in={or_a['entries_in_or']} below={or_a['entries_below_or']}")
    print("  [OK] OR analysis tests passed")

    # ── Test equity curve ─────────────────────────────────────────────────
    print("\n--- Equity Curve Tests ---")
    mock_cumulative = [
        {"trading_date": f"2026-01-{i+1:02d}", "net_pnl_rupees": 5000.0 * (1 if i % 3 != 2 else -1),
         "capital_end": 1000000.0 + i * 3000.0}
        for i in range(10)
    ]
    ec = compute_equity_curve(mock_cumulative)
    assert ec["total_trading_days"] == 10, f"Expected 10 days, got {ec['total_trading_days']}"
    assert ec["capital_start"] is not None, "Capital start should not be None"
    assert ec["max_drawdown_rupees"] >= 0, "Max drawdown should be non-negative"
    print(f"  Equity curve: {ec['total_trading_days']} days "
          f"win_rate={ec['day_win_rate_pct']}% "
          f"max_dd=Rs{ec['max_drawdown_rupees']:,.0f}")
    print("  [OK] Equity curve tests passed")

    # ── Test regime distribution ──────────────────────────────────────────
    print("\n--- Regime Distribution Tests ---")
    mock_regime_decisions = [
        {"final_regime": "PREMIUM_SELL_RANGE"},
        {"final_regime": "PREMIUM_SELL_RANGE"},
        {"final_regime": "NO_TRADE"},
        {"final_regime": "PREMIUM_SELL_BULL"},
    ]
    rd = compute_regime_distribution(mock_regime_decisions, [])
    assert "PREMIUM_SELL_RANGE" in rd, "Should have PREMIUM_SELL_RANGE"
    assert rd["PREMIUM_SELL_RANGE"] == 2, f"Expected 2, got {rd['PREMIUM_SELL_RANGE']}"
    print(f"  Regime dist: {rd}")
    print("  [OK] Regime distribution tests passed")

    # ── Test no-trade reasons aggregation ─────────────────────────────────
    print("\n--- No-Trade Reasons Tests ---")
    mock_decisions = [
        {"action": "NO_TRADE", "reason": "VOL_NEUTRAL"},
        {"action": "NO_TRADE", "reason": "VOL_NEUTRAL"},
        {"action": "NO_TRADE", "reason": "confidence_LOW"},
        {"action": "ENTER", "reason": "regime:PREMIUM_SELL_RANGE"},
    ]
    ntr = aggregate_no_trade_reasons(mock_decisions)
    assert ntr.get("VOL_NEUTRAL") == 2, f"Expected 2 VOL_NEUTRAL, got {ntr.get('VOL_NEUTRAL')}"
    assert "confidence_LOW" in ntr, "Should have confidence_LOW"
    assert len(ntr) == 2, f"Should have 2 reasons, got {len(ntr)}"
    print(f"  No-trade reasons: {ntr}")
    print("  [OK] No-trade reasons tests passed")

    # ── Test phantom summary ──────────────────────────────────────────────
    print("\n--- Phantom Summary Tests ---")
    mock_phantoms = [
        {"would_have_been_profitable": 1, "credit_would_be": 28.0},
        {"would_have_been_profitable": 1, "credit_would_be": 25.0},
        {"would_have_been_profitable": 0, "credit_would_be": 22.0},
        {"would_have_been_profitable": 1, "credit_would_be": 30.0},
    ]
    ps = compute_phantom_summary(mock_phantoms)
    assert ps["total"] == 4, f"Expected 4, got {ps['total']}"
    assert ps["would_have_won"] == 3, f"Expected 3, got {ps['would_have_won']}"
    assert ps["false_negative_rate_pct"] == 75.0, \
        f"Expected 75.0%, got {ps['false_negative_rate_pct']}"
    assert "too tight" in ps["interpretation"], \
        f"Expected 'too tight' interpretation, got {ps['interpretation']}"
    print(f"  Phantom summary: total={ps['total']} fnr={ps['false_negative_rate_pct']}%")
    print("  [OK] Phantom summary tests passed")

    # ── Test exit quality summary ─────────────────────────────────────────
    print("\n--- Exit Quality Summary Tests ---")
    mock_exit_quality = [
        {"exit_priority_fired": 4, "exit_pnl_rupees": 3000.0,
         "pnl_15min_after_exit": 3500.0, "was_exit_premature": 1, "was_exit_late": 0},
        {"exit_priority_fired": 6, "exit_pnl_rupees": 2500.0,
         "pnl_15min_after_exit": 2200.0, "was_exit_premature": 0, "was_exit_late": 0},
        {"exit_priority_fired": 7, "exit_pnl_rupees": 1800.0,
         "pnl_15min_after_exit": None, "was_exit_premature": 0, "was_exit_late": 0},
    ]
    eqs = compute_exit_quality_summary(mock_exit_quality)
    assert eqs["total_exits"] == 3, f"Expected 3, got {eqs['total_exits']}"
    assert eqs["premature_exits"] == 1, f"Expected 1 premature, got {eqs['premature_exits']}"
    assert eqs["exit_quality_score"] is not None, "Quality score should not be None"
    print(f"  Exit quality: total={eqs['total_exits']} "
          f"premature={eqs['premature_exits']} "
          f"score={eqs['exit_quality_score']}")
    print("  [OK] Exit quality summary tests passed")

    # ── Test anomaly detection ────────────────────────────────────────────
    print("\n--- Anomaly Detection Tests ---")
    mock_session_ok = {
        "daily_halted": False, "circuit_breaker_suspected": False,
        "vix_spike_detected": False, "or_computed": True,
        "session_initialized": True,
    }
    mock_spot_ok = {"range_pct": 0.8}
    mock_vix_ok  = {"vix_range": 1.5}
    mock_or_ok   = {"or_computed": True, "or_width_pct": 0.3}
    mock_vrp_ok  = {"vrp_negative_cycles": 0, "total_vrp_cycles": 10}
    mock_cal_ok  = {"calibration_tier": 2, "is_valid": True}
    mock_phantom_ok = {"total": 5, "false_negative_rate_pct": 20.0}
    mock_exit_ok    = {"total_exits": 2, "avg_improvement_15min": 100.0}
    mock_regime_acc_ok = {"total_decisions": 5, "avg_score": 0.75}

    flags = detect_anomalies(
        mock_session_ok, [], {}, None, [], [], mock_vrp_ok,
        mock_spot_ok, [], mock_cal_ok, mock_vix_ok, mock_or_ok,
        mock_phantom_ok, mock_exit_ok, mock_regime_acc_ok,
    )
    # Should have calibration OK flag and no major anomalies
    assert any("[OK]" in f for f in flags), "Should have at least one OK flag"
    print(f"  Anomaly flags: {len(flags)} total")
    for f in flags:
        print(f"    {f}")

    # Test with halted session
    mock_session_halted = dict(mock_session_ok)
    mock_session_halted["daily_halted"] = True
    flags_halted = detect_anomalies(
        mock_session_halted, [], {}, None, [], [], mock_vrp_ok,
        mock_spot_ok, [], mock_cal_ok, mock_vix_ok, mock_or_ok,
        mock_phantom_ok, mock_exit_ok, mock_regime_acc_ok,
    )
    assert any("[FLAG]" in f and "halted" in f.lower() for f in flags_halted), \
        "Should have halted flag"
    print("  [OK] Anomaly detection tests passed")

    # ── Test markdown helpers ─────────────────────────────────────────────
    print("\n--- Markdown Helper Tests ---")

    # md_kv
    kv_out = md_kv({"key1": "val1", "key2": 42.5, "key3": None})
    assert "**key1**" in kv_out, "Should have bold key"
    assert "val1" in kv_out, "Should have value"
    assert "N/A" in kv_out, "None should become N/A"

    # md_table
    rows = [{"a": 1, "b": "hello"}, {"a": 2, "b": "world"}]
    tbl = md_table(rows, ["a", "b"])
    assert "| a | b |" in tbl, "Should have header row"
    assert "hello" in tbl, "Should have data"

    # md_table with max_rows
    many_rows = [{"x": i} for i in range(100)]
    tbl2 = md_table(many_rows, ["x"], max_rows=5)
    assert "95 more rows omitted" in tbl2, "Should mention omitted rows"

    # filter_log_lines_by_level
    mock_lines = [
        "2026-01-15 10:00:00 | INFO     | test | normal message",
        "2026-01-15 10:01:00 | WARNING  | test | warning message",
        "2026-01-15 10:02:00 | ERROR    | test | error message",
        "2026-01-15 10:03:00 | DEBUG    | test | debug message",
    ]
    filtered = filter_log_lines_by_level(mock_lines, {"WARNING", "ERROR"})
    assert len(filtered) == 2, f"Expected 2 filtered lines, got {len(filtered)}"
    assert all("WARNING" in l or "ERROR" in l for l in filtered), \
        "All filtered lines should be WARNING or ERROR"

    print("  [OK] Markdown helper tests passed")

    # ── Test calibration summary ──────────────────────────────────────────
    print("\n--- Calibration Summary Tests ---")
    mock_cal_row = {
        "calibration_tier": 2,
        "is_valid": 1,
        "n_trading_days": 25,
        "n_tuesday_expiries": 5,
        "calibrated_at": "2026-01-15T15:30:00",
        "vrp_sell_threshold": 2.5,
        "vrp_fair_threshold": 1.5,
        "vix_p50": 12.5,
        "notes": "tier=2 days=25",
    }
    cal_sum = compute_calibration_summary(mock_cal_row)
    assert cal_sum["calibration_tier"] == 2, f"Expected tier 2, got {cal_sum['calibration_tier']}"
    assert cal_sum["is_valid"] == True, "Should be valid"
    assert "Tier 2" in cal_sum["tier_description"], "Should have Tier 2 description"
    print(f"  Calibration summary: tier={cal_sum['calibration_tier']} "
          f"valid={cal_sum['is_valid']}")
    print("  [OK] Calibration summary tests passed")

    # ── Test with no calibration ──────────────────────────────────────────
    cal_sum_none = compute_calibration_summary(None)
    assert "No calibration" in cal_sum_none.get("status", ""), \
        "None calibration should have status message"
    print("  [OK] No-calibration edge case passed")

    # ── Test database connection (if DB exists) ───────────────────────────
    print("\n--- Database Tests ---")
    if DB_PATH.exists():
        try:
            conn = get_connection(DB_PATH)
            tables = [
                row[0] for row in
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            print(f"  Database found: {DB_PATH}")
            print(f"  Tables: {len(tables)}")
            conn.close()
            print("  [OK] Database connection test passed")
        except Exception as e:
            print(f"  Database test error: {e}")
    else:
        print(f"  Database not found at {DB_PATH} — skipping DB tests")
        print("  (Run the engine first to create the database)")

    print()
    print("=" * 60)
    print("EOD REPORT SELF-TEST COMPLETE — All tests passed")
    print("=" * 60)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from datetime import date as _d2

    args = sys.argv[1:]

    # Self-test mode
    if "--test" in args:
        _self_test()
        sys.exit(0)

    # Parse date
    td = _d2.today().isoformat()
    for i, a in enumerate(args):
        if a == "--date" and i + 1 < len(args):
            td = args[i + 1]
        elif a == "--yesterday":
            td = (_d2.today() - timedelta(days=1)).isoformat()

    # Validate date format
    try:
        datetime.strptime(td, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid date format: {td}. Use YYYY-MM-DD.")
        sys.exit(1)

    print(f"Generating EOD report for: {td}")
    generate_report(td)