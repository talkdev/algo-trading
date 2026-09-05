import glob
import json
import statistics
import sqlite3
import csv
from pathlib import Path
from datetime import datetime, timedelta

TARGET_DATE = "2026-09-08"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "reports"


def load_env_simple(path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_ENV = load_env_simple(BASE_DIR / "env.txt")
DB_PATH = Path(_ENV.get("DB_PATH", "data/nifty_algo.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
LOG_DIR = Path(_ENV.get("LOG_DIR", "logs"))
if not LOG_DIR.is_absolute():
    LOG_DIR = BASE_DIR / LOG_DIR

LOT_SIZE            = int(_ENV.get("NIFTY_LOT_SIZE", "65") or 65)
STT_RATE            = float(_ENV.get("STT_RATE", "0.0015") or 0.0015)
EXCHANGE_TXN_RATE   = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003552") or 0.0003552)
BROKERAGE_PER_ORDER = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)
STARTING_CAPITAL    = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1000000)
MAX_DAILY_LOSS_PCT  = float(_ENV.get("MAX_DAILY_LOSS_PCT", "0.02") or 0.02)


def get_connection(db_path):
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}.")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def q(conn, sql, params=()):
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def fetch_session_state(conn, d):
    if not table_exists(conn, "session_state"):
        return None
    rows = q(conn, "SELECT * FROM session_state WHERE trading_date=?", (d,))
    return rows[0] if rows else None


def fetch_cycle_log(conn, d):
    if not table_exists(conn, "cycle_log"):
        return []
    return q(conn, "SELECT * FROM cycle_log WHERE trading_date=? ORDER BY cycle_id", (d,))


def fetch_strategy_decisions(conn, d):
    if not table_exists(conn, "strategy_decisions"):
        return []
    return q(conn, "SELECT * FROM strategy_decisions WHERE trading_date=? ORDER BY decision_id", (d,))


def fetch_positions(conn, d):
    if not table_exists(conn, "positions"):
        return []
    return q(conn, "SELECT * FROM positions WHERE trading_date=? ORDER BY entry_time", (d,))


def fetch_position_legs(conn, position_ids):
    if not position_ids or not table_exists(conn, "position_legs"):
        return []
    placeholders = ",".join("?" for _ in position_ids)
    return q(conn, f"SELECT * FROM position_legs WHERE position_id IN ({placeholders}) ORDER BY position_id, leg_id", tuple(position_ids))


def fetch_trade_entries(conn, d):
    if not table_exists(conn, "trade_entries"):
        return []
    return q(conn, "SELECT * FROM trade_entries WHERE trading_date=? ORDER BY entry_time", (d,))


def fetch_trade_exits(conn, d):
    if not table_exists(conn, "trade_exits"):
        return []
    return q(conn, "SELECT te.* FROM trade_exits te JOIN positions p ON te.position_id=p.position_id WHERE p.trading_date=? ORDER BY te.exit_time", (d,))


def fetch_daily_summary(conn, d):
    if not table_exists(conn, "daily_summary"):
        return None
    rows = q(conn, "SELECT * FROM daily_summary WHERE trading_date=?", (d,))
    return rows[0] if rows else None


def fetch_regime_decisions(conn, d):
    if not table_exists(conn, "regime_decisions"):
        return []
    return q(conn, "SELECT * FROM regime_decisions WHERE date=? ORDER BY timestamp", (d,))


def fetch_calibration_state(conn):
    if not table_exists(conn, "calibration_state"):
        return None
    rows = q(conn, "SELECT * FROM calibration_state WHERE is_valid=1 ORDER BY calibrated_at DESC LIMIT 1")
    return rows[0] if rows else None


def fetch_calibration_drift(conn):
    if not table_exists(conn, "calibration_state"):
        return []
    return q(conn, "SELECT * FROM calibration_state ORDER BY calibrated_at DESC LIMIT 20")


def fetch_option_chain_snapshot(conn, d):
    if not table_exists(conn, "option_chain_snapshot"):
        return []
    return q(conn, "SELECT * FROM option_chain_snapshot WHERE trading_date=? ORDER BY capture_time, strike, option_type", (d,))


def fetch_api_call_log(conn, d):
    if not table_exists(conn, "api_call_log"):
        return []
    return q(conn, "SELECT * FROM api_call_log WHERE call_time LIKE ? ORDER BY call_time", (f"{d}%",))


def fetch_audit_log_db(conn, d):
    if not table_exists(conn, "audit_log"):
        return []
    return q(conn, "SELECT * FROM audit_log WHERE log_time LIKE ? ORDER BY log_time", (f"{d}%",))


def fetch_audit_log_file_lines(log_dir, d):
    lines = []
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


def fetch_prior_days_summary(conn, d, n=10):
    if not table_exists(conn, "daily_summary"):
        return []
    return q(conn, "SELECT * FROM daily_summary WHERE trading_date < ? ORDER BY trading_date DESC LIMIT ?", (d, n))


def fetch_intraday_candles(conn, d):
    if not table_exists(conn, "intraday_candles"):
        return []
    return q(conn, "SELECT candle_time, open, high, low, close, volume FROM intraday_candles WHERE trading_date=? AND interval_min=1 ORDER BY candle_time", (d,))


def fetch_cumulative_performance(conn, d, lookback_days=90):
    if not table_exists(conn, "daily_summary"):
        return []
    cutoff = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return q(conn, "SELECT * FROM daily_summary WHERE trading_date >= ? AND trading_date <= ? ORDER BY trading_date", (cutoff, d))


def fetch_vix_history_today(conn, d):
    if not table_exists(conn, "vix_history"):
        return []
    return q(conn, "SELECT * FROM vix_history WHERE date=? ORDER BY timestamp", (d,))


def fetch_market_snapshots_today(conn, d):
    if not table_exists(conn, "market_snapshots"):
        return []
    return q(conn, "SELECT * FROM market_snapshots WHERE date=? ORDER BY timestamp", (d,))


def compute_candle_statistics(candles):
    if not candles:
        return {}
    closes  = [c["close"] for c in candles if c.get("close")]
    highs   = [c["high"]  for c in candles if c.get("high")]
    lows    = [c["low"]   for c in candles if c.get("low")]
    volumes = [c["volume"] for c in candles if c.get("volume")]
    if not closes:
        return {}
    ranges = [c["high"] - c["low"] for c in candles if c.get("high") and c.get("low")]
    result = {
        "total_1min_bars": len(candles),
        "first_bar_time":  candles[0]["candle_time"] if candles else None,
        "last_bar_time":   candles[-1]["candle_time"] if candles else None,
        "open":  closes[0],
        "close": closes[-1],
        "high":  max(highs) if highs else None,
        "low":   min(lows)  if lows  else None,
        "total_volume":      sum(volumes) if volumes else 0,
        "avg_bar_range_pts": round(statistics.mean(ranges), 3) if ranges else None,
        "max_bar_range_pts": round(max(ranges), 3) if ranges else None,
        "zero_volume_bars":  sum(1 for v in volumes if v == 0),
    }
    if len(closes) >= 2:
        result["net_change_pts"] = round(closes[-1] - closes[0], 2)
        result["net_change_pct"] = round((closes[-1] - closes[0]) / closes[0] * 100, 3)
    return result


def compute_equity_curve(cumulative_days):
    if not cumulative_days:
        return {}
    pnls    = [d.get("net_pnl_rupees") or 0 for d in cumulative_days]
    capital = [d.get("capital_end") for d in cumulative_days if d.get("capital_end")]
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
        "day_win_rate_pct":     round(wins / len(cumulative_days) * 100, 1) if cumulative_days else 0,
        "total_pnl_rupees":     round(total_pnl, 2),
        "avg_daily_pnl_rupees": round(total_pnl / len(cumulative_days), 2) if cumulative_days else 0,
        "gross_wins_rupees":    round(gross_wins, 2),
        "gross_losses_rupees":  round(gross_losses, 2),
        "profit_factor":        round(gross_wins / gross_losses, 3) if gross_losses > 0 else None,
        "max_drawdown_rupees":  round(max_dd, 2),
        "capital_start":        capital[0]  if capital else None,
        "capital_end":          capital[-1] if capital else None,
        "total_return_pct":     round((capital[-1] - capital[0]) / capital[0] * 100, 3) if len(capital) >= 2 else 0,
        "daily_pnl_series": [
            {"date": d.get("trading_date"), "pnl": d.get("net_pnl_rupees"), "capital": d.get("capital_end")}
            for d in cumulative_days
        ],
    }


