# backtest.py
# NIFTY Intraday Options Engine v3.0
# Walk-forward backtest: replays stored trade data from database,
# computes comprehensive performance metrics, regime analysis,
# strategy breakdown, DTE performance, IV crush, slippage analysis,
# payoff geometry, calibration drift, and generates JSON report.

from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path
from datetime import datetime, date as _date_cls, timedelta
from typing import Optional, List, Dict, Any

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

LOT_SIZE         = int(_ENV.get("NIFTY_LOT_SIZE", "75") or 75)
STARTING_CAPITAL = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1_000_000)


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """Open read-only SQLite connection."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
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
# SAFE STATISTICS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def safe_mean(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 4) if vals else None


def safe_stdev(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.stdev(vals), 4) if len(vals) >= 2 else None


def safe_median(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 4) if vals else None


def safe_percentile(values: list, pct: float) -> Optional[float]:
    """Compute percentile using linear interpolation."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    idx = (n - 1) * pct / 100.0
    lo  = int(idx)
    hi  = lo + 1
    if hi >= n:
        return round(vals[-1], 4)
    frac = idx - lo
    return round(vals[lo] + frac * (vals[hi] - vals[lo]), 4)


def compute_sharpe(pnl_series: list, risk_free_daily: float = 0.0) -> Optional[float]:
    """Compute annualised Sharpe ratio from daily P&L series."""
    if len(pnl_series) < 5:
        return None
    try:
        excess = [p - risk_free_daily for p in pnl_series]
        mean   = statistics.mean(excess)
        std    = statistics.stdev(excess)
        if std <= 0:
            return None
        return round(mean / std * (252 ** 0.5), 3)
    except Exception:
        return None


def compute_sortino(pnl_series: list, risk_free_daily: float = 0.0) -> Optional[float]:
    """Compute annualised Sortino ratio from daily P&L series."""
    if len(pnl_series) < 5:
        return None
    try:
        excess    = [p - risk_free_daily for p in pnl_series]
        mean      = statistics.mean(excess)
        downside  = [p for p in excess if p < 0]
        if not downside:
            return None
        down_std  = statistics.stdev(downside) if len(downside) >= 2 else abs(downside[0])
        if down_std <= 0:
            return None
        return round(mean / down_std * (252 ** 0.5), 3)
    except Exception:
        return None


def compute_calmar(total_return_pct: float, max_drawdown_pct: float) -> Optional[float]:
    """Compute Calmar ratio = total return / max drawdown."""
    if max_drawdown_pct <= 0:
        return None
    return round(total_return_pct / max_drawdown_pct, 3)


def compute_max_drawdown(capital_series: list) -> tuple:
    """
    Compute max drawdown from capital series.
    Returns (max_drawdown_rupees, max_drawdown_pct, drawdown_start_idx, drawdown_end_idx)
    """
    if not capital_series:
        return 0.0, 0.0, 0, 0
    peak       = capital_series[0]
    peak_idx   = 0
    max_dd     = 0.0
    max_dd_pct = 0.0
    dd_start   = 0
    dd_end     = 0

    for i, cap in enumerate(capital_series):
        if cap > peak:
            peak     = cap
            peak_idx = i
        dd = peak - cap
        if dd > max_dd:
            max_dd     = dd
            max_dd_pct = dd / peak * 100.0 if peak > 0 else 0.0
            dd_start   = peak_idx
            dd_end     = i

    return round(max_dd, 2), round(max_dd_pct, 3), dd_start, dd_end


# ─────────────────────────────────────────────────────────────────────────────
# DAY REPLAY
# ─────────────────────────────────────────────────────────────────────────────