def summarize_option_chain(chain_rows):
    if not chain_rows:
        return {"total_rows": 0}
    capture_times = sorted(set(r["capture_time"] for r in chain_rows if r.get("capture_time")))
    strikes       = sorted(set(r["strike"] for r in chain_rows if r.get("strike") is not None))
    zero_bid_ask  = sum(1 for r in chain_rows if (r.get("bid") or 0) == 0 and (r.get("ask") or 0) == 0)
    spreads = [(r["ask"] - r["bid"]) for r in chain_rows if (r.get("bid") or 0) > 0 and (r.get("ask") or 0) > 0]
    ivs     = [r["iv"] for r in chain_rows if r.get("iv") and r["iv"] > 0]
    call_ois = [r["oi"] for r in chain_rows if r.get("option_type") == "call" and r.get("oi")]
    put_ois  = [r["oi"] for r in chain_rows if r.get("option_type") == "put"  and r.get("oi")]
    total_call_oi = sum(call_ois)
    total_put_oi  = sum(put_ois)
    return {
        "total_rows":           len(chain_rows),
        "unique_capture_times": len(capture_times),
        "unique_strikes":       len(strikes),
        "strike_range":         [min(strikes), max(strikes)] if strikes else None,
        "zero_bid_ask_count":   zero_bid_ask,
        "zero_bid_ask_pct":     round(zero_bid_ask / len(chain_rows) * 100, 1) if chain_rows else 0,
        "avg_spread":           round(statistics.mean(spreads), 3) if spreads else None,
        "max_spread":           round(max(spreads), 3) if spreads else None,
        "avg_iv_pct":           round(statistics.mean(ivs) * 100, 2) if ivs else None,
        "total_call_oi":        total_call_oi,
        "total_put_oi":         total_put_oi,
        "chain_pcr":            round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else None,
        "first_capture":        capture_times[0]  if capture_times else None,
        "last_capture":         capture_times[-1] if capture_times else None,
    }


def summarize_api_calls(api_rows):
    if not api_rows:
        return {"total_calls": 0}
    by_category    = {}
    errors         = []
    response_times = []
    rate_limited   = 0
    for r in api_rows:
        cat = r.get("category") or "unknown"
        by_category[cat] = by_category.get(cat, 0) + 1
        if r.get("error_message"):
            errors.append(r)
        if r.get("rate_limited"):
            rate_limited += 1
        if r.get("response_time_ms") is not None:
            response_times.append(r["response_time_ms"])
    return {
        "total_calls":        len(api_rows),
        "by_category":        by_category,
        "error_count":        len(errors),
        "rate_limited_count": rate_limited,
        "avg_response_ms":    round(statistics.mean(response_times), 1) if response_times else None,
        "p95_response_ms":    round(sorted(response_times)[int(len(response_times) * 0.95)], 1) if len(response_times) > 5 else None,
        "max_response_ms":    round(max(response_times), 1) if response_times else None,
        "slow_calls_over_2s": sum(1 for t in response_times if t > 2000),
        "errors_sample":      errors[:20],
    }


def compute_vrp_statistics(cycle_rows):
    vrps = [c["vrp"] for c in cycle_rows if c.get("vrp") is not None]
    ivs  = [c["atm_iv_pct"] for c in cycle_rows if c.get("atm_iv_pct") is not None]
    rvs  = [c["parkinson_rv_pct"] for c in cycle_rows if c.get("parkinson_rv_pct") is not None]
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
        "vrp_very_rich_cycles": sum(1 for v in vrps if v > 5.0),
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


def compute_vix_profile(vix_rows):
    if not vix_rows:
        return {}
    vals = [r["vix_value"] for r in vix_rows if r.get("vix_value")]
    if not vals:
        return {}
    return {
        "vix_open":       round(vals[0], 2),
        "vix_close":      round(vals[-1], 2),
        "vix_high":       round(max(vals), 2),
        "vix_low":        round(min(vals), 2),
        "vix_range":      round(max(vals) - min(vals), 2),
        "vix_change_pct": round((vals[-1] - vals[0]) / vals[0] * 100, 2) if vals[0] > 0 else 0,
        "readings_count": len(vals),
        "pct_above_20":   round(sum(1 for v in vals if v > 20) / len(vals) * 100, 1),
        "pct_below_14":   round(sum(1 for v in vals if v < 14) / len(vals) * 100, 1),
    }


def compute_vrp_curve(cycle_rows):
    curve = []
    for c in cycle_rows:
        if c.get("vrp") is not None:
            curve.append({
                "time": str(c.get("cycle_time", ""))[:19],
                "vrp": round(c["vrp"], 3),
                "atm_iv_pct": round(c["atm_iv_pct"], 3) if c.get("atm_iv_pct") else None,
                "parkinson_rv_pct": round(c["parkinson_rv_pct"], 3) if c.get("parkinson_rv_pct") else None,
                "volatility_condition": c.get("volatility_condition"),
            })
    return curve


def compute_or_analysis(session_state, cycle_rows, trade_entries):
    if not session_state:
        return {"or_computed": False}
    or_high = session_state.get("or_high")
    or_low  = session_state.get("or_low")
    or_width = session_state.get("or_width")
    if not or_high or not or_low:
        return {"or_computed": False}
    spots = [c["spot"] for c in cycle_rows if c.get("spot")]
    return {
        "or_computed":             True,
        "or_high":                 or_high,
        "or_low":                  or_low,
        "or_width_pts":            or_width,
        "or_condition":            session_state.get("or_condition"),
        "or_width_pct":            round(or_width / ((or_high + or_low) / 2) * 100, 3) if or_width else None,
        "entries_above_or":        sum(1 for t in trade_entries if t.get("entry_spot") and t["entry_spot"] > or_high),
        "entries_below_or":        sum(1 for t in trade_entries if t.get("entry_spot") and t["entry_spot"] < or_low),
        "entries_in_or":           sum(1 for t in trade_entries if t.get("entry_spot") and or_low <= t["entry_spot"] <= or_high),
        "max_excursion_above_pts": round(max((s - or_high for s in spots if s > or_high), default=0), 1),
        "max_excursion_below_pts": round(max((or_low - s for s in spots if s < or_low), default=0), 1),
    }


def compute_iv_crush_per_trade(entry, exit_row, cycle_rows):
    if not entry or not exit_row:
        return {}
    entry_iv = entry.get("entry_atm_iv")
    if not entry_iv or entry_iv <= 0:
        return {}
    exit_time = exit_row.get("exit_time", "")
    exit_cycles = [c for c in cycle_rows if str(c.get("cycle_time", "")) >= exit_time]
    if not exit_cycles:
        return {}
    exit_iv_pct = exit_cycles[0].get("atm_iv_pct")
    if not exit_iv_pct:
        return {}
    exit_iv = exit_iv_pct / 100.0
    crush = (entry_iv - exit_iv) / entry_iv * 100.0 if entry_iv > 0 else 0.0
    return {
        "entry_atm_iv_pct": round(entry_iv * 100, 2),
        "exit_atm_iv_pct":  round(exit_iv * 100, 2),
        "iv_crush_pct":     round(crush, 2),
        "direction":        "CRUSH" if crush > 5 else ("EXPAND" if crush < -5 else "STABLE"),
    }


def compute_regime_accuracy(regime_decisions, trade_exits):
    if not regime_decisions or not trade_exits:
        return {}
    result = {}
    for rd in regime_decisions:
        regime = rd.get("final_regime", "UNKNOWN")
        ts = rd.get("timestamp", "")
        matching = [e for e in trade_exits if str(e.get("exit_time", "")) >= ts]
        if matching:
            outcome = matching[0].get("result", "UNKNOWN")
            if regime not in result:
                result[regime] = {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0, "total": 0}
            result[regime][outcome] = result[regime].get(outcome, 0) + 1
            result[regime]["total"] += 1
    for regime in result:
        t = result[regime]["total"]
        result[regime]["win_rate_pct"] = round(result[regime].get("WIN", 0) / t * 100, 1) if t else 0
    return result