def replay_day(conn: sqlite3.Connection, trading_date: str) -> tuple:
    """
    Replay one trading day from stored database records.
    Returns (day_dict, iv_crush_by_strategy_dict)
    """
    # ── Fetch all data for this day ───────────────────────────────────────
    cycle_rows = q(conn,
        "SELECT * FROM cycle_log WHERE trading_date=? ORDER BY cycle_id",
        (trading_date,))

    trade_entries = q(conn,
        "SELECT * FROM trade_entries WHERE trading_date=? ORDER BY entry_time",
        (trading_date,))

    trade_exits = []
    if table_exists(conn, "trade_exits"):
        trade_exits = q(conn,
            """SELECT te.* FROM trade_exits te
               JOIN positions p ON te.position_id = p.position_id
               WHERE p.trading_date=? ORDER BY te.exit_time""",
            (trading_date,))

    regime_decisions = []
    if table_exists(conn, "regime_decisions"):
        regime_decisions = q(conn,
            "SELECT * FROM regime_decisions WHERE date=? ORDER BY timestamp",
            (trading_date,))

    vix_rows = []
    if table_exists(conn, "vix_history"):
        vix_rows = q(conn,
            "SELECT * FROM vix_history WHERE date=? ORDER BY timestamp",
            (trading_date,))

    phantom_rows = []
    if table_exists(conn, "phantom_trades"):
        phantom_rows = q(conn,
            "SELECT * FROM phantom_trades WHERE trading_date=? ORDER BY block_time",
            (trading_date,))

    session_state = None
    if table_exists(conn, "session_state"):
        session_state = q1(conn,
            "SELECT * FROM session_state WHERE trading_date=?",
            (trading_date,))

    # ── Build exits lookup ────────────────────────────────────────────────
    exits_by_pos: Dict[str, dict] = {e["position_id"]: e for e in trade_exits}

    # ── Extract key signals ───────────────────────────────────────────────
    vrp_vals  = [c["vrp_smoothed"] or c.get("vrp_raw")
                 for c in cycle_rows
                 if (c.get("vrp_smoothed") or c.get("vrp_raw")) is not None]
    vix_vals  = [r["vix_value"] for r in vix_rows if r.get("vix_value")]
    or_cond   = next((c["or_condition"] for c in cycle_rows if c.get("or_condition")), "UNKNOWN")

    # Dominant regimes
    vol_counts:   dict = {}
    price_counts: dict = {}
    final_counts: dict = {}
    for c in cycle_rows:
        vr = c.get("vol_regime")
        pr = c.get("price_regime")
        fr = c.get("final_regime")
        if vr:
            vol_counts[vr]   = vol_counts.get(vr, 0) + 1
        if pr:
            price_counts[pr] = price_counts.get(pr, 0) + 1
        if fr and fr not in ("NO_TRADE", "SIGNAL_ONLY", None):
            final_counts[fr] = final_counts.get(fr, 0) + 1

    dominant_vol   = max(vol_counts,   key=lambda k: vol_counts[k])   if vol_counts   else "UNKNOWN"
    dominant_price = max(price_counts, key=lambda k: price_counts[k]) if price_counts else "UNKNOWN"
    dominant_final = max(final_counts, key=lambda k: final_counts[k]) if final_counts else "NO_TRADE"

    # ── Initialise day dict ───────────────────────────────────────────────
    day: dict = {
        "trading_date":          trading_date,
        "day_label":             session_state.get("day_label") if session_state else None,
        "cycles":                len(cycle_rows),
        "regime_decisions":      len(regime_decisions),
        "trades_entered":        len(trade_entries),
        "trades_closed":         len(trade_exits),
        "wins":                  0,
        "losses":                0,
        "breakevens":            0,
        "gross_pnl_rupees":      0.0,
        "total_costs_rupees":    0.0,
        "net_pnl_rupees":        0.0,
        "net_pnl_pct":           0.0,
        "avg_hold_minutes":      0.0,
        "stops_fired":           0,
        "target_exits":          0,
        "time_exits":            0,
        "delta_breach_exits":    0,
        "spot_proximity_exits":  0,
        "price_stop_exits":      0,
        "profit_lock_exits":     0,
        "cheap_buyback_exits":   0,
        "iv_crush_trades":       0,
        "strategies_used":       {},
        "vol_regime_performance":  {},
        "price_regime_performance":{},
        "final_regime_performance":{},
        "vrp_condition_performance":{},
        "dte_performance":       {0: {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0},
                                   1: {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0},
                                   2: {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}},
        "vix_open":              vix_vals[0]  if vix_vals else None,
        "vix_close":             vix_vals[-1] if vix_vals else None,
        "vrp_mean":              round(statistics.mean(vrp_vals), 3) if vrp_vals else None,
        "or_condition":          or_cond,
        "dominant_vol_regime":   dominant_vol,
        "dominant_price_regime": dominant_price,
        "dominant_final_regime": dominant_final,
        "phantom_trades_blocked":len(phantom_rows),
        "phantom_would_have_won":sum(1 for p in phantom_rows
                                     if p.get("would_have_been_profitable")),
        "trade_details":         [],
    }

    hold_mins:   list = []
    entry_vrps:  list = []
    day_iv_crush_by_strategy: dict = {}

    # ── Process each trade ────────────────────────────────────────────────
    for t in trade_entries:
        pid      = t.get("position_id")
        ex       = exits_by_pos.get(pid)
        strat    = str(t.get("strategy_name") or "UNKNOWN")
        vol_reg  = str(t.get("vol_regime_at_entry") or "UNKNOWN")
        price_reg = str(t.get("price_regime_at_entry") or "UNKNOWN")
        final_reg = str(t.get("final_regime_at_entry") or "UNKNOWN")
        vol_cond = str(t.get("vol_regime_at_entry") or "UNKNOWN")
        entry_vrp = t.get("entry_vrp_smoothed") or t.get("entry_vrp")

        # Initialise aggregation buckets
        for bucket_key, bucket_dict in [
            (strat,     day["strategies_used"]),
            (vol_reg,   day["vol_regime_performance"]),
            (price_reg, day["price_regime_performance"]),
            (final_reg, day["final_regime_performance"]),
            (vol_cond,  day["vrp_condition_performance"]),
        ]:
            if bucket_key not in bucket_dict:
                bucket_dict[bucket_key] = {
                    "count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0
                }
            bucket_dict[bucket_key]["count"] += 1

        if entry_vrp is not None:
            entry_vrps.append(float(entry_vrp))

        # Build trade detail
        detail: dict = {
            "position_id":           pid,
            "strategy_name":         strat,
            "entry_time":            t.get("entry_time"),
            "entry_spot":            t.get("entry_spot"),
            "entry_vix":             t.get("entry_vix"),
            "entry_vrp":             entry_vrp,
            "entry_credit":          t.get("entry_credit"),
            "gross_credit":          t.get("gross_credit"),
            "opening_straddle":      t.get("opening_straddle_at_entry"),
            "final_lots":            t.get("final_lots"),
            "actual_dte":            t.get("actual_dte"),
            "vol_regime":            vol_reg,
            "price_regime":          price_reg,
            "final_regime":          final_reg,
            "confidence":            t.get("confidence_level_at_entry"),
            "confidence_score":      t.get("confidence_score_at_entry"),
            "or_condition":          t.get("or_condition"),
            "iv_behavior":           t.get("iv_behavior"),
            "day_move_used_at_entry":t.get("day_move_used_at_entry"),
            "is_borderline_sell":    t.get("is_borderline_sell"),
            "calibration_tier":      t.get("calibration_tier_at_entry"),
            "exit_time":             None,
            "exit_reason":           None,
            "exit_priority":         None,
            "hold_minutes":          None,
            "gross_pnl_rupees":      None,
            "total_costs_rupees":    None,
            "net_pnl_rupees":        None,
            "net_pnl_pct_credit":    None,
            "result":                None,
            "actual_slippage_pts":   None,
        }

        if ex:
            net_pnl   = float(ex.get("net_pnl_rupees") or 0)
            gross_pnl = float(ex.get("gross_pnl_rupees") or 0)
            tot_costs = float(ex.get("total_costs_rupees") or 0)
            result    = str(ex.get("result") or "UNKNOWN")
            hold_min  = float(ex.get("hold_minutes") or 0)
            exit_rsn  = str(ex.get("exit_reason") or "")
            exit_pri  = int(ex.get("exit_priority") or 0)

            # Accumulate day totals
            day["gross_pnl_rupees"]   += gross_pnl
            day["total_costs_rupees"] += tot_costs
            day["net_pnl_rupees"]     += net_pnl

            # Win/loss tracking
            if result == "WIN":
                day["wins"] += 1
                for bd in [
                    day["strategies_used"][strat],
                    day["vol_regime_performance"][vol_reg],
                    day["price_regime_performance"][price_reg],
                    day["final_regime_performance"][final_reg],
                    day["vrp_condition_performance"][vol_cond],
                ]:
                    bd["wins"] += 1
            elif result == "LOSS":
                day["losses"] += 1
                for bd in [
                    day["strategies_used"][strat],
                    day["vol_regime_performance"][vol_reg],
                    day["price_regime_performance"][price_reg],
                    day["final_regime_performance"][final_reg],
                    day["vrp_condition_performance"][vol_cond],
                ]:
                    bd["losses"] += 1
            else:
                day["breakevens"] += 1

            # P&L accumulation in buckets
            for bd in [
                day["strategies_used"][strat],
                day["vol_regime_performance"][vol_reg],
                day["price_regime_performance"][price_reg],
                day["final_regime_performance"][final_reg],
                day["vrp_condition_performance"][vol_cond],
            ]:
                bd["net_pnl"] += net_pnl

            # Exit reason classification
            if exit_rsn == "CLOSE_STOP":
                day["stops_fired"] += 1
                if exit_pri == 1:
                    day["delta_breach_exits"] += 1
                elif exit_pri == 2:
                    day["spot_proximity_exits"] += 1
                elif exit_pri == 3:
                    day["price_stop_exits"] += 1
            elif exit_rsn == "CLOSE_TARGET":
                day["target_exits"] += 1
                if exit_pri == 4:
                    day["profit_lock_exits"] += 1
                elif exit_pri == 5:
                    day["cheap_buyback_exits"] += 1
            elif exit_rsn in ("CLOSE_TIME", "HARD_EXIT_15:00", "EOD_CLOSE"):
                day["time_exits"] += 1

            hold_mins.append(hold_min)

            # IV crush detection
            entry_iv = t.get("entry_atm_iv") or t.get("entry_vrp")
            if entry_iv and float(entry_iv) > 0:
                exit_cycles = [
                    c for c in cycle_rows
                    if str(c.get("cycle_time", "")) >= str(ex.get("exit_time", ""))
                ]
                if exit_cycles:
                    exit_iv_raw = (
                        exit_cycles[0].get("atm_iv_pct") or
                        exit_cycles[0].get("vrp_smoothed")
                    )
                    if exit_iv_raw and float(exit_iv_raw) < float(entry_iv):
                        day["iv_crush_trades"] += 1
                        if strat not in day_iv_crush_by_strategy:
                            day_iv_crush_by_strategy[strat] = {"crush_count": 0, "total": 0}
                        day_iv_crush_by_strategy[strat]["crush_count"] += 1

            if strat not in day_iv_crush_by_strategy:
                day_iv_crush_by_strategy[strat] = {"crush_count": 0, "total": 0}
            day_iv_crush_by_strategy[strat]["total"] += 1

            # DTE performance
            dte_bucket = min(int(t.get("actual_dte") or 1), 2)
            if dte_bucket not in day["dte_performance"]:
                day["dte_performance"][dte_bucket] = {
                    "count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0
                }
            day["dte_performance"][dte_bucket]["count"]   += 1
            day["dte_performance"][dte_bucket]["net_pnl"] += net_pnl
            if result == "WIN":
                day["dte_performance"][dte_bucket]["wins"]   += 1
            elif result == "LOSS":
                day["dte_performance"][dte_bucket]["losses"] += 1

            # Slippage analysis
            actual_slip = 0.0
            if table_exists(conn, "position_legs"):
                legs = q(conn,
                    "SELECT quoted_mid_at_entry, quoted_mid_at_exit, "
                    "entry_price, exit_price, action "
                    "FROM position_legs WHERE position_id=?",
                    (pid,))
                for leg in legs:
                    qme = float(leg.get("quoted_mid_at_entry") or 0)
                    ep  = float(leg.get("entry_price") or 0)
                    qmx = float(leg.get("quoted_mid_at_exit") or 0)
                    xp  = float(leg.get("exit_price") or 0)
                    if qme > 0 and ep > 0:
                        actual_slip += abs(ep - qme)
                    if qmx > 0 and xp > 0:
                        actual_slip += abs(xp - qmx)

            entry_credit = float(t.get("entry_credit") or t.get("entry_debit") or 0)
            net_pnl_pts  = float(ex.get("net_pnl_pts") or 0)

            detail.update({
                "exit_time":          ex.get("exit_time"),
                "exit_reason":        exit_rsn,
                "exit_priority":      exit_pri,
                "hold_minutes":       hold_min,
                "gross_pnl_rupees":   gross_pnl,
                "total_costs_rupees": tot_costs,
                "net_pnl_rupees":     net_pnl,
                "net_pnl_pct_credit": round(net_pnl_pts / entry_credit * 100, 1)
                                      if entry_credit > 0 else None,
                "result":             result,
                "actual_slippage_pts":round(actual_slip, 3),
            })

        day["trade_details"].append(detail)

    # ── Day-level aggregation ─────────────────────────────────────────────
    if hold_mins:
        day["avg_hold_minutes"] = round(statistics.mean(hold_mins), 1)
    if entry_vrps:
        day["avg_entry_vrp"] = round(statistics.mean(entry_vrps), 3)

    total_t = day["wins"] + day["losses"] + day["breakevens"]
    day["win_rate_pct"] = round(day["wins"] / total_t * 100, 1) if total_t else 0.0
    day["net_pnl_pct"]  = round(day["net_pnl_rupees"] / STARTING_CAPITAL * 100, 3)

    # Profit factor for the day
    gw = sum(float(e.get("net_pnl_rupees") or 0)
             for e in trade_exits if (e.get("net_pnl_rupees") or 0) > 0)
    gl = abs(sum(float(e.get("net_pnl_rupees") or 0)
                 for e in trade_exits if (e.get("net_pnl_rupees") or 0) < 0))
    day["profit_factor"] = round(gw / gl, 3) if gl > 0 else None

    # Cost efficiency
    if day["gross_pnl_rupees"] > 0:
        day["cost_efficiency_pct"] = round(
            day["total_costs_rupees"] / day["gross_pnl_rupees"] * 100, 1
        )
    else:
        day["cost_efficiency_pct"] = None

    return day, day_iv_crush_by_strategy


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def add_win_rate_and_avg(d: dict) -> dict:
    """Enrich a performance dict with win_rate_pct and avg_pnl."""
    result: dict = {}
    for k, v in sorted(d.items(), key=lambda x: -x[1].get("net_pnl", 0)):
        enriched = dict(v)
        n = v.get("count", 0)
        enriched["win_rate_pct"] = round(v["wins"] / n * 100, 1) if n else 0.0
        enriched["avg_pnl"]      = round(v["net_pnl"] / n, 2)    if n else 0.0
        result[k] = enriched
    return result