def compute_slippage_analysis(trade_entries, trade_exits):
    if not trade_entries:
        return {}
    n = len(trade_entries)
    total_est   = sum(t.get("total_slippage", 0) or 0 for t in trade_entries)
    total_costs = sum(e.get("total_costs_rupees", 0) or 0 for e in trade_exits)
    return {
        "total_estimated_slippage_pts": round(total_est, 3),
        "avg_estimated_slippage_pts":   round(total_est / n, 3) if n else 0,
        "total_actual_costs_rupees":    round(total_costs, 2),
        "avg_actual_costs_rupees":      round(total_costs / n, 2) if n else 0,
        "cost_per_lot_rupees":          round(total_costs / (n * LOT_SIZE), 2) if n else 0,
    }


def compute_cost_by_strategy(trade_entries, trade_exits):
    exits_by_pos = {e["position_id"]: e for e in trade_exits}
    result = {}
    for t in trade_entries:
        strat = t.get("strategy_name", "UNKNOWN")
        pid   = t.get("position_id")
        ex    = exits_by_pos.get(pid)
        if strat not in result:
            result[strat] = {"trades": 0, "wins": 0, "losses": 0, "total_costs": 0.0, "total_net_pnl": 0.0}
        result[strat]["trades"] += 1
        if ex:
            result[strat]["total_costs"]   += ex.get("total_costs_rupees", 0) or 0
            result[strat]["total_net_pnl"] += ex.get("net_pnl_rupees", 0) or 0
            if ex.get("result") == "WIN":
                result[strat]["wins"] += 1
            elif ex.get("result") == "LOSS":
                result[strat]["losses"] += 1
    for s in result:
        t = result[s]["trades"]
        result[s]["win_rate_pct"]      = round(result[s]["wins"] / t * 100, 1) if t else 0
        result[s]["avg_cost_per_trade"] = round(result[s]["total_costs"] / t, 2) if t else 0
    return result


def compute_greeks_attribution(trade_entries, trade_exits, cycle_rows):
    exits_by_pos = {e["position_id"]: e for e in trade_exits}
    result = []
    for t in trade_entries:
        pid       = t.get("position_id")
        ex        = exits_by_pos.get(pid)
        entry_vrp = t.get("entry_vrp")
        exit_vrp  = None
        if ex:
            exit_time   = ex.get("exit_time", "")
            exit_cycles = [c for c in cycle_rows if str(c.get("cycle_time", "")) >= exit_time]
            if exit_cycles:
                exit_vrp = exit_cycles[0].get("vrp")
        theta_est = 0.0
        if t.get("legs_json"):
            try:
                legs     = json.loads(t["legs_json"])
                hold_min = ex.get("hold_minutes", 0) if ex else 0
                lots     = t.get("final_lots", 1) or 1
                for leg in legs:
                    theta = abs(leg.get("theta", 0) or 0)
                    sign  = -1 if leg.get("action") == "SELL" else 1
                    theta_est += sign * theta * lots * ((hold_min or 0) / (6.5 * 60))
            except Exception:
                pass
        result.append({
            "position_id":         pid,
            "strategy_name":       t.get("strategy_name"),
            "net_pnl_rupees":      ex.get("net_pnl_rupees") if ex else None,
            "hold_minutes":        ex.get("hold_minutes") if ex else None,
            "entry_vrp":           entry_vrp,
            "exit_vrp":            exit_vrp,
            "vrp_change":          round((exit_vrp - entry_vrp), 3) if (exit_vrp is not None and entry_vrp is not None) else None,
            "estimated_theta_pts": round(theta_est, 3),
            "result":              ex.get("result") if ex else None,
            "exit_reason":         ex.get("exit_reason") if ex else None,
        })
    return result


def compute_intraday_spot_profile(cycle_rows):
    spots = [(c["cycle_time"], c["spot"]) for c in cycle_rows if c.get("spot") is not None]
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
        "direction":      "UP" if spot_values[-1] > spot_values[0] else ("DOWN" if spot_values[-1] < spot_values[0] else "FLAT"),
    }
    if len(spot_values) >= 6:
        mid = len(spot_values) // 2
        fh  = max(spot_values[:mid]) - min(spot_values[:mid])
        sh  = max(spot_values[mid:]) - min(spot_values[mid:])
        result["first_half_range_pts"]             = round(fh, 2)
        result["second_half_range_pts"]            = round(sh, 2)
        result["volatility_expansion_second_half"] = sh > fh
    return result


def compute_adx_profile(cycle_rows):
    adx_col  = "adx_15" if any(c.get("adx_15") for c in cycle_rows) else "adx"
    adx_vals = [c[adx_col] for c in cycle_rows if c.get(adx_col) is not None]
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


def compute_pcr_profile(cycle_rows):
    pcrs = [c["pcr"] for c in cycle_rows if c.get("pcr") is not None]
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


def compute_skew_profile(cycle_rows):
    skew_col = "skew" if any(c.get("skew") for c in cycle_rows) else "skew_ratio"
    skews = [c[skew_col] for c in cycle_rows if c.get(skew_col) is not None]
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


def compute_regime_distribution(regime_decisions, cycle_rows):
    dist = {}
    for r in regime_decisions:
        fr = r.get("final_regime") or "UNKNOWN"
        dist[fr] = dist.get(fr, 0) + 1
    if not dist:
        for c in cycle_rows:
            fr = c.get("final_regime") or c.get("action_taken") or "UNKNOWN"
            if fr not in ("SIGNAL_ONLY", None):
                dist[fr] = dist.get(fr, 0) + 1
    return dict(sorted(dist.items(), key=lambda x: -x[1]))


def compute_regime_timeline(regime_decisions, cycle_rows):
    timeline = []
    for r in regime_decisions:
        timeline.append({
            "time":            r.get("timestamp", ""),
            "source":          "regime_engine",
            "final_regime":    r.get("final_regime"),
            "confidence":      r.get("confidence"),
            "size_multiplier": r.get("size_multiplier"),
            "vol_regime":      r.get("volatility_regime"),
            "price_regime_15": r.get("price_regime_15"),
            "positioning":     r.get("positioning_regime"),
            "adx_15":          r.get("adx_15"),
            "ema_structure":   r.get("ema_structure"),
            "event_day":       r.get("event_day"),
            "calibration_tier":r.get("calibration_tier"),
            "notes":           r.get("notes"),
        })
    for c in cycle_rows:
        if c.get("final_regime"):
            timeline.append({
                "time":            c.get("cycle_time", ""),
                "source":          "cycle_log",
                "final_regime":    c.get("final_regime"),
                "confidence":      c.get("confidence"),
                "size_multiplier": None,
                "vol_regime":      c.get("volatility_condition"),
                "price_regime_15": c.get("price_regime_15"),
                "positioning":     None,
                "adx_15":          c.get("adx_15") or c.get("adx"),
                "ema_structure":   c.get("ema_structure"),
                "event_day":       None,
                "calibration_tier":None,
                "notes":           c.get("no_trade_reason"),
            })
    timeline.sort(key=lambda x: x["time"])
    return timeline