def aggregate_performance_dicts(all_days: list, key: str) -> dict:
    """Aggregate a performance sub-dict across all days."""
    agg: dict = {}
    for day in all_days:
        for bucket, vals in day.get(key, {}).items():
            if bucket not in agg:
                agg[bucket] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
            for f in ["count", "wins", "losses"]:
                agg[bucket][f] += vals.get(f, 0)
            agg[bucket]["net_pnl"] += vals.get("net_pnl", 0.0)
    return agg


def aggregate_dte_performance(all_days: list) -> dict:
    """Aggregate DTE performance across all days."""
    agg: dict = {}
    for day in all_days:
        for dte_k, dte_v in day.get("dte_performance", {}).items():
            if dte_k not in agg:
                agg[dte_k] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
            for f in ["count", "wins", "losses"]:
                agg[dte_k][f] += dte_v.get(f, 0)
            agg[dte_k]["net_pnl"] += dte_v.get("net_pnl", 0.0)
    return agg


# ─────────────────────────────────────────────────────────────────────────────
# PAYOFF GEOMETRY
# ─────────────────────────────────────────────────────────────────────────────

def compute_payoff_geometry(all_days: list) -> dict:
    """Compute payoff geometry metrics across all trades."""
    all_trades = [
        td for day in all_days
        for td in day.get("trade_details", [])
        if td.get("net_pnl_rupees") is not None
    ]

    wins   = [td for td in all_trades if (td.get("net_pnl_rupees") or 0) > 0]
    losses = [td for td in all_trades if (td.get("net_pnl_rupees") or 0) < 0]

    n_wins   = max(len(wins),   1)
    n_losses = max(len(losses), 1)

    avg_win_rs  = sum(float(t["net_pnl_rupees"]) for t in wins)   / n_wins
    avg_loss_rs = abs(sum(float(t["net_pnl_rupees"]) for t in losses)) / n_losses

    avg_win_pts  = avg_win_rs  / LOT_SIZE
    avg_loss_pts = avg_loss_rs / LOT_SIZE

    reward_to_risk = round(avg_win_pts / avg_loss_pts, 3) if avg_loss_pts > 0 else None

    # Break-even win rate required
    if reward_to_risk and reward_to_risk > 0:
        be_wr = round(1 / (1 + reward_to_risk) * 100, 1)
    else:
        be_wr = None

    # Actual slippage
    slips = [
        float(td.get("actual_slippage_pts") or 0)
        for td in all_trades
        if td.get("actual_slippage_pts") is not None
    ]
    avg_actual_slip = round(statistics.mean(slips), 3) if slips else None

    # Hold time distribution
    hold_times = [
        float(td.get("hold_minutes") or 0)
        for td in all_trades
        if td.get("hold_minutes") is not None
    ]

    # Credit-to-range ratio
    credit_ratios = []
    for td in all_trades:
        credit  = float(td.get("entry_credit") or td.get("gross_credit") or 0)
        straddle = float(td.get("opening_straddle") or 0)
        if credit > 0 and straddle > 0:
            credit_ratios.append(credit / straddle)

    return {
        "total_trades":              len(all_trades),
        "total_wins":                len(wins),
        "total_losses":              len(losses),
        "avg_win_pts":               round(avg_win_pts, 3),
        "avg_loss_pts":              round(avg_loss_pts, 3),
        "avg_win_rupees":            round(avg_win_rs, 2),
        "avg_loss_rupees":           round(avg_loss_rs, 2),
        "reward_to_risk":            reward_to_risk,
        "break_even_win_rate_pct":   be_wr,
        "avg_actual_slippage_pts":   avg_actual_slip,
        "avg_hold_minutes":          round(statistics.mean(hold_times), 1) if hold_times else None,
        "median_hold_minutes":       safe_median(hold_times),
        "p90_hold_minutes":          safe_percentile(hold_times, 90),
        "avg_credit_to_straddle_pct":round(statistics.mean(credit_ratios) * 100, 2)
                                     if credit_ratios else None,
        "win_pnl_series":            [round(float(t["net_pnl_rupees"]), 2) for t in wins],
        "loss_pnl_series":           [round(float(t["net_pnl_rupees"]), 2) for t in losses],
    }