def compute_calibration_summary(calibration_row):
    if not calibration_row:
        return {"status": "No calibration data found"}
    return {
        "calibration_tier":       calibration_row.get("calibration_tier", 0),
        "is_valid":               bool(calibration_row.get("is_valid")),
        "n_trading_days":         calibration_row.get("n_trading_days"),
        "n_tuesday_expiries":     calibration_row.get("n_tuesday_expiries"),
        "calibrated_at":          calibration_row.get("calibrated_at"),
        "vix_p25":                calibration_row.get("vix_p25"),
        "vix_p50":                calibration_row.get("vix_p50"),
        "vix_p75":                calibration_row.get("vix_p75"),
        "vix_p90":                calibration_row.get("vix_p90"),
        "vrp_sell_threshold":     calibration_row.get("vrp_sell_threshold"),
        "vrp_fair_threshold":     calibration_row.get("vrp_fair_threshold"),
        "skew_bearish_threshold": calibration_row.get("skew_bearish_threshold"),
        "skew_bullish_threshold": calibration_row.get("skew_bullish_threshold"),
        "oi_buildup_threshold":   calibration_row.get("oi_buildup_threshold"),
        "oi_unwind_threshold":    calibration_row.get("oi_unwind_threshold"),
        "oi_wall_strong_cal":     calibration_row.get("oi_wall_strong_cal"),
        "oi_wall_moderate_cal":   calibration_row.get("oi_wall_moderate_cal"),
        "straddle_ratio_sell":    calibration_row.get("straddle_ratio_sell"),
        "day_size_tuesday":       calibration_row.get("day_size_tuesday"),
        "day_size_monday":        calibration_row.get("day_size_monday"),
        "day_size_wednesday":     calibration_row.get("day_size_wednesday"),
        "day_size_thursday":      calibration_row.get("day_size_thursday"),
        "day_size_friday":        calibration_row.get("day_size_friday"),
        "notes":                  calibration_row.get("notes"),
        "tier_description": {
            0: "No calibration — using Config estimates",
            1: "Tier 1 — VIX percentiles from live data (5+ days)",
            2: "Tier 2 — Full calibration (20+ days, all thresholds data-derived)",
            3: "Tier 3 — Robust calibration (60+ days, 100+ VIX rows)",
        }.get(calibration_row.get("calibration_tier", 0), "Unknown"),
    }


def aggregate_no_trade_reasons(decisions):
    counts = {}
    for d in decisions:
        if d.get("action") == "NO_TRADE":
            r = d.get("reason") or "unknown"
            counts[r] = counts.get(r, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def aggregate_strategies(trade_entries):
    counts = {}
    for t in trade_entries:
        counts[t.get("strategy_name", "UNKNOWN")] = counts.get(t.get("strategy_name", "UNKNOWN"), 0) + 1
    return counts


def compute_pnl_curve(cycle_rows):
    curve = []
    for c in cycle_rows:
        if c.get("cycle_time") and c.get("daily_pnl_net") is not None:
            curve.append({
                "time":   c["cycle_time"],
                "pnl":    c["daily_pnl_net"],
                "spot":   c.get("spot"),
                "vrp":    c.get("vrp"),
                "adx":    c.get("adx_15") or c.get("adx"),
                "regime": c.get("final_regime"),
            })
    return curve


def detect_anomalies(session_state, cycle_rows, api_summary, daily_summary, decisions, trade_exits, vrp_stats, spot_profile, regime_decisions, calibration_summary, vix_profile, or_analysis):
    flags = []
    if session_state:
        if session_state.get("daily_halted"):
            flags.append(f"[FLAG] Daily trading halted: {session_state.get('last_stop_reason')}")
        if session_state.get("circuit_breaker_suspected"):
            flags.append("[FLAG] Circuit breaker suspected today")
        if session_state.get("vix_spike_detected"):
            flags.append("[FLAG] VIX spike detected today")
        if not session_state.get("or_computed"):
            flags.append("[FLAG] Opening range never computed")
        if not session_state.get("session_initialized"):
            flags.append("[FLAG] Session never initialized — opening IV baseline missing")
    if cycle_rows:
        missing_vrp  = sum(1 for c in cycle_rows if c.get("vrp") is None)
        missing_spot = sum(1 for c in cycle_rows if c.get("spot") is None)
        unknown_vol  = sum(1 for c in cycle_rows if c.get("volatility_condition") == "UNKNOWN")
        no_regime    = sum(1 for c in cycle_rows if not c.get("final_regime"))
        if missing_vrp > len(cycle_rows) * 0.3:
            flags.append(f"[FLAG] {missing_vrp}/{len(cycle_rows)} cycles had missing VRP data")
        if missing_spot > 0:
            flags.append(f"[FLAG] {missing_spot} cycles had missing spot price")
        if unknown_vol > 3:
            flags.append(f"[FLAG] {unknown_vol} cycles had volatility_condition=UNKNOWN")
        if no_regime > len(cycle_rows) * 0.5:
            flags.append(f"[FLAG] {no_regime}/{len(cycle_rows)} cycles had no final_regime")
    if api_summary.get("error_count", 0) > 5:
        flags.append(f"[FLAG] {api_summary['error_count']} API errors today")
    if api_summary.get("rate_limited_count", 0) > 0:
        flags.append(f"[FLAG] {api_summary['rate_limited_count']} API calls rate-limited")
    if daily_summary and (daily_summary.get("stops_fired") or 0) >= 2:
        flags.append(f"[FLAG] {daily_summary['stops_fired']} stop-losses fired today")
    if vrp_stats.get("vrp_negative_cycles", 0) > 0 and vrp_stats.get("total_vrp_cycles", 0) > 0:
        neg_pct = vrp_stats["vrp_negative_cycles"] / vrp_stats["total_vrp_cycles"] * 100
        if neg_pct > 30:
            flags.append(f"[FLAG] VRP negative in {neg_pct:.0f}% of cycles — IV below realized vol")
    if spot_profile.get("range_pct", 0) > 1.5:
        flags.append(f"[FLAG] NIFTY moved {spot_profile.get('range_pct', 0):.2f}% intraday — high-move day")
    if spot_profile.get("range_pct", 0) < 0.3:
        flags.append(f"[FLAG] NIFTY moved only {spot_profile.get('range_pct', 0):.2f}% — very low move day")
    if vix_profile.get("vix_range", 0) > 5:
        flags.append(f"[FLAG] VIX range today: {vix_profile.get('vix_range')} — high volatility day")
    if or_analysis.get("or_computed") and or_analysis.get("or_width_pct", 0) > 0.75:
        flags.append(f"[FLAG] Very wide OR: {or_analysis.get('or_width_pct')}% — dangerous for premium selling")
    no_trade_reasons = aggregate_no_trade_reasons(decisions)
    _ok_reasons = {"position_already_open_single_position_engine", "max_concurrent_positions_reached"}
    if no_trade_reasons:
        top_reason, top_count = next(iter(no_trade_reasons.items()))
        total_decisions = len(decisions)
        if total_decisions and top_count > total_decisions * 0.5:
            if top_reason not in _ok_reasons:
                flags.append(f"[FLAG] Dominant no-trade reason: '{top_reason}' {top_count}/{total_decisions} ({top_count/total_decisions*100:.0f}%)")
            else:
                flags.append(f"[OK] Single position engine: '{top_reason}' {top_count}/{total_decisions} — expected")
    stop_exits = [e for e in trade_exits if e.get("exit_reason") == "CLOSE_STOP"]
    if len(stop_exits) >= 2:
        flags.append(f"[FLAG] {len(stop_exits)} stop-loss exits today")
    if not regime_decisions:
        flags.append("[FLAG] No regime_decisions rows — regime engine not persisting")
    else:
        emergency = [r for r in regime_decisions if r.get("final_regime") == "EMERGENCY_EXIT"]
        if emergency:
            flags.append(f"[FLAG] {len(emergency)} EMERGENCY_EXIT regime decisions today")
    cal_tier  = calibration_summary.get("calibration_tier", 0)
    cal_valid = calibration_summary.get("is_valid", False)
    if not cal_valid:
        flags.append(f"[INFO] Calibration tier={cal_tier} — using defaults. Need 20 trading days for Tier 2.")
    else:
        flags.append(f"[OK] Calibration tier={cal_tier} VALID — thresholds data-derived")
    if not flags:
        flags.append("[OK] No major anomalies detected")
    return flags


def filter_log_lines_by_level(lines, levels):
    out = []
    for line in lines:
        parts = line.split("|")
        if len(parts) >= 2 and parts[1].strip() in levels:
            out.append(line)
    return out


def build_master_timeline(cycle_rows, decisions, trade_entries, trade_exits, regime_decisions, audit_lines):
    events = []
    for c in cycle_rows:
        events.append((c.get("cycle_time") or "", "CYCLE",
            f"spot={c.get('spot')} vix={c.get('vix')} vrp={c.get('vrp')} vol={c.get('volatility_condition')} trend={c.get('trend_condition')} dir={c.get('direction')} adx={c.get('adx_15') or c.get('adx')} pcr={c.get('pcr')} regime={c.get('final_regime')} conf={c.get('confidence')} action={c.get('action_taken')} pnl={c.get('daily_pnl_net')}"))
    for r in regime_decisions:
        events.append((r.get("timestamp") or "", "REGIME",
            f"{r.get('final_regime')} conf={r.get('confidence')} size={r.get('size_multiplier')} vol={r.get('volatility_regime')} price15={r.get('price_regime_15')} pos={r.get('positioning_regime')} adx15={r.get('adx_15')} tier={r.get('calibration_tier')} notes={r.get('notes')}"))
    for d in decisions:
        events.append((d.get("decision_time") or "", "DECISION",
            f"{d.get('action')} {d.get('strategy_name') or ''} — {d.get('reason')}"))
    for t in trade_entries:
        events.append((t.get("entry_time") or "", "ENTRY",
            f"{t.get('strategy_name')} lots={t.get('final_lots')} credit={t.get('entry_credit') or t.get('entry_debit')} vrp={t.get('entry_vrp')} regime={t.get('final_regime_at_entry')} conf={t.get('confidence_at_entry')}"))
    for e in trade_exits:
        events.append((e.get("exit_time") or "", "EXIT",
            f"{e.get('strategy_name')} reason={e.get('exit_reason')} hold={e.get('hold_minutes')}min net_pnl={e.get('net_pnl_rupees')} result={e.get('result')}"))
    for line in filter_log_lines_by_level(audit_lines, {"WARNING", "ERROR", "CRITICAL"}):
        parts = line.split("|")
        ts    = parts[0].strip() if parts else ""
        msg   = "|".join(parts[2:]).strip() if len(parts) > 2 else line
        level = parts[1].strip() if len(parts) > 1 else "LOG"
        events.append((ts, f"LOG:{level}", msg))
    events.sort(key=lambda x: x[0])
    return events


def md_kv(d):
    if not d:
        return "_(none)_\n"
    return "\n".join(f"- **{k}**: {v}" for k, v in d.items()) + "\n"


def md_table(rows, columns, max_rows=80):
    if not rows:
        return "_(no data)_\n"
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for r in rows[:max_rows]:
        vals = []
        for c in columns:
            v = r.get(c, "") if isinstance(r, dict) else ""
            if v is None:
                v = ""
            if isinstance(v, float):
                v = f"{v:.4f}"
            vals.append(str(v).replace("|", "\\|").replace("\n", " ")[:120])
        lines.append("| " + " | ".join(vals) + " |")
    out = "\n".join(lines) + "\n"
    if len(rows) > max_rows:
        out += f"\n_... {len(rows) - max_rows} more rows omitted._\n"
    return out


def generate_report(target_date):
    datetime.strptime(target_date, "%Y-%m-%d")
    conn = get_connection(DB_PATH)

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

    conn.close()

    exits_by_position = {e["position_id"]: e for e in trade_exits}
    legs_by_position  = {}
    for leg in legs:
        legs_by_position.setdefault(leg["position_id"], []).append(leg)

    no_trade_reasons    = aggregate_no_trade_reasons(decisions)
    strategies_used     = aggregate_strategies(trade_entries)
    chain_summary       = summarize_option_chain(chain_rows)
    api_summary         = summarize_api_calls(api_rows)
    vrp_stats           = compute_vrp_statistics(cycle_rows)
    spot_profile        = compute_intraday_spot_profile(cycle_rows)
    adx_profile         = compute_adx_profile(cycle_rows)
    pcr_profile         = compute_pcr_profile(cycle_rows)
    skew_profile        = compute_skew_profile(cycle_rows)
    pnl_curve           = compute_pnl_curve(cycle_rows)
    candle_stats        = compute_candle_statistics(intraday_candles)
    equity_curve        = compute_equity_curve(cumulative_days)
    regime_timeline     = compute_regime_timeline(regime_decisions, cycle_rows)
    regime_dist         = compute_regime_distribution(regime_decisions, cycle_rows)
    calibration_summary = compute_calibration_summary(calibration_row)
    vix_profile         = compute_vix_profile(vix_history_today)
    vrp_curve           = compute_vrp_curve(cycle_rows)
    or_analysis         = compute_or_analysis(session_state, cycle_rows, trade_entries)
    slippage_analysis   = compute_slippage_analysis(trade_entries, trade_exits)
    cost_by_strat       = compute_cost_by_strategy(trade_entries, trade_exits)
    regime_accuracy     = compute_regime_accuracy(regime_decisions, trade_exits)
    greeks_attr         = compute_greeks_attribution(trade_entries, trade_exits, cycle_rows)

    iv_crush_by_pos = {}
    for t in trade_entries:
        pid = t.get("position_id")
        iv_crush_by_pos[pid] = compute_iv_crush_per_trade(t, exits_by_position.get(pid), cycle_rows)

    anomalies = detect_anomalies(
        session_state, cycle_rows, api_summary, daily_summary,
        decisions, trade_exits, vrp_stats, spot_profile,
        regime_decisions, calibration_summary, vix_profile, or_analysis
    )
    timeline = build_master_timeline(
        cycle_rows, decisions, trade_entries, trade_exits,
        regime_decisions, audit_file_lines
    )
    warning_error_lines = filter_log_lines_by_level(audit_file_lines, {"WARNING", "ERROR", "CRITICAL"})

    net_pnl     = round(sum(e.get("net_pnl_rupees") or 0 for e in trade_exits), 2)
    gross_pnl   = round(sum(e.get("gross_pnl_rupees") or 0 for e in trade_exits), 2)
    total_costs = round(sum(e.get("total_costs_rupees") or 0 for e in trade_exits), 2)
    wins        = sum(1 for e in trade_exits if e.get("result") == "WIN")
    losses      = sum(1 for e in trade_exits if e.get("result") == "LOSS")

    md = []
    md.append("# NIFTY Intraday Options Engine v2.0 — EOD Forensic Report")
    md.append(f"**Target Date:** {target_date}  |  **Generated:** {datetime.now().isoformat()}\n")
    md.append("## 0. Engine Architecture Note\n")
    md.append("Integrated v2.0 engine. Regime engine classifies volatility, price, positioning using ADX, EMA, HH/HL, ORB, OI change, skew, PCR. Calibration tiered 0-3, auto-improves. All positions intraday only.\n")

    md.append("## 1. Table of Contents\n")
    md.append("2. Executive Summary | 3. Anomalies | 4. Calibration | 5. Session Config | 6. NIFTY Profile | 7. VRP Deep Dive | 8. VRP Curve | 9. ADX Profile | 10. PCR/Skew | 11. OR Analysis | 12. VIX Profile | 13. Regime Timeline | 14. Regime Distribution | 15. Regime Accuracy | 16. Market Data Timeline | 17. Gate Analysis | 18. Strategy Decisions | 19. No-Trade Reasons | 20. Trade Deep Dive | 21. Greeks Attribution | 22. IV Crush | 23. Slippage | 24. Cost by Strategy | 25. P&L Curve | 26. Option Chain | 27. Candle Stats | 28. API Health | 29. Data Quality | 30. Calibration Drift | 31. Cumulative Performance | 32. Prior Days | 33. Audit Warnings | 34. Master Timeline | 35. Daily Summary | 36. LLM Context | 37. Raw Export\n")

    md.append("## 2. Executive Summary\n")
    exec_summary = {
        "Day label":                   session_state.get("day_label") if session_state else "N/A",
        "Day mode":                    session_state.get("day_mode") if session_state else "N/A",
        "VIX regime":                  session_state.get("vix_regime") if session_state else "N/A",
        "OR condition / width":        f"{session_state.get('or_condition')} / {session_state.get('or_width')}" if session_state else "N/A",
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
        "NIFTY range":                 f"{spot_profile.get('range_pts')}pts ({spot_profile.get('range_pct')}%)" if spot_profile else "N/A",
        "VRP mean today (pp)":         vrp_stats.get("vrp_mean"),
        "ATM IV open/close":           f"{vrp_stats.get('atm_iv_open_pct')} / {vrp_stats.get('atm_iv_close_pct')}",
        "IV crush today (pp)":         vrp_stats.get("iv_crush_pct"),
        "VIX open/close":              f"{vix_profile.get('vix_open')} / {vix_profile.get('vix_close')}",
        "VIX readings today":          len(vix_history_today),
        "Market snapshots today":      len(market_snaps_today),
        "Option chain snapshot rows":  len(chain_rows),
        "API calls made":              len(api_rows),
        "Audit log lines":             len(audit_file_lines),
    }
    md.append(md_kv(exec_summary))

    md.append("## 3. Auto-Detected Anomalies\n")
    md.append("\n".join(f"- {f}" for f in anomalies) + "\n")

    md.append("## 4. Calibration Status\n")
    md.append(md_kv(calibration_summary))

    md.append("## 5. Session Configuration\n")
    md.append(md_kv(session_state) if session_state else "_No session_state row found._\n")

    md.append("## 6. NIFTY Intraday Profile\n")
    md.append(md_kv(spot_profile))

    md.append("## 7. VRP / Volatility Deep Dive\n")
    md.append(md_kv(vrp_stats))
    md.append("\n**VRP thresholds (NIFTY 2026 calibrated):** VERY_RICH > 3.75pp. RICH 3.0-3.75pp. FAIR 1.5-3.0pp. THIN 0-1.5pp. CHEAP < 0. Parkinson RV uses 375 bars/day annualization.\n")

    md.append("## 8. Intraday VRP Curve\n")
    if vrp_curve:
        md.append(md_table(vrp_curve, ["time", "vrp", "atm_iv_pct", "parkinson_rv_pct", "volatility_condition"], max_rows=100))
    else:
        md.append("_No VRP curve data._\n")

    md.append("## 9. ADX / Trend Profile\n")
    md.append(md_kv(adx_profile))

    md.append("## 10. PCR / Skew / Directional Profile\n")
    md.append(md_kv(pcr_profile))
    md.append(md_kv(skew_profile))

    md.append("## 11. Opening Range Analysis\n")
    md.append(md_kv(or_analysis))

    md.append("## 12. VIX Intraday Profile\n")
    md.append(md_kv(vix_profile))

    md.append("## 13. Regime Engine Timeline\n")
    if regime_timeline:
        md.append(md_table(regime_timeline, ["time", "source", "final_regime", "confidence", "size_multiplier", "vol_regime", "price_regime_15", "positioning", "adx_15", "ema_structure", "calibration_tier", "notes"], max_rows=100))
    else:
        md.append("_No regime_decisions rows found._\n")

    md.append("## 14. Regime Distribution Today\n")
    for regime, count in regime_dist.items():
        md.append(f"- **{regime}**: {count} decisions\n")

    md.append("## 15. Regime Accuracy Scoring\n")
    if regime_accuracy:
        for regime, acc in regime_accuracy.items():
            md.append(f"- **{regime}**: {acc.get('WIN',0)}/{acc['total']} wins ({acc['win_rate_pct']}%)\n")
    else:
        md.append("_No regime accuracy data — need trades to score._\n")

    md.append("## 16. Market Data Timeline (cycle_log)\n")
    md.append(f"Total cycles: {len(cycle_rows)}.\n")
    md.append(md_table(cycle_rows, ["cycle_time", "spot", "vix", "vrp", "atm_iv_pct", "parkinson_rv_pct", "adx", "adx_condition", "vwap_dist_pct", "pcr", "skew_ratio", "or_condition", "volatility_condition", "trend_condition", "direction", "final_regime", "confidence", "action_taken", "no_trade_reason", "open_positions", "daily_pnl_net"], max_rows=100))

    md.append("## 17. Gate Blockage Analysis\n")
    gate_categories = {
        "risk_gates":       ["daily_loss_limit", "max_entries", "max_concurrent", "consecutive_stops", "stop_cooldown"],
        "data_gates":       ["vrp_unknown", "opening_range_not", "or_pending", "chain_unavailable"],
        "market_condition": ["circuit_breaker", "vix_spike", "iv_expanding", "iv_spiking"],
        "timing_gates":     ["before_entry_window", "past_entry_window", "0dte", "hard_exit"],
        "strategy_gates":   ["no_conditions_met", "params_invalid", "strategy_rules_failed", "credit_"],
        "event_gates":      ["event_day", "vix_suppressed", "very_wide_or"],
        "regime_gates":     ["regime_engine", "EMERGENCY_EXIT", "NO_TRADE: Choppy", "NO_TRADE: Before", "NO_TRADE: Past", "OBSERVING"],
    }
    categorized = {cat: 0 for cat in gate_categories}
    categorized["other"] = 0
    for reason, count in no_trade_reasons.items():
        matched = False
        for cat, keywords in gate_categories.items():
            if any(kw in reason for kw in keywords):
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
        "no_trade_rate_pct": round(no_trade_count / total_decisions * 100, 1) if total_decisions > 0 else 0,
        "gate_category_counts": categorized,
    }))
    md.append("\n**Top 10 no-trade reasons:**\n")
    for reason, count in list(no_trade_reasons.items())[:10]:
        md.append(f"- [{count}x] {reason}\n")

    md.append("## 18. Strategy Decisions\n")
    md.append(md_table(decisions, ["decision_time", "action", "strategy_name", "reason"], max_rows=100))

    md.append("## 19. No-Trade Reason Frequency\n")
    if no_trade_reasons:
        for reason, count in no_trade_reasons.items():
            md.append(f"- [{count}x] {reason}\n")
    else:
        md.append("_No NO_TRADE decisions recorded._\n")

    md.append("## 20. Trade Deep Dive\n")
    if not trade_entries:
        md.append("_No trades entered._\n")
    for t in trade_entries:
        pid = t.get("position_id")
        md.append(f"\n### Position `{pid}` — {t.get('strategy_name')}\n")
        md.append(md_kv({
            "Entry time":            t.get("entry_time"),
            "Day label":             t.get("day_label"),
            "Selection reason":      t.get("selection_reason"),
            "Entry spot/VIX/VRP":    f"{t.get('entry_spot')} / {t.get('entry_vix')} / {t.get('entry_vrp')}",
            "ATM IV at entry":       t.get("entry_atm_iv"),
            "ADX at entry":          t.get("entry_adx"),
            "VWAP dist at entry":    t.get("entry_vwap_dist"),
            "PCR at entry":          t.get("entry_pcr"),
            "Skew at entry":         t.get("entry_skew_ratio"),
            "Volatility condition":  t.get("volatility_condition"),
            "IV behavior":           t.get("iv_behavior"),
            "Trend condition":       t.get("trend_condition"),
            "ADX condition":         t.get("adx_condition"),
            "Direction":             t.get("direction"),
            "Final regime":          t.get("final_regime_at_entry"),
            "Confidence":            t.get("confidence_at_entry"),
            "Defined risk only":     t.get("defined_risk_only"),
            "Event day":             t.get("event_day"),
            "OR condition/width":    f"{t.get('or_condition')} / {t.get('or_width')}",
            "Target expiry/DTE":     f"{t.get('target_expiry')} / {t.get('actual_dte')}",
            "Entry credit/debit":    t.get("entry_credit") or t.get("entry_debit"),
            "Gross credit":          t.get("gross_credit"),
            "Entry costs (Rs)":      t.get("entry_costs_rupees"),
            "Stop premium":          t.get("stop_premium"),
            "Target premium":        t.get("target_premium"),
            "Price stop (pts)":      t.get("price_stop_pts"),
            "Final lots":            t.get("final_lots"),
            "Max loss/lot (Rs)":     t.get("max_loss_per_lot"),
            "Total max risk (Rs)":   t.get("total_max_risk"),
            "Capital at entry":      t.get("capital_at_entry"),
            "Daily P&L at entry":    t.get("daily_pnl_at_entry"),
        }))
        try:
            leg_list = json.loads(t["legs_json"]) if t.get("legs_json") else []
        except Exception:
            leg_list = []
        if leg_list:
            md.append("\n**Legs at entry:**\n")
            md.append(md_table(leg_list, ["action", "option_type", "strike", "exec_price", "delta", "gamma", "vega", "theta", "iv", "oi"]))
        exit_row = exits_by_position.get(pid)
        if exit_row:
            md.append("\n**Exit:**\n")
            md.append(md_kv({
                "Exit time":          exit_row.get("exit_time"),
                "Exit reason":        exit_row.get("exit_reason"),
                "Hold time (min)":    exit_row.get("hold_minutes"),
                "Exit premium":       exit_row.get("exit_premium"),
                "Exit spot":          exit_row.get("exit_spot"),
                "Exit VIX":           exit_row.get("exit_vix"),
                "Gross P&L (pts/Rs)": f"{exit_row.get('gross_pnl_pts')} / {exit_row.get('gross_pnl_rupees')}",
                "Exit costs (Rs)":    exit_row.get("exit_costs_rupees"),
                "Total costs (Rs)":   exit_row.get("total_costs_rupees"),
                "Net P&L (pts/Rs/%)": f"{exit_row.get('net_pnl_pts')} / {exit_row.get('net_pnl_rupees')} / {exit_row.get('net_pnl_pct')}",
                "Result":             exit_row.get("result"),
                "Profit pct credit":  exit_row.get("profit_pct_of_credit"),
            }))
            crush = iv_crush_by_pos.get(pid, {})
            if crush:
                md.append(md_kv({"IV crush": f"entry={crush.get('entry_atm_iv_pct')}% exit={crush.get('exit_atm_iv_pct')}% crush={crush.get('iv_crush_pct')}% [{crush.get('direction')}]"}))
        else:
            md.append("\n**Exit:** _No matching exit row — position may still be open._\n")

    md.append("## 21. Greeks P&L Attribution\n")
    if greeks_attr:
        md.append(md_table(greeks_attr, ["position_id", "strategy_name", "net_pnl_rupees", "hold_minutes", "entry_vrp", "exit_vrp", "vrp_change", "estimated_theta_pts", "result", "exit_reason"]))
    else:
        md.append("_No trades._\n")

    md.append("## 22. IV Crush Per Trade\n")
    for pid, crush in iv_crush_by_pos.items():
        if crush:
            md.append(f"- **{str(pid)[:16]}**: entry={crush.get('entry_atm_iv_pct')}% exit={crush.get('exit_atm_iv_pct')}% crush={crush.get('iv_crush_pct')}% [{crush.get('direction')}]\n")

    md.append("## 23. Slippage Analysis\n")
    md.append(md_kv(slippage_analysis))

    md.append("## 24. Cost Breakdown by Strategy\n")
    for strat, cb in cost_by_strat.items():
        md.append(f"\n### {strat}\n")
        md.append(md_kv(cb))

    md.append("## 25. P&L Curve (intraday)\n")
    if pnl_curve:
        md.append(md_table(pnl_curve, ["time", "pnl", "spot", "vrp", "adx", "regime"], max_rows=100))
    else:
        md.append("_No P&L curve data._\n")

    md.append("## 26. Option Chain Statistics\n")
    md.append(md_kv(chain_summary))
    md.append(f"\n_Full chain ({len(chain_rows)} rows) in raw JSON export._\n")

    md.append("## 27. Intraday 1-Minute Candle Statistics\n")
    md.append(md_kv(candle_stats) if candle_stats else "_No intraday candle data._\n")
    md.append(f"\n_Total 1-min bars: {len(intraday_candles)}._\n")

    md.append("## 28. API Call Health\n")
    md.append(md_kv({k: v for k, v in api_summary.items() if k != "errors_sample"}))
    if api_summary.get("errors_sample"):
        md.append("\n**Sample API errors:**\n")
        md.append(md_table(api_summary["errors_sample"], ["call_time", "category", "endpoint", "status_code", "error_message"]))

    md.append("## 29. Data Quality Checks\n")
    dq = {
        "Cycles missing spot":           sum(1 for c in cycle_rows if c.get("spot") is None),
        "Cycles with day_move_used>=70pct": sum(1 for c in cycle_rows if (c.get("day_move_used_pct") or 0) >= 70.0),
        "Max day_move_used_pct today":   round(max((c.get("day_move_used_pct") or 0 for c in cycle_rows), default=0), 1),
        "Cycles with stale chain":        sum(1 for c in cycle_rows if c.get("chain_stale")),
        "Stale chain pct":               round(sum(1 for c in cycle_rows if c.get("chain_stale")) / len(cycle_rows) * 100, 1) if cycle_rows else 0,
        "Cycles missing VIX":            sum(1 for c in cycle_rows if c.get("vix") is None),
        "Cycles missing VRP":            sum(1 for c in cycle_rows if c.get("vrp") is None),
        "Cycles missing PCR":            sum(1 for c in cycle_rows if c.get("pcr") is None),
        "Cycles no final_regime":        sum(1 for c in cycle_rows if not c.get("final_regime")),
        "Cycles vol_condition=UNKNOWN":  sum(1 for c in cycle_rows if c.get("volatility_condition") == "UNKNOWN"),
        "Cycles trend=OR_PENDING":       sum(1 for c in cycle_rows if c.get("trend_condition") == "OR_PENDING"),
        "Regime decisions logged":       len(regime_decisions),
        "Chain rows zero bid/ask":       chain_summary.get("zero_bid_ask_count", 0),
        "Trades no exit row":            sum(1 for t in trade_entries if t["position_id"] not in exits_by_position),
        "Positions still OPEN":          sum(1 for p in positions if p.get("status") == "OPEN"),
        "VIX history rows today":        len(vix_history_today),
        "Market snapshots today":        len(market_snaps_today),
        "Calibration tier":              calibration_summary.get("calibration_tier"),
        "Calibration valid":             calibration_summary.get("is_valid"),
    }
    md.append(md_kv(dq))

    md.append("## 30. Calibration Drift Tracking\n")
    if calibration_drift:
        md.append(md_table(calibration_drift, ["calibrated_at", "calibration_tier", "is_valid", "vix_p50", "vix_p75", "skew_bearish_threshold", "oi_buildup_threshold", "straddle_ratio_sell", "vrp_sell_threshold", "day_size_tuesday"], max_rows=20))
    else:
        md.append("_No calibration history._\n")

    md.append("## 31. Cumulative Performance (90-day)\n")
    if equity_curve:
        md.append(md_kv({k: v for k, v in equity_curve.items() if k != "daily_pnl_series"}))
        if equity_curve.get("daily_pnl_series"):
            md.append("\n**Daily P&L Series:**\n")
            md.append(md_table(equity_curve["daily_pnl_series"], ["date", "pnl", "capital"], max_rows=90))
    else:
        md.append("_No cumulative data yet._\n")

    md.append("## 32. Prior Days Comparison\n")
    if prior_days_summary:
        md.append(md_table(prior_days_summary, ["trading_date", "day_label", "trades_executed", "win_rate_pct", "net_pnl_rupees", "net_pnl_pct_capital", "vrp_mean", "or_condition", "stops_fired", "profit_factor", "capital_end"], max_rows=10))
    else:
        md.append("_No prior days data._\n")

    md.append("## 33. Audit Log — Warnings and Errors\n")
    md.append(f"WARNING/ERROR/CRITICAL: {len(warning_error_lines)} of {len(audit_file_lines)} total ({len(audit_db_rows)} in DB)\n\n")
    if warning_error_lines:
        md.append("```\n" + "\n".join(warning_error_lines[:200]) + "\n```\n")
        if len(warning_error_lines) > 200:
            md.append(f"_... {len(warning_error_lines) - 200} more lines in raw export._\n")
    else:
        md.append("_No warnings or errors logged today._\n")

    md.append("## 34. Unified Master Timeline\n")
    md.append(md_table([{"time": e[0], "type": e[1], "detail": e[2]} for e in timeline], ["time", "type", "detail"], max_rows=200))

    md.append("## 35. Daily Summary (engine EOD)\n")
    md.append(md_kv(daily_summary) if daily_summary else "_No daily_summary row found._\n")

    md.append("## 36. LLM Analysis Context\n")
    md.append(f"""
**For AI/LLM analysis — NIFTY intraday options engine v2.0 — {target_date}:**

Net P&L: Rs{net_pnl} | Trades: {len(trade_entries)} entered {len(trade_exits)} closed | Win Rate: {round(wins/len(trade_exits)*100,1) if trade_exits else 0}%
VIX: {vix_profile.get('vix_open')} -> {vix_profile.get('vix_close')} | VIX range: {vix_profile.get('vix_range')}
OR: {or_analysis.get('or_condition')} {or_analysis.get('or_width_pts')}pts | VRP mean: {vrp_stats.get('vrp_mean')}pp
Calibration: Tier {calibration_summary.get('calibration_tier')} valid={calibration_summary.get('is_valid')}
Regime decisions: {len(regime_decisions)} | Cycles: {len(cycle_rows)} | VRP curve points: {len(vrp_curve)}

Key questions for LLM:
1. Was regime classification accurate given actual market outcome?
2. Were entry/exit timings optimal given VRP curve?
3. Did IV crush work in favor of the strategy?
4. Were transaction costs proportionate to gross P&L?
5. What calibration improvements would improve future performance?
6. Were there missed opportunities in the cycle log?
7. Was stop loss management appropriate given day volatility?
8. Which regime had the best accuracy today?
9. Was the OR condition predictive of the day type?
10. Did VIX regime correctly size positions?
11. What does the VRP curve suggest about premium richness timing?
12. Were the calibrated VRP thresholds appropriate for today's market?
""")

    md.append("## 37. Raw Data Export Manifest\n")
    md.append(f"All tables exported to: `eod_report_{target_date}_raw/`\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"eod_report_{target_date}.md"
    report_path.write_text("\n".join(md), encoding="utf-8")

    raw_dir = OUTPUT_DIR / f"eod_report_{target_date}_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    raw_exports = {
        "session_state":              session_state,
        "cycle_log":                  cycle_rows,
        "strategy_decisions":         decisions,
        "positions":                  positions,
        "position_legs":              legs,
        "trade_entries":              trade_entries,
        "trade_exits":                trade_exits,
        "daily_summary":              daily_summary,
        "option_chain_snapshot":      chain_rows,
        "api_call_log":               api_rows,
        "audit_log_db":               audit_db_rows,
        "audit_log_file_lines":       audit_file_lines,
        "regime_decisions":           regime_decisions,
        "calibration_state":          calibration_row,
        "calibration_drift":          calibration_drift,
        "vix_history_today":          vix_history_today,
        "market_snapshots_today":     market_snaps_today,
        "master_timeline":            timeline,
        "anomaly_flags":              anomalies,
        "no_trade_reason_counts":     no_trade_reasons,
        "strategies_used_counts":     strategies_used,
        "chain_summary":              chain_summary,
        "vrp_statistics":             vrp_stats,
        "vrp_curve":                  vrp_curve,
        "spot_profile":               spot_profile,
        "adx_profile":                adx_profile,
        "pcr_profile":                pcr_profile,
        "skew_profile":               skew_profile,
        "pnl_curve":                  pnl_curve,
        "regime_timeline":            regime_timeline,
        "regime_distribution":        regime_dist,
        "regime_accuracy":            regime_accuracy,
        "calibration_summary":        calibration_summary,
        "intraday_candles_1min":      intraday_candles,
        "candle_statistics":          candle_stats,
        "cumulative_performance_90d": equity_curve,
        "iv_crush_per_trade":         iv_crush_by_pos,
        "slippage_analysis":          slippage_analysis,
        "cost_by_strategy":           cost_by_strat,
        "greeks_pnl_attribution":     greeks_attr,
        "or_analysis":                or_analysis,
        "vix_intraday_profile":       vix_profile,
        "api_summary":                {k: v for k, v in api_summary.items() if k != "errors_sample"},
        "dte_performance_raw": {
            str(dte): {
                "trades": sum(1 for t in trade_entries if (t.get("actual_dte") or 99) == dte),
                "wins": sum(1 for t in trade_entries if (t.get("actual_dte") or 99) == dte and exits_by_pos.get(t.get("position_id"), {}).get("result") == "WIN"),
                "losses": sum(1 for t in trade_entries if (t.get("actual_dte") or 99) == dte and exits_by_pos.get(t.get("position_id"), {}).get("result") == "LOSS"),
                "net_pnl": round(sum(exits_by_pos.get(t.get("position_id"), {}).get("net_pnl_rupees", 0) or 0 for t in trade_entries if (t.get("actual_dte") or 99) == dte), 2),
            }
            for dte in [0, 1, 2, 3, 4, 5, 6]
        },
        "llm_analysis_context": {
            "target_date":         target_date,
            "net_pnl_rupees":      net_pnl,
            "gross_pnl_rupees":    gross_pnl,
            "total_costs_rupees":  total_costs,
            "trades_entered":      len(trade_entries),
            "trades_closed":       len(trade_exits),
            "wins":                wins,
            "losses":              losses,
            "win_rate_pct":        round(wins / len(trade_exits) * 100, 1) if trade_exits else 0,
            "vix_profile":         vix_profile,
            "or_analysis":         or_analysis,
            "regime_accuracy":     regime_accuracy,
            "calibration_tier":    calibration_summary.get("calibration_tier"),
            "vrp_curve_summary": {
                "count":   len(vrp_curve),
                "min_vrp": min((v["vrp"] for v in vrp_curve), default=None),
                "max_vrp": max((v["vrp"] for v in vrp_curve), default=None),
                "avg_vrp": round(statistics.mean(v["vrp"] for v in vrp_curve), 3) if vrp_curve else None,
            },
            "anomaly_count":       len([f for f in anomalies if "[FLAG]" in f]),
            "dte_breakdown": {
                "dte_0_trades": sum(1 for t in trade_entries if (t.get("actual_dte") or 99) == 0),
                "dte_1_trades": sum(1 for t in trade_entries if (t.get("actual_dte") or 99) == 1),
                "dte_2plus_trades": sum(1 for t in trade_entries if (t.get("actual_dte") or 0) >= 2),
                "dte_0_wins": sum(1 for t in trade_entries if (t.get("actual_dte") or 99) == 0 and exits_by_pos.get(t.get("position_id"), {}).get("result") == "WIN"),
                "dte_1_wins": sum(1 for t in trade_entries if (t.get("actual_dte") or 99) == 1 and exits_by_pos.get(t.get("position_id"), {}).get("result") == "WIN"),
                "dte_2plus_wins": sum(1 for t in trade_entries if (t.get("actual_dte") or 0) >= 2 and exits_by_pos.get(t.get("position_id"), {}).get("result") == "WIN"),
            },
            "day_move_used_at_entries": [
                {"time": t.get("entry_time"), "day_move_used_pct": None}
                for t in trade_entries
            ],
            "chain_stale_cycles":  sum(1 for c in cycle_rows if c.get("chain_stale")),
            "chain_stale_pct":     round(sum(1 for c in cycle_rows if c.get("chain_stale")) / len(cycle_rows) * 100, 1) if cycle_rows else 0,
            "iv_crush_summary":    {k: v for k, v in iv_crush_by_pos.items() if v},
            "greeks_attribution":  greeks_attr,
            "slippage_analysis":   slippage_analysis,
        },
    }

    for name, data_val in raw_exports.items():
        (raw_dir / f"{name}.json").write_text(json.dumps(data_val, indent=2, default=str), encoding="utf-8")

    print(f"Report: {report_path}")
    print(f"Raw: {raw_dir}/")
    print(f"Net P&L: Rs{net_pnl} | Trades: {len(trade_entries)} | Anomalies: {len([f for f in anomalies if '[FLAG]' in f])}")
    print("\nQuick summary:")
    for k, v in exec_summary.items():
        print(f"  {k}: {v}")
    print("\nAnomaly flags:")
    for f in anomalies:
        print(f"  {f}")


if __name__ == "__main__":
    import sys
    td = TARGET_DATE
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--date" and i + 1 < len(args):
            td = args[i + 1]
    generate_report(td)