# ─────────────────────────────────────────────────────────────────────────────
# CALIBRATION DRIFT ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def compute_calibration_drift(conn: sqlite3.Connection) -> dict:
    """Analyse calibration drift over time."""
    if not table_exists(conn, "calibration_state"):
        return {"available": False}

    rows = q(conn,
        "SELECT * FROM calibration_state ORDER BY calibrated_at DESC LIMIT 30")
    if not rows:
        return {"available": False, "rows": 0}

    vrp_sells = [float(r["vrp_sell_threshold"]) for r in rows
                 if r.get("vrp_sell_threshold")]
    vix_p50s  = [float(r["vix_p50"]) for r in rows if r.get("vix_p50")]
    tue_sizes = [float(r["day_size_tuesday"]) for r in rows
                 if r.get("day_size_tuesday")]

    def drift_pct(series: list) -> Optional[float]:
        if len(series) < 2:
            return None
        return round(abs(series[0] - series[-1]) / series[-1] * 100, 1) if series[-1] else None

    return {
        "available":              True,
        "n_calibration_runs":     len(rows),
        "latest_tier":            rows[0].get("calibration_tier", 0) if rows else 0,
        "latest_valid":           bool(rows[0].get("is_valid")) if rows else False,
        "latest_vrp_sell":        rows[0].get("vrp_sell_threshold") if rows else None,
        "latest_vix_p50":         rows[0].get("vix_p50") if rows else None,
        "latest_day_size_tuesday":rows[0].get("day_size_tuesday") if rows else None,
        "vrp_sell_drift_pct":     drift_pct(vrp_sells),
        "vix_p50_drift_pct":      drift_pct(vix_p50s),
        "tue_size_drift_pct":     drift_pct(tue_sizes),
        "vrp_sell_history":       vrp_sells[:10],
        "vix_p50_history":        vix_p50s[:10],
        "phantom_fnr_latest":     rows[0].get("phantom_false_negative_rate") if rows else None,
        "exit_quality_latest":    rows[0].get("exit_quality_score") if rows else None,
        "regime_accuracy_latest": rows[0].get("regime_accuracy_score") if rows else None,
        "recent_calibrations":    [
            {
                "calibrated_at":    r.get("calibrated_at"),
                "tier":             r.get("calibration_tier"),
                "valid":            bool(r.get("is_valid")),
                "vrp_sell":         r.get("vrp_sell_threshold"),
                "vix_p50":          r.get("vix_p50"),
                "day_size_tuesday": r.get("day_size_tuesday"),
                "phantom_fnr":      r.get("phantom_false_negative_rate"),
                "exit_quality":     r.get("exit_quality_score"),
            }
            for r in rows[:10]
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHANTOM TRADE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def compute_phantom_analysis(conn: sqlite3.Connection, all_dates: list) -> dict:
    """Analyse phantom trades (NEUTRAL-blocked trades) across the backtest period."""
    if not table_exists(conn, "phantom_trades"):
        return {"available": False}

    from_date = all_dates[0]  if all_dates else "2000-01-01"
    to_date   = all_dates[-1] if all_dates else "2099-12-31"

    rows = q(conn,
        "SELECT * FROM phantom_trades "
        "WHERE trading_date >= ? AND trading_date <= ? "
        "ORDER BY block_time",
        (from_date, to_date))

    if not rows:
        return {"available": True, "total_phantom_trades": 0}

    total      = len(rows)
    would_win  = sum(1 for r in rows if r.get("would_have_been_profitable"))
    fnr        = round(would_win / total * 100, 1) if total > 0 else 0.0

    avg_credit = safe_mean([float(r["credit_would_be"]) for r in rows
                            if r.get("credit_would_be")])
    avg_vrp    = safe_mean([float(r["vrp_smoothed_at_block"] or r.get("vrp_at_block") or 0)
                            for r in rows
                            if (r.get("vrp_smoothed_at_block") or r.get("vrp_at_block"))])

    # By DTE
    by_dte: dict = {}
    for r in rows:
        dte = int(r.get("dte_at_block") or 0)
        if dte not in by_dte:
            by_dte[dte] = {"total": 0, "would_win": 0}
        by_dte[dte]["total"]    += 1
        by_dte[dte]["would_win"] += int(bool(r.get("would_have_been_profitable")))

    # By strategy
    by_strat: dict = {}
    for r in rows:
        s = str(r.get("strategy_would_be") or "UNKNOWN")
        if s not in by_strat:
            by_strat[s] = {"total": 0, "would_win": 0}
        by_strat[s]["total"]    += 1
        by_strat[s]["would_win"] += int(bool(r.get("would_have_been_profitable")))

    return {
        "available":                True,
        "total_phantom_trades":     total,
        "would_have_won":           would_win,
        "false_negative_rate_pct":  fnr,
        "avg_credit_would_be":      avg_credit,
        "avg_vrp_at_block":         avg_vrp,
        "interpretation": (
            "Threshold too tight — lower VRP sell threshold"
            if fnr > 30 else (
                "Threshold may be too low — raise VRP sell threshold"
                if fnr < 10 and total >= 20 else
                "Threshold appropriate (10-30% FNR)"
            )
        ),
        "by_dte": {
            str(k): {
                **v,
                "fnr_pct": round(v["would_win"] / v["total"] * 100, 1)
                           if v["total"] > 0 else 0.0,
            }
            for k, v in sorted(by_dte.items())
        },
        "by_strategy": {
            k: {
                **v,
                "fnr_pct": round(v["would_win"] / v["total"] * 100, 1)
                           if v["total"] > 0 else 0.0,
            }
            for k, v in sorted(by_strat.items(), key=lambda x: -x[1]["total"])
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXIT QUALITY ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def compute_exit_quality_analysis(conn: sqlite3.Connection, all_dates: list) -> dict:
    """Analyse exit quality across the backtest period."""
    if not table_exists(conn, "exit_quality_log"):
        return {"available": False}

    from_date = all_dates[0]  if all_dates else "2000-01-01"
    to_date   = all_dates[-1] if all_dates else "2099-12-31"

    rows = q(conn,
        "SELECT * FROM exit_quality_log "
        "WHERE trading_date >= ? AND trading_date <= ?",
        (from_date, to_date))

    if not rows:
        return {"available": True, "total_exits": 0}

    total      = len(rows)
    premature  = sum(1 for r in rows if r.get("was_exit_premature"))
    late       = sum(1 for r in rows if r.get("was_exit_late"))

    improvements = [
        float(r["pnl_15min_after_exit"]) - float(r["exit_pnl_rupees"])
        for r in rows
        if r.get("pnl_15min_after_exit") is not None
        and r.get("exit_pnl_rupees") is not None
    ]

    # By exit priority
    by_priority: dict = {}
    for r in rows:
        pri = int(r.get("exit_priority_fired") or 0)
        if pri not in by_priority:
            by_priority[pri] = {"count": 0, "premature": 0, "avg_improvement": []}
        by_priority[pri]["count"] += 1
        if r.get("was_exit_premature"):
            by_priority[pri]["premature"] += 1
        if (r.get("pnl_15min_after_exit") is not None and
                r.get("exit_pnl_rupees") is not None):
            by_priority[pri]["avg_improvement"].append(
                float(r["pnl_15min_after_exit"]) - float(r["exit_pnl_rupees"])
            )

    priority_names = {
        1: "DELTA_BREACH", 2: "SPOT_PROXIMITY", 3: "PRICE_STOP",
        4: "PROFIT_LOCK",  5: "CHEAP_BUYBACK",  6: "TIME_TARGET",
        7: "HARD_EXIT",
    }

    return {
        "available":              True,
        "total_exits":            total,
        "premature_exits":        premature,
        "late_exits":             late,
        "premature_rate_pct":     round(premature / total * 100, 1) if total else 0.0,
        "late_rate_pct":          round(late / total * 100, 1) if total else 0.0,
        "avg_improvement_15min":  round(statistics.mean(improvements), 2)
                                  if improvements else None,
        "exit_quality_score":     round(100 - premature / total * 100, 1) if total else None,
        "interpretation": (
            "Exits too early on average — consider later time targets"
            if improvements and statistics.mean(improvements) > 500 else (
                "Exits well-timed"
                if improvements and statistics.mean(improvements) < -200 else
                "Exit timing appropriate"
            )
        ),
        "by_priority": {
            str(pri): {
                "name":           priority_names.get(pri, f"PRIORITY_{pri}"),
                "count":          vals["count"],
                "premature":      vals["premature"],
                "premature_pct":  round(vals["premature"] / vals["count"] * 100, 1)
                                  if vals["count"] else 0.0,
                "avg_improvement":round(statistics.mean(vals["avg_improvement"]), 2)
                                  if vals["avg_improvement"] else None,
            }
            for pri, vals in sorted(by_priority.items())
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# REGIME ACCURACY ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def compute_regime_accuracy_analysis(
    conn: sqlite3.Connection, all_dates: list
) -> dict:
    """Analyse regime classification accuracy across the backtest period."""
    if not table_exists(conn, "regime_accuracy_scores"):
        return {"available": False}

    from_date = all_dates[0]  if all_dates else "2000-01-01"
    to_date   = all_dates[-1] if all_dates else "2099-12-31"

    rows = q(conn,
        "SELECT * FROM regime_accuracy_scores "
        "WHERE trading_date >= ? AND trading_date <= ?",
        (from_date, to_date))

    if not rows:
        return {"available": True, "total_decisions": 0}

    total = len(rows)
    scores = [float(r["score_value"]) for r in rows if r.get("score_value") is not None]

    vol_correct   = sum(1 for r in rows if r.get("was_vol_correct") == 1)
    price_correct = sum(1 for r in rows if r.get("was_price_correct") == 1)
    final_correct = sum(1 for r in rows if r.get("was_final_correct") == 1)

    vol_n   = sum(1 for r in rows if r.get("was_vol_correct")   is not None)
    price_n = sum(1 for r in rows if r.get("was_price_correct") is not None)
    final_n = sum(1 for r in rows if r.get("was_final_correct") is not None)

    return {
        "available":              True,
        "total_decisions":        total,
        "avg_score":              round(statistics.mean(scores), 3) if scores else None,
        "vol_regime_accuracy":    round(vol_correct / vol_n * 100, 1)   if vol_n   else None,
        "price_regime_accuracy":  round(price_correct / price_n * 100, 1) if price_n else None,
        "final_regime_accuracy":  round(final_correct / final_n * 100, 1) if final_n else None,
        "interpretation": (
            "Regime engine highly accurate" if scores and statistics.mean(scores) >= 0.70 else (
                "Regime engine moderately accurate"
                if scores and statistics.mean(scores) >= 0.50 else
                "Regime engine needs improvement"
            )
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN BACKTEST FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def run_walkforward_backtest(
    from_date: Optional[str] = None,
    to_date:   Optional[str] = None,
) -> Optional[dict]:
    """
    Run walk-forward backtest over all available trading dates.

    Args:
        from_date: Start date (YYYY-MM-DD), optional
        to_date:   End date (YYYY-MM-DD), optional

    Returns:
        Summary dict with all performance metrics, or None on error.
    """
    conn = get_connection()

    if not table_exists(conn, "trade_entries"):
        print("No trade_entries table. Run engine first.")
        conn.close()
        return None

    # ── Get all trading dates ─────────────────────────────────────────────
    all_dates = [
        r["trading_date"] for r in q(conn,
            "SELECT DISTINCT trading_date FROM trade_entries "
            "ORDER BY trading_date")
    ]
    if from_date:
        all_dates = [d for d in all_dates if d >= from_date]
    if to_date:
        all_dates = [d for d in all_dates if d <= to_date]

    if not all_dates:
        print("No trading dates found in the specified range.")
        conn.close()
        return None

    print(f"\nWalk-forward backtest: {len(all_dates)} trading days "
          f"({all_dates[0]} to {all_dates[-1]})")
    print()

    # ── Replay each day ───────────────────────────────────────────────────
    all_days:           list  = []
    capital             = STARTING_CAPITAL
    peak_capital        = STARTING_CAPITAL
    capital_series:     list  = [STARTING_CAPITAL]
    cum_pnl             = 0.0
    consec_losses       = 0
    max_consec_losses   = 0
    consec_wins         = 0
    max_consec_wins     = 0
    iv_crush_by_strategy: dict = {}

    for d in all_dates:
        day, day_iv_crush = replay_day(conn, d)

        day["capital_start"] = capital
        capital += day["net_pnl_rupees"]
        day["capital_end"]   = capital
        cum_pnl += day["net_pnl_rupees"]
        day["cumulative_pnl"] = round(cum_pnl, 2)
        capital_series.append(capital)

        if capital > peak_capital:
            peak_capital = capital

        # Consecutive loss/win tracking
        if day["losses"] > 0 and day["wins"] == 0:
            consec_losses += 1
            max_consec_losses = max(max_consec_losses, consec_losses)
            consec_wins = 0
        elif day["wins"] > 0 and day["losses"] == 0:
            consec_wins += 1
            max_consec_wins = max(max_consec_wins, consec_wins)
            consec_losses = 0
        else:
            consec_losses = 0
            consec_wins   = 0

        # IV crush aggregation
        for strat, vals in day_iv_crush.items():
            if strat not in iv_crush_by_strategy:
                iv_crush_by_strategy[strat] = {"crush_count": 0, "total": 0}
            iv_crush_by_strategy[strat]["crush_count"] += vals.get("crush_count", 0)
            iv_crush_by_strategy[strat]["total"]       += vals.get("total", 0)

        all_days.append(day)

        # Print day summary
        status   = "WIN" if day["net_pnl_rupees"] > 0 else (
                   "LOSS" if day["net_pnl_rupees"] < 0 else "FLAT")
        or_str   = str(day.get("or_condition") or "?")[:12]
        vix_str  = str(day.get("vix_open") or "?")[:5]
        vrp_str  = str(day.get("vrp_mean") or "?")[:6]
        vol_str  = str(day.get("dominant_vol_regime") or "?")[:20]
        price_str = str(day.get("dominant_price_regime") or "?")[:15]

        print(
            f"  {d} [{or_str:<12}] VIX={vix_str:<5} VRP={vrp_str:<6} "
            f"Vol={vol_str:<20} Price={price_str:<15} "
            f"T={day['trades_entered']:2d} "
            f"P&L=Rs{day['net_pnl_rupees']:8.0f} [{status}] "
            f"Cap=Rs{capital:,.0f}"
        )

    conn_for_analysis = get_connection()

    # ── Overall performance metrics ───────────────────────────────────────
    total_days    = len(all_days)
    prof_days     = sum(1 for d in all_days if d["net_pnl_rupees"] > 0)
    loss_days     = sum(1 for d in all_days if d["net_pnl_rupees"] < 0)
    flat_days     = total_days - prof_days - loss_days
    total_trades  = sum(d["trades_entered"] for d in all_days)
    total_wins    = sum(d["wins"]   for d in all_days)
    total_losses  = sum(d["losses"] for d in all_days)
    total_gross   = sum(d["gross_pnl_rupees"]   for d in all_days)
    total_costs   = sum(d["total_costs_rupees"] for d in all_days)
    total_net     = sum(d["net_pnl_rupees"]     for d in all_days)
    total_stops   = sum(d["stops_fired"]        for d in all_days)
    total_tgts    = sum(d["target_exits"]       for d in all_days)
    total_time    = sum(d["time_exits"]         for d in all_days)
    total_iv_crush = sum(d["iv_crush_trades"]   for d in all_days)
    total_phantom  = sum(d.get("phantom_trades_blocked", 0) for d in all_days)
    total_phantom_win = sum(d.get("phantom_would_have_won", 0) for d in all_days)

    # Exit priority breakdown
    total_delta_breach   = sum(d.get("delta_breach_exits", 0)   for d in all_days)
    total_spot_prox      = sum(d.get("spot_proximity_exits", 0) for d in all_days)
    total_price_stop     = sum(d.get("price_stop_exits", 0)     for d in all_days)
    total_profit_lock    = sum(d.get("profit_lock_exits", 0)    for d in all_days)
    total_cheap_buyback  = sum(d.get("cheap_buyback_exits", 0)  for d in all_days)

    gw_total = sum(d["net_pnl_rupees"] for d in all_days if d["net_pnl_rupees"] > 0)
    gl_total = abs(sum(d["net_pnl_rupees"] for d in all_days if d["net_pnl_rupees"] < 0))
    overall_pf = round(gw_total / gl_total, 3) if gl_total > 0 else None
    trade_wr   = round(total_wins / (total_wins + total_losses) * 100, 1) \
                 if (total_wins + total_losses) > 0 else 0.0

    # Drawdown
    max_dd_rs, max_dd_pct, dd_start_idx, dd_end_idx = compute_max_drawdown(capital_series)

    # Daily P&L series for risk metrics
    daily_pnl_series = [d["net_pnl_rupees"] for d in all_days]
    sharpe   = compute_sharpe(daily_pnl_series)
    sortino  = compute_sortino(daily_pnl_series)
    total_return_pct = round(total_net / STARTING_CAPITAL * 100, 3)
    calmar   = compute_calmar(total_return_pct, max_dd_pct)

    # ── Aggregate performance dicts ───────────────────────────────────────
    strategy_agg    = aggregate_performance_dicts(all_days, "strategies_used")
    vol_regime_agg  = aggregate_performance_dicts(all_days, "vol_regime_performance")
    price_regime_agg = aggregate_performance_dicts(all_days, "price_regime_performance")
    final_regime_agg = aggregate_performance_dicts(all_days, "final_regime_performance")
    vrp_cond_agg    = aggregate_performance_dicts(all_days, "vrp_condition_performance")
    dte_agg         = aggregate_dte_performance(all_days)

    # ── Advanced analyses ─────────────────────────────────────────────────
    payoff_geometry   = compute_payoff_geometry(all_days)
    cal_drift         = compute_calibration_drift(conn_for_analysis)
    phantom_analysis  = compute_phantom_analysis(conn_for_analysis, all_dates)
    exit_quality      = compute_exit_quality_analysis(conn_for_analysis, all_dates)
    regime_accuracy   = compute_regime_accuracy_analysis(conn_for_analysis, all_dates)

    conn_for_analysis.close()

    # ── OR condition analysis ─────────────────────────────────────────────
    or_perf: dict = {}
    for day in all_days:
        or_c = str(day.get("or_condition") or "UNKNOWN")
        if or_c not in or_perf:
            or_perf[or_c] = {"days": 0, "trade_days": 0, "net_pnl": 0.0,
                              "wins": 0, "losses": 0}
        or_perf[or_c]["days"] += 1
        if day["trades_entered"] > 0:
            or_perf[or_c]["trade_days"] += 1
        or_perf[or_c]["net_pnl"] += day["net_pnl_rupees"]
        or_perf[or_c]["wins"]    += day["wins"]
        or_perf[or_c]["losses"]  += day["losses"]

    # ── VIX regime analysis ───────────────────────────────────────────────
    vix_regime_perf: dict = {}
    for day in all_days:
        vr = str(day.get("dominant_vol_regime") or "UNKNOWN")
        if vr not in vix_regime_perf:
            vix_regime_perf[vr] = {"days": 0, "net_pnl": 0.0,
                                    "wins": 0, "losses": 0}
        vix_regime_perf[vr]["days"]    += 1
        vix_regime_perf[vr]["net_pnl"] += day["net_pnl_rupees"]
        vix_regime_perf[vr]["wins"]    += day["wins"]
        vix_regime_perf[vr]["losses"]  += day["losses"]

    # ── Day of week analysis ──────────────────────────────────────────────
    dow_perf: dict = {}
    for day in all_days:
        label = str(day.get("day_label") or "UNKNOWN")
        if label not in dow_perf:
            dow_perf[label] = {"days": 0, "trade_days": 0, "net_pnl": 0.0,
                                "wins": 0, "losses": 0}
        dow_perf[label]["days"] += 1
        if day["trades_entered"] > 0:
            dow_perf[label]["trade_days"] += 1
        dow_perf[label]["net_pnl"]  += day["net_pnl_rupees"]
        dow_perf[label]["wins"]     += day["wins"]
        dow_perf[label]["losses"]   += day["losses"]

    # ── Build summary ─────────────────────────────────────────────────────
    summary = {
        "backtest_metadata": {
            "generated_at":       datetime.now().isoformat(),
            "from_date":          all_dates[0],
            "to_date":            all_dates[-1],
            "total_trading_days": total_days,
            "starting_capital":   STARTING_CAPITAL,
            "lot_size":           LOT_SIZE,
            "engine_version":     "v3.0_regime_based",
            "nifty_context":      (
                "NIFTY weekly Tuesday expiry, suppressed VIX 2026 regime, "
                "regime-based not VIX-based"
            ),
        },

        "overall_performance": {
            "profitable_days":           prof_days,
            "loss_days":                 loss_days,
            "flat_days":                 flat_days,
            "day_win_rate_pct":          round(prof_days / total_days * 100, 1)
                                         if total_days else 0.0,
            "total_trades":              total_trades,
            "total_wins":                total_wins,
            "total_losses":              total_losses,
            "trade_win_rate_pct":        trade_wr,
            "total_gross_pnl_rupees":    round(total_gross, 2),
            "total_costs_rupees":        round(total_costs, 2),
            "total_net_pnl_rupees":      round(total_net, 2),
            "total_return_pct":          total_return_pct,
            "profit_factor":             overall_pf,
            "sharpe_ratio":              sharpe,
            "sortino_ratio":             sortino,
            "calmar_ratio":              calmar,
            "max_drawdown_rupees":       max_dd_rs,
            "max_drawdown_pct":          max_dd_pct,
            "max_consecutive_losses":    max_consec_losses,
            "max_consecutive_wins":      max_consec_wins,
            "capital_final":             round(capital, 2),
            "total_stops_fired":         total_stops,
            "total_target_exits":        total_tgts,
            "total_time_exits":          total_time,
            "exit_priority_breakdown": {
                "1_delta_breach":    total_delta_breach,
                "2_spot_proximity":  total_spot_prox,
                "3_price_stop":      total_price_stop,
                "4_profit_lock":     total_profit_lock,
                "5_cheap_buyback":   total_cheap_buyback,
                "6_time_target":     total_tgts - total_profit_lock - total_cheap_buyback,
                "7_hard_exit":       total_time,
            },
            "iv_crush_trades":           total_iv_crush,
            "iv_crush_rate_pct":         round(total_iv_crush / total_trades * 100, 1)
                                         if total_trades else 0.0,
            "cost_as_pct_gross":         round(total_costs / total_gross * 100, 1)
                                         if total_gross > 0 else None,
            "avg_daily_pnl_rupees":      round(total_net / total_days, 2)
                                         if total_days else 0.0,
            "avg_trades_per_day":        round(total_trades / total_days, 2)
                                         if total_days else 0.0,
            "stop_rate_pct":             round(total_stops / total_trades * 100, 1)
                                         if total_trades else 0.0,
            "target_rate_pct":           round(total_tgts / total_trades * 100, 1)
                                         if total_trades else 0.0,
            "phantom_trades_blocked":    total_phantom,
            "phantom_would_have_won":    total_phantom_win,
            "phantom_false_negative_rate_pct":
                round(total_phantom_win / total_phantom * 100, 1)
                if total_phantom > 0 else None,
        },

        "strategy_performance":     add_win_rate_and_avg(strategy_agg),
        "vol_regime_performance":   add_win_rate_and_avg(vol_regime_agg),
        "price_regime_performance": add_win_rate_and_avg(price_regime_agg),
        "final_regime_performance": add_win_rate_and_avg(final_regime_agg),
        "vrp_condition_performance":add_win_rate_and_avg(vrp_cond_agg),

        "dte_performance_breakdown": {
            str(k): {
                **v,
                "win_rate_pct": round(v["wins"] / v["count"] * 100, 1)
                                if v["count"] else 0.0,
                "avg_pnl":      round(v["net_pnl"] / v["count"], 2)
                                if v["count"] else 0.0,
                "label": {
                    0: "0DTE_Tuesday",
                    1: "1DTE_Monday",
                    2: "2plus_DTE_MidWeek",
                }.get(k, f"DTE_{k}"),
            }
            for k, v in sorted(dte_agg.items())
        },

        "or_condition_performance": {
            k: {
                **v,
                "win_rate_pct": round(v["wins"] / (v["wins"] + v["losses"]) * 100, 1)
                                if (v["wins"] + v["losses"]) > 0 else 0.0,
                "avg_pnl_per_trade_day": round(v["net_pnl"] / v["trade_days"], 2)
                                          if v["trade_days"] > 0 else 0.0,
            }
            for k, v in sorted(or_perf.items(), key=lambda x: -x[1]["net_pnl"])
        },

        "vix_regime_performance": {
            k: {
                **v,
                "win_rate_pct": round(v["wins"] / (v["wins"] + v["losses"]) * 100, 1)
                                if (v["wins"] + v["losses"]) > 0 else 0.0,
            }
            for k, v in sorted(vix_regime_perf.items(), key=lambda x: -x[1]["net_pnl"])
        },

        "day_of_week_performance": {
            k: {
                **v,
                "win_rate_pct": round(v["wins"] / (v["wins"] + v["losses"]) * 100, 1)
                                if (v["wins"] + v["losses"]) > 0 else 0.0,
                "avg_pnl_per_trade_day": round(v["net_pnl"] / v["trade_days"], 2)
                                          if v["trade_days"] > 0 else 0.0,
            }
            for k, v in sorted(dow_perf.items(), key=lambda x: -x[1]["net_pnl"])
        },

        "iv_crush_by_strategy": {
            k: {
                "crush_count": v.get("crush_count", 0),
                "total":       v.get("total", 0),
                "crush_rate_pct": round(
                    v.get("crush_count", 0) / v.get("total", 1) * 100, 1
                ) if v.get("total", 0) > 0 else 0.0,
            }
            for k, v in sorted(iv_crush_by_strategy.items())
        },

        "payoff_geometry_analysis":  payoff_geometry,
        "calibration_drift":         cal_drift,
        "phantom_trade_analysis":    phantom_analysis,
        "exit_quality_analysis":     exit_quality,
        "regime_accuracy_analysis":  regime_accuracy,

        "equity_curve": [
            {
                "date":           d["trading_date"],
                "capital":        d["capital_end"],
                "pnl":            d["net_pnl_rupees"],
                "cumulative_pnl": d["cumulative_pnl"],
                "day_label":      d.get("day_label"),
                "or_condition":   d.get("or_condition"),
                "vix_open":       d.get("vix_open"),
                "vrp_mean":       d.get("vrp_mean"),
                "vol_regime":     d.get("dominant_vol_regime"),
                "price_regime":   d.get("dominant_price_regime"),
            }
            for d in all_days
        ],

        "daily_results": all_days,

        "key_findings": {
            "best_strategy":     max(strategy_agg, key=lambda k: strategy_agg[k]["net_pnl"])
                                 if strategy_agg else None,
            "worst_strategy":    min(strategy_agg, key=lambda k: strategy_agg[k]["net_pnl"])
                                 if strategy_agg else None,
            "best_vol_regime":   max(vol_regime_agg, key=lambda k: vol_regime_agg[k]["net_pnl"])
                                 if vol_regime_agg else None,
            "best_price_regime": max(price_regime_agg, key=lambda k: price_regime_agg[k]["net_pnl"])
                                 if price_regime_agg else None,
            "best_or_condition": max(or_perf, key=lambda k: or_perf[k]["net_pnl"])
                                 if or_perf else None,
            "best_dow":          max(dow_perf, key=lambda k: dow_perf[k]["net_pnl"])
                                 if dow_perf else None,
            "stop_rate_pct":     round(total_stops / total_trades * 100, 1)
                                 if total_trades else 0.0,
            "target_rate_pct":   round(total_tgts / total_trades * 100, 1)
                                 if total_trades else 0.0,
            "cost_drag_pct":     round(total_costs / STARTING_CAPITAL * 100, 3),
            "iv_crush_rate_pct": round(total_iv_crush / total_trades * 100, 1)
                                 if total_trades else 0.0,
            "phantom_fnr_pct":   round(total_phantom_win / total_phantom * 100, 1)
                                 if total_phantom > 0 else None,
        },

        "nifty_specific_metrics": {
            "tuesday_0dte_trades":      sum(d["dte_performance"].get(0, {}).get("count", 0) for d in all_days),
            "tuesday_0dte_wins":        sum(d["dte_performance"].get(0, {}).get("wins", 0) for d in all_days),
            "monday_1dte_trades":       sum(d["dte_performance"].get(1, {}).get("count", 0) for d in all_days),
            "monday_1dte_wins":         sum(d["dte_performance"].get(1, {}).get("wins", 0) for d in all_days),
            "midweek_2plus_trades":     sum(d["dte_performance"].get(2, {}).get("count", 0) for d in all_days),
            "midweek_2plus_wins":       sum(d["dte_performance"].get(2, {}).get("wins", 0) for d in all_days),
            "tuesday_0dte_win_rate":    round(
                sum(d["dte_performance"].get(0, {}).get("wins", 0) for d in all_days) /
                max(sum(d["dte_performance"].get(0, {}).get("count", 0) for d in all_days), 1) * 100, 1
            ),
            "monday_1dte_win_rate":     round(
                sum(d["dte_performance"].get(1, {}).get("wins", 0) for d in all_days) /
                max(sum(d["dte_performance"].get(1, {}).get("count", 0) for d in all_days), 1) * 100, 1
            ),
            "iron_condor_pnl":          round(sum(
                v.get("net_pnl", 0) for d in all_days
                for k, v in d.get("strategies_used", {}).items() if "IRON_CONDOR" in k
            ), 2),
            "iron_butterfly_pnl":       round(sum(
                v.get("net_pnl", 0) for d in all_days
                for k, v in d.get("strategies_used", {}).items() if "IRON_BUTTERFLY" in k
            ), 2),
            "bull_put_spread_pnl":      round(sum(
                v.get("net_pnl", 0) for d in all_days
                for k, v in d.get("strategies_used", {}).items() if "BULL_PUT" in k
            ), 2),
            "bear_call_spread_pnl":     round(sum(
                v.get("net_pnl", 0) for d in all_days
                for k, v in d.get("strategies_used", {}).items() if "BEAR_CALL" in k
            ), 2),
            "range_regime_days":        sum(1 for d in all_days if d.get("dominant_price_regime") == "RANGE"),
            "uptrend_regime_days":      sum(1 for d in all_days if "UPTREND" in str(d.get("dominant_price_regime", ""))),
            "downtrend_regime_days":    sum(1 for d in all_days if "DOWNTREND" in str(d.get("dominant_price_regime", ""))),
            "sell_premium_days":        sum(1 for d in all_days if d.get("dominant_vol_regime") in ("SELL_PREMIUM", "STRONG_SELL_PREMIUM")),
            "neutral_vol_days":         sum(1 for d in all_days if d.get("dominant_vol_regime") == "NEUTRAL"),
            "abort_days":               sum(1 for d in all_days if d.get("dominant_vol_regime") == "ABORT"),
            "avg_vrp_on_trade_days":    round(statistics.mean(
                [float(d.get("vrp_mean") or 0) for d in all_days
                 if d.get("trades_entered", 0) > 0 and d.get("vrp_mean")]
            ), 3) if any(d.get("trades_entered", 0) > 0 and d.get("vrp_mean") for d in all_days) else None,
            "avg_vrp_on_no_trade_days": round(statistics.mean(
                [float(d.get("vrp_mean") or 0) for d in all_days
                 if d.get("trades_entered", 0) == 0 and d.get("vrp_mean")]
            ), 3) if any(d.get("trades_entered", 0) == 0 and d.get("vrp_mean") for d in all_days) else None,
            "total_delta_breach_exits":  total_delta_breach,
            "total_spot_proximity_exits":total_spot_prox,
            "total_price_stop_exits":    total_price_stop,
            "total_profit_lock_exits":   total_profit_lock,
            "total_cheap_buyback_exits": total_cheap_buyback,
            "pct_exits_via_stop":        round(total_stops / max(total_trades, 1) * 100, 1),
            "pct_exits_via_target":      round(total_tgts / max(total_trades, 1) * 100, 1),
            "pct_exits_via_time":        round(total_time / max(total_trades, 1) * 100, 1),
            "cost_coverage_ratio":       round(total_gross / max(total_costs, 1), 2),
            "note": "NIFTY intraday only. No overnight positions. Tuesday 0DTE primary.",
        },
        "llm_analysis_context": {
            "summary": (
                f"Walk-forward backtest of NIFTY intraday options engine v3.0 "
                f"(regime-based) over {total_days} trading days "
                f"({all_dates[0]} to {all_dates[-1]})."
            ),
            "engine_facts": {
                "lot_size": LOT_SIZE,
                "starting_capital": STARTING_CAPITAL,
                "nifty_expiry": "Weekly Tuesday — 0DTE=Tuesday 1DTE=Monday",
                "vix_environment_2026": "Suppressed 11-13 is normal not elevated",
                "session_minutes": 375,
                "hard_exit": "15:00 IST — no overnight positions",
                "strategies": ["IRON_CONDOR","IRON_BUTTERFLY",
                                "BULL_PUT_SPREAD","BEAR_CALL_SPREAD"],
                "exit_priorities": {
                    "1": "DELTA_BREACH short_delta>0.40",
                    "2": "SPOT_PROXIMITY within 40pts of short strike",
                    "3": "PRICE_STOP 0.30x opening_straddle from short",
                    "4": "PROFIT_LOCK 40pct DTE0 or 25pct DTE1plus",
                    "5": "CHEAP_BUYBACK short_leg<=2pts after 13:00",
                    "6": "TIME_TARGET credit fraction at scheduled time",
                    "7": "HARD_EXIT 15:00 flat always",
                },
                "cost_floor_2leg_pts": 1.45,
                "cost_floor_4leg_pts": 2.91,
                "note": "All trades intraday only. No overnight. No multiday.",
            },
            "dte_performance_context": {
                "dte0_tuesday": "Expiring contract — fastest theta — primary",
                "dte1_monday": "Expiring next day — good theta — secondary",
                "dte2_wednesday": "5 days to expiry — moderate theta",
                "dte3_thursday": "4 days to expiry — lower theta",
                "dte4_friday": "3 days to expiry — low theta",
                "dte5_monday_next": "2 days to expiry — minimal theta",
                "dte6_tuesday_next": "1 day to expiry — next week",
                "warning": "DTE0 bucket must only contain expiring-day contracts",
            },
            "key_questions": [
                "Was the regime engine accurate? (check regime_accuracy_analysis)",
                "Is the VRP sell threshold optimal? (check phantom_trade_analysis)",
                "Are exits happening at the right time? (check exit_quality_analysis)",
                "Which vol regime produced the best risk-adjusted returns?",
                "Is the profit factor sustainable given the sample size?",
                "Are transaction costs below 15% of gross P&L?",
                "Which OR condition had the best win rate?",
                "Is the Tuesday 0DTE entry window producing better results than DTE1+?",
                "Are calibrated day sizes improving performance vs defaults?",
                "What is the win rate breakdown by vol regime?",
                "Is IRON_CONDOR outperforming directional spreads in RANGE regime?",
                "What percentage of trades were exited via stop vs target vs time?",
                "Are borderline sell trades profitable enough to justify the lower threshold?",
                "Is the straddle ratio at entry predictive of trade outcome?",
                "What is the average hold time by strategy?",
                "Are there specific OR conditions that produce better outcomes?",
                "Is the price regime classification (RANGE/UPTREND/DOWNTREND) accurate?",
                "Did the phantom trade analysis suggest the NEUTRAL threshold is correct?",
                "Are exits via PROFIT_LOCK (priority 4) well-timed?",
                "Is the CHEAP_BUYBACK exit (priority 5) adding value?",
                "Were any DTE0 records contaminated with non-expiring contracts?",
                "Did Parkinson RV spikes cause false BUY_OPTIONS regime on flat days?",
                "What was the EV gate rejection rate and at what credit levels?",
                "Were multi-day (DTE 2-6) trades profitable vs 0DTE trades?",
                "Was the day_move_used gate correctly calibrated to actual straddle tenor?",
                "Did the persistence filter delay regime confirmation on 0DTE days?",
                "Were there days with valid SELL_PREMIUM regime but zero trades taken?",
            ],
        },
    }

    # ── Save report ───────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"backtest_{all_dates[0]}_to_{all_dates[-1]}.json"
    report_path.write_text(
        json.dumps(summary, indent=2, default=str),
        encoding="utf-8",
    )

    # ── Print results ─────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("WALK-FORWARD BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Period       : {all_dates[0]} to {all_dates[-1]} ({total_days} days)")
    print(f"  Trades       : {total_trades} | Win Rate: {trade_wr}% | "
          f"Day Win: {round(prof_days/total_days*100,1) if total_days else 0}%")
    print(f"  Net P&L      : Rs{total_net:,.2f} ({total_return_pct:.3f}%)")
    print(f"  Profit Factor: {overall_pf}")
    print(f"  Sharpe Ratio : {sharpe}")
    print(f"  Sortino Ratio: {sortino}")
    print(f"  Calmar Ratio : {calmar}")
    print(f"  Max Drawdown : Rs{max_dd_rs:,.2f} ({max_dd_pct:.3f}%)")
    print(f"  Total Costs  : Rs{total_costs:,.2f}")
    print(f"  Exit Breakdown: Stops={total_stops} Targets={total_tgts} "
          f"Time={total_time}")
    print(f"  Exit Priorities: Delta={total_delta_breach} Prox={total_spot_prox} "
          f"PriceStop={total_price_stop} Lock={total_profit_lock} "
          f"Cheap={total_cheap_buyback}")
    print(f"  IV Crush     : {total_iv_crush}/{total_trades} "
          f"({round(total_iv_crush/total_trades*100,1) if total_trades else 0}%)")
    print(f"  Phantom FNR  : {round(total_phantom_win/total_phantom*100,1) if total_phantom else 'N/A'}% "
          f"({total_phantom} blocked)")
    print()

    if vol_regime_agg:
        print("  Vol Regime Performance:")
        for regime, perf in sorted(
            vol_regime_agg.items(), key=lambda x: -x[1]["net_pnl"]
        ):
            wr = round(perf["wins"] / perf["count"] * 100, 1) if perf["count"] else 0
            print(f"    {str(regime):<30} n={perf['count']:3d} "
                  f"wr={wr:5.1f}% pnl=Rs{perf['net_pnl']:8.0f}")
    print()

    if price_regime_agg:
        print("  Price Regime Performance:")
        for regime, perf in sorted(
            price_regime_agg.items(), key=lambda x: -x[1]["net_pnl"]
        ):
            wr = round(perf["wins"] / perf["count"] * 100, 1) if perf["count"] else 0
            print(f"    {str(regime):<30} n={perf['count']:3d} "
                  f"wr={wr:5.1f}% pnl=Rs{perf['net_pnl']:8.0f}")
    print()

    if strategy_agg:
        print("  Strategy Performance:")
        for strat, perf in sorted(
            strategy_agg.items(), key=lambda x: -x[1]["net_pnl"]
        ):
            wr = round(perf["wins"] / perf["count"] * 100, 1) if perf["count"] else 0
            print(f"    {str(strat):<25} n={perf['count']:3d} "
                  f"wr={wr:5.1f}% pnl=Rs{perf['net_pnl']:8.0f}")
    print()

    if dte_agg:
        print("  DTE Performance:")
        dte_labels = {0: "0DTE (Tuesday)", 1: "1DTE (Monday)", 2: "2+DTE (Mid-week)"}
        for dte_k in sorted(dte_agg.keys()):
            dv = dte_agg[dte_k]
            wr = round(dv["wins"] / dv["count"] * 100, 1) if dv["count"] else 0
            label = dte_labels.get(dte_k, f"DTE={dte_k}")
            print(f"    {label:<20} n={dv['count']:3d} "
                  f"wr={wr:5.1f}% pnl=Rs{dv['net_pnl']:8.0f}")
    print()

    if or_perf:
        print("  OR Condition Performance:")
        for or_c, perf in sorted(or_perf.items(), key=lambda x: -x[1]["net_pnl"]):
            wr = round(perf["wins"] / (perf["wins"] + perf["losses"]) * 100, 1) \
                 if (perf["wins"] + perf["losses"]) > 0 else 0
            print(f"    {str(or_c):<15} days={perf['days']:3d} "
                  f"wr={wr:5.1f}% pnl=Rs{perf['net_pnl']:8.0f}")
    print()

    if iv_crush_by_strategy:
        print("  IV Crush by Strategy:")
        for strat, vals in sorted(iv_crush_by_strategy.items()):
            rate = round(vals.get("crush_count", 0) / vals.get("total", 1) * 100, 1) \
                   if vals.get("total", 0) > 0 else 0
            print(f"    {strat:<25} crush={vals.get('crush_count',0)}/"
                  f"{vals.get('total',0)} ({rate}%)")
    print()

    # Payoff geometry summary
    pg = payoff_geometry
    print("  Payoff Geometry:")
    print(f"    Avg Win:  {pg.get('avg_win_pts'):.2f}pts = Rs{pg.get('avg_win_rupees'):.0f}")
    print(f"    Avg Loss: {pg.get('avg_loss_pts'):.2f}pts = Rs{pg.get('avg_loss_rupees'):.0f}")
    print(f"    R:R Ratio: {pg.get('reward_to_risk')}")
    print(f"    BE Win Rate: {pg.get('break_even_win_rate_pct')}%")
    print(f"    Avg Slippage: {pg.get('avg_actual_slippage_pts')}pts")
    print(f"    Avg Hold: {pg.get('avg_hold_minutes')}min")
    print()

    # Calibration drift summary
    if cal_drift.get("available"):
        print("  Calibration Drift:")
        print(f"    Latest Tier:     {cal_drift.get('latest_tier')} "
              f"(valid={cal_drift.get('latest_valid')})")
        print(f"    VRP Sell:        {cal_drift.get('latest_vrp_sell')}")
        print(f"    VIX p50:         {cal_drift.get('latest_vix_p50')}")
        print(f"    VRP Drift 30d:   {cal_drift.get('vrp_sell_drift_pct')}%")
        print(f"    Phantom FNR:     {cal_drift.get('phantom_fnr_latest')}%")
        print(f"    Exit Quality:    {cal_drift.get('exit_quality_latest')}")
        print(f"    Regime Accuracy: {cal_drift.get('regime_accuracy_latest')}")
    print()

    print(f"  Full report: {report_path}")
    print()

    conn.close()
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────

def _self_test() -> None:
    """
    Self-test for backtest.py.
    Tests helper functions without requiring a live database.
    Run: python backtest.py --test
    """
    print("=" * 60)
    print("BACKTEST SELF-TEST")
    print("=" * 60)

    # ── Test safe statistics ──────────────────────────────────────────────
    print("\n--- Statistics Tests ---")

    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert abs(safe_mean(vals) - 30.0) < 0.01, "Mean should be 30"
    assert safe_percentile(vals, 50) == 30.0, "p50 should be 30"
    assert safe_percentile(vals, 0)  == 10.0, "p0 should be 10"
    assert safe_percentile(vals, 100) == 50.0, "p100 should be 50"
    assert safe_mean([]) is None, "Empty mean should be None"
    assert safe_stdev([1.0]) is None, "Single value stdev should be None"
    print("  [OK] Statistics tests passed")

    # ── Test Sharpe ratio ─────────────────────────────────────────────────
    print("\n--- Risk Metrics Tests ---")

    pnl_series = [1000, -500, 2000, -300, 1500, 800, -200, 1200]
    sharpe = compute_sharpe(pnl_series)
    sortino = compute_sortino(pnl_series)
    print(f"  Sharpe:  {sharpe}")
    print(f"  Sortino: {sortino}")
    assert sharpe is not None, "Sharpe should not be None"
    assert sortino is not None, "Sortino should not be None"
    assert compute_sharpe([]) is None, "Empty series Sharpe should be None"
    print("  [OK] Risk metrics tests passed")

    # ── Test max drawdown ─────────────────────────────────────────────────
    print("\n--- Max Drawdown Tests ---")

    cap_series = [1000000, 1050000, 980000, 1020000, 950000, 1100000]
    dd_rs, dd_pct, dd_start, dd_end = compute_max_drawdown(cap_series)
    print(f"  Max drawdown: Rs{dd_rs:,.0f} ({dd_pct:.2f}%)")
    print(f"  Drawdown period: idx {dd_start} → {dd_end}")
    assert dd_rs > 0, "Max drawdown should be positive"
    assert dd_pct > 0, "Max drawdown pct should be positive"
    # Peak is 1050000 at idx 1, trough is 950000 at idx 4
    assert abs(dd_rs - 100000) < 1, f"Expected drawdown Rs100000, got {dd_rs}"
    print("  [OK] Max drawdown tests passed")

    # ── Test Calmar ratio ─────────────────────────────────────────────────
    print("\n--- Calmar Ratio Tests ---")

    calmar = compute_calmar(15.0, 5.0)
    print(f"  Calmar (15% return, 5% dd): {calmar}")
    assert calmar == 3.0, f"Expected 3.0, got {calmar}"
    assert compute_calmar(10.0, 0.0) is None, "Zero drawdown Calmar should be None"
    print("  [OK] Calmar ratio tests passed")

    # ── Test add_win_rate_and_avg ─────────────────────────────────────────
    print("\n--- Performance Enrichment Tests ---")

    test_perf = {
        "IRON_CONDOR": {"count": 10, "wins": 7, "losses": 3, "net_pnl": 15000.0},
        "BULL_PUT_SPREAD": {"count": 5, "wins": 3, "losses": 2, "net_pnl": 8000.0},
    }
    enriched = add_win_rate_and_avg(test_perf)
    assert enriched["IRON_CONDOR"]["win_rate_pct"] == 70.0, \
        f"Expected 70.0%, got {enriched['IRON_CONDOR']['win_rate_pct']}"
    assert enriched["IRON_CONDOR"]["avg_pnl"] == 1500.0, \
        f"Expected 1500.0, got {enriched['IRON_CONDOR']['avg_pnl']}"
    # Should be sorted by net_pnl descending
    keys = list(enriched.keys())
    assert keys[0] == "IRON_CONDOR", "IRON_CONDOR should be first (higher net_pnl)"
    print("  [OK] Performance enrichment tests passed")

    # ── Test payoff geometry ──────────────────────────────────────────────
    print("\n--- Payoff Geometry Tests ---")

    mock_days = [
        {
            "trade_details": [
                {"net_pnl_rupees": 3000.0, "hold_minutes": 120,
                 "actual_slippage_pts": 0.6, "entry_credit": 35.0,
                 "opening_straddle": 175.0},
                {"net_pnl_rupees": -2000.0, "hold_minutes": 45,
                 "actual_slippage_pts": 0.9, "entry_credit": 30.0,
                 "opening_straddle": 160.0},
                {"net_pnl_rupees": 2500.0, "hold_minutes": 90,
                 "actual_slippage_pts": 0.5, "entry_credit": 32.0,
                 "opening_straddle": 170.0},
            ]
        }
    ]
    pg = compute_payoff_geometry(mock_days)
    print(f"  Avg win pts:  {pg['avg_win_pts']:.3f}")
    print(f"  Avg loss pts: {pg['avg_loss_pts']:.3f}")
    print(f"  R:R ratio:    {pg['reward_to_risk']}")
    print(f"  BE win rate:  {pg['break_even_win_rate_pct']}%")
    assert pg["total_trades"] == 3, f"Expected 3 trades, got {pg['total_trades']}"
    assert pg["total_wins"]   == 2, f"Expected 2 wins, got {pg['total_wins']}"
    assert pg["total_losses"] == 1, f"Expected 1 loss, got {pg['total_losses']}"
    assert pg["reward_to_risk"] is not None, "R:R should not be None"
    print("  [OK] Payoff geometry tests passed")

    # ── Test aggregate functions ──────────────────────────────────────────
    print("\n--- Aggregation Tests ---")

    mock_days_agg = [
        {
            "strategies_used": {
                "IRON_CONDOR": {"count": 2, "wins": 1, "losses": 1, "net_pnl": 1000.0}
            },
            "dte_performance": {
                0: {"count": 2, "wins": 1, "losses": 1, "net_pnl": 1000.0}
            },
        },
        {
            "strategies_used": {
                "IRON_CONDOR": {"count": 1, "wins": 1, "losses": 0, "net_pnl": 2000.0},
                "BULL_PUT_SPREAD": {"count": 1, "wins": 0, "losses": 1, "net_pnl": -500.0},
            },
            "dte_performance": {
                0: {"count": 1, "wins": 1, "losses": 0, "net_pnl": 2000.0},
                1: {"count": 1, "wins": 0, "losses": 1, "net_pnl": -500.0},
            },
        },
    ]

    strat_agg = aggregate_performance_dicts(mock_days_agg, "strategies_used")
    assert strat_agg["IRON_CONDOR"]["count"] == 3, \
        f"Expected 3, got {strat_agg['IRON_CONDOR']['count']}"
    assert strat_agg["IRON_CONDOR"]["net_pnl"] == 3000.0, \
        f"Expected 3000, got {strat_agg['IRON_CONDOR']['net_pnl']}"
    assert "BULL_PUT_SPREAD" in strat_agg, "BULL_PUT_SPREAD should be in aggregation"

    dte_agg_test = aggregate_dte_performance(mock_days_agg)
    assert dte_agg_test[0]["count"] == 3, f"DTE0 count should be 3, got {dte_agg_test[0]['count']}"
    assert dte_agg_test[1]["count"] == 1, f"DTE1 count should be 1, got {dte_agg_test[1]['count']}"
    print("  [OK] Aggregation tests passed")

    # ── Test database connection (if DB exists) ───────────────────────────
    print("\n--- Database Tests ---")
    if DB_PATH.exists():
        try:
            conn = get_connection()
            tables = [
                row[0] for row in
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            print(f"  Database found: {DB_PATH}")
            print(f"  Tables: {len(tables)}")
            for t in tables[:10]:
                print(f"    - {t}")
            conn.close()
            print("  [OK] Database connection test passed")
        except Exception as e:
            print(f"  Database test error: {e}")
    else:
        print(f"  Database not found at {DB_PATH} — skipping DB tests")
        print("  (Run the engine first to create the database)")

    print()
    print("=" * 60)
    print("BACKTEST SELF-TEST COMPLETE — All tests passed")
    print("=" * 60)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args   = sys.argv[1:]
    from_d = to_d = None

    # Self-test mode
    if "--test" in args:
        _self_test()
        sys.exit(0)

    # Parse date arguments
    for i, a in enumerate(args):
        if a == "--from" and i + 1 < len(args):
            from_d = args[i + 1]
        if a == "--to" and i + 1 < len(args):
            to_d = args[i + 1]

    # Validate date format
    for label, d in [("--from", from_d), ("--to", to_d)]:
        if d is not None:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                print(f"Invalid date format for {label}: {d}. Use YYYY-MM-DD.")
                sys.exit(1)

    print(f"Starting walk-forward backtest...")
    if from_d:
        print(f"  From: {from_d}")
    if to_d:
        print(f"  To:   {to_d}")

    result = run_walkforward_backtest(from_date=from_d, to_date=to_d)

    if result is None:
        print("Backtest failed — check database and try again.")
        sys.exit(1)

    sys.exit(0)