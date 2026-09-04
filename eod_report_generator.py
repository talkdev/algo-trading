import glob
import json
import statistics
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

TARGET_DATE = "2026-09-04"

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

LOT_SIZE = int(_ENV.get("NIFTY_LOT_SIZE", "75") or 75)
STT_RATE = float(_ENV.get("STT_RATE", "0.0015") or 0.0015)
EXCHANGE_TXN_RATE = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003552") or 0.0003552)
BROKERAGE_PER_ORDER = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)
STARTING_CAPITAL = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1000000)
MAX_DAILY_LOSS_PCT = float(_ENV.get("MAX_DAILY_LOSS_PCT", "0.02") or 0.02)


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
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def q(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


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
    return q(conn, "SELECT * FROM trade_exits WHERE exit_time LIKE ? ORDER BY exit_time", (f"{d}%",))


def fetch_daily_summary(conn, d):
    if not table_exists(conn, "daily_summary"):
        return None
    rows = q(conn, "SELECT * FROM daily_summary WHERE trading_date=?", (d,))
    return rows[0] if rows else None


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


def fetch_prior_days_cycle_stats(conn, d, n=5):
    if not table_exists(conn, "cycle_log"):
        return []
    return q(conn, "SELECT trading_date, AVG(vrp) as avg_vrp, AVG(adx) as avg_adx, AVG(vix) as avg_vix, AVG(spot) as avg_spot, COUNT(*) as cycle_count FROM cycle_log WHERE trading_date < ? GROUP BY trading_date ORDER BY trading_date DESC LIMIT ?", (d, n))


def fetch_all_exits_for_strategy_analysis(conn, d, lookback_days=30):
    if not table_exists(conn, "trade_exits"):
        return []
    try:
        cutoff = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    except Exception:
        return []
    return q(conn, "SELECT te.*, p.strategy_name, p.trading_date, p.entry_credit, p.entry_debit, p.final_lots, p.entry_vix, p.entry_vrp, ent.volatility_condition, ent.trend_condition FROM trade_exits te JOIN positions p ON te.position_id = p.position_id LEFT JOIN trade_entries ent ON te.position_id = ent.position_id WHERE p.trading_date >= ? AND p.trading_date <= ? ORDER BY te.exit_time", (cutoff, d))


def fetch_chain_atm_history(conn, d):
    if not table_exists(conn, "option_chain_snapshot"):
        return []
    return q(conn, "SELECT capture_time, strike, option_type, bid, ask, ltp, iv, delta, oi, volume FROM option_chain_snapshot WHERE trading_date=? AND ABS(delta) BETWEEN 0.35 AND 0.65 ORDER BY capture_time, strike", (d,))


def fetch_chain_wing_history(conn, d):
    if not table_exists(conn, "option_chain_snapshot"):
        return []
    return q(conn, "SELECT capture_time, strike, option_type, bid, ask, ltp, iv, delta, oi, volume FROM option_chain_snapshot WHERE trading_date=? AND ABS(delta) BETWEEN 0.15 AND 0.35 ORDER BY capture_time, strike", (d,))


def filter_log_lines_by_level(lines, levels):
    out = []
    for line in lines:
        parts = line.split("|")
        if len(parts) >= 2 and parts[1].strip() in levels:
            out.append(line)
    return out


def detect_cycle_gaps(cycle_rows, threshold_minutes=10.0):
    gaps = []
    for i in range(1, len(cycle_rows)):
        try:
            t1 = datetime.fromisoformat(cycle_rows[i - 1]["cycle_time"])
            t2 = datetime.fromisoformat(cycle_rows[i]["cycle_time"])
            diff_min = (t2 - t1).total_seconds() / 60.0
            if diff_min > threshold_minutes:
                gaps.append((cycle_rows[i - 1]["cycle_time"], cycle_rows[i]["cycle_time"], diff_min))
        except Exception:
            continue
    return gaps


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
        counts[t["strategy_name"]] = counts.get(t["strategy_name"], 0) + 1
    return counts


def recompute_cost_breakdown(sell_pts, buy_pts, num_orders, lots, lot_size, stt_rate, exch_rate, brokerage):
    turnover = sell_pts + buy_pts
    stt = sell_pts * lot_size * lots * stt_rate
    exchange = turnover * lot_size * lots * exch_rate
    brokerage_total = brokerage * num_orders
    sebi_fee = turnover * lot_size * lots * 0.000001
    stamp = buy_pts * lot_size * lots * 0.00003
    gst = (brokerage_total + exchange + sebi_fee) * 0.18
    total = stt + exchange + brokerage_total + sebi_fee + stamp + gst
    return {"stt": round(stt, 2), "exchange": round(exchange, 2), "brokerage": round(brokerage_total, 2), "sebi": round(sebi_fee, 4), "stamp": round(stamp, 4), "gst": round(gst, 2), "total": round(total, 2)}


def analyze_trade_costs(trade_entries, trade_exits):
    exits_by_pos = {e["position_id"]: e for e in trade_exits}
    results = []
    for t in trade_entries:
        try:
            legs = json.loads(t["legs_json"]) if t.get("legs_json") else []
        except Exception:
            legs = []
        sell_pts = sum(l.get("exec_price", 0) or 0 for l in legs if l.get("action") == "SELL")
        buy_pts = sum(l.get("exec_price", 0) or 0 for l in legs if l.get("action") == "BUY")
        lots = t.get("final_lots") or 1
        breakdown = recompute_cost_breakdown(sell_pts, buy_pts, len(legs), lots, LOT_SIZE, STT_RATE, EXCHANGE_TXN_RATE, BROKERAGE_PER_ORDER)
        exit_row = exits_by_pos.get(t["position_id"])
        cost_discrepancy = None
        if exit_row:
            recorded_total = (t.get("entry_costs_rupees") or 0) + (exit_row.get("exit_costs_rupees") or 0)
            cost_discrepancy = round(recorded_total - breakdown["total"], 2)
        results.append({
            "position_id": t["position_id"],
            "strategy": t["strategy_name"],
            "recomputed_entry_costs": breakdown,
            "recorded_entry_costs_rupees": t.get("entry_costs_rupees"),
            "recorded_exit_costs_rupees": exit_row.get("exit_costs_rupees") if exit_row else None,
            "cost_discrepancy_rupees": cost_discrepancy,
            "net_pnl_rupees": exit_row.get("net_pnl_rupees") if exit_row else None,
            "gross_pnl_rupees": exit_row.get("gross_pnl_rupees") if exit_row else None,
            "result": exit_row.get("result") if exit_row else "STILL_OPEN_OR_MISSING_EXIT",
            "hold_minutes": exit_row.get("hold_minutes") if exit_row else None,
            "exit_reason": exit_row.get("exit_reason") if exit_row else None,
        })
    return results


def summarize_option_chain(chain_rows):
    if not chain_rows:
        return {"total_rows": 0}
    capture_times = sorted(set(r["capture_time"] for r in chain_rows if r.get("capture_time")))
    strikes = sorted(set(r["strike"] for r in chain_rows if r.get("strike") is not None))
    zero_bid_ask = sum(1 for r in chain_rows if (r.get("bid") or 0) == 0 and (r.get("ask") or 0) == 0)
    spreads = [(r["ask"] - r["bid"]) for r in chain_rows if (r.get("bid") or 0) > 0 and (r.get("ask") or 0) > 0]
    ivs = [r["iv"] for r in chain_rows if r.get("iv") and r["iv"] > 0]
    call_ois = [r["oi"] for r in chain_rows if r.get("option_type") == "call" and r.get("oi")]
    put_ois = [r["oi"] for r in chain_rows if r.get("option_type") == "put" and r.get("oi")]
    total_call_oi = sum(call_ois)
    total_put_oi = sum(put_ois)
    chain_pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else None
    return {
        "total_rows": len(chain_rows),
        "unique_capture_times": len(capture_times),
        "unique_strikes": len(strikes),
        "strike_range": [min(strikes), max(strikes)] if strikes else None,
        "zero_bid_ask_count": zero_bid_ask,
        "zero_bid_ask_pct": round(zero_bid_ask / len(chain_rows) * 100, 1) if chain_rows else 0,
        "avg_spread": round(statistics.mean(spreads), 3) if spreads else None,
        "max_spread": round(max(spreads), 3) if spreads else None,
        "avg_iv_pct": round(statistics.mean(ivs) * 100, 2) if ivs else None,
        "iv_range_pct": [round(min(ivs) * 100, 2), round(max(ivs) * 100, 2)] if ivs else None,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "chain_pcr": chain_pcr,
        "first_capture": capture_times[0] if capture_times else None,
        "last_capture": capture_times[-1] if capture_times else None,
    }


def summarize_api_calls(api_rows):
    if not api_rows:
        return {"total_calls": 0}
    by_category = {}
    errors = []
    response_times = []
    rate_limited = 0
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
        "total_calls": len(api_rows),
        "by_category": by_category,
        "error_count": len(errors),
        "rate_limited_count": rate_limited,
        "avg_response_ms": round(statistics.mean(response_times), 1) if response_times else None,
        "p95_response_ms": round(sorted(response_times)[int(len(response_times) * 0.95)], 1) if len(response_times) > 5 else None,
        "max_response_ms": round(max(response_times), 1) if response_times else None,
        "slow_calls_over_2s": sum(1 for t in response_times if t > 2000),
        "errors_sample": errors[:20],
    }


def compute_vrp_statistics(cycle_rows):
    vrps = [c["vrp"] for c in cycle_rows if c.get("vrp") is not None]
    ivs = [c["atm_iv_pct"] for c in cycle_rows if c.get("atm_iv_pct") is not None]
    rvs = [c["parkinson_rv_pct"] for c in cycle_rows if c.get("parkinson_rv_pct") is not None]
    if not vrps:
        return {}
    result = {
        "vrp_mean": round(statistics.mean(vrps), 3),
        "vrp_min": round(min(vrps), 3),
        "vrp_max": round(max(vrps), 3),
        "vrp_stdev": round(statistics.stdev(vrps), 3) if len(vrps) > 1 else 0,
        "vrp_positive_cycles": sum(1 for v in vrps if v > 0),
        "vrp_negative_cycles": sum(1 for v in vrps if v <= 0),
        "vrp_rich_cycles": sum(1 for v in vrps if v > 3.0),
        "vrp_very_rich_cycles": sum(1 for v in vrps if v > 5.0),
        "vrp_cheap_cycles": sum(1 for v in vrps if v < 0),
        "total_vrp_cycles": len(vrps),
    }
    if ivs:
        result["atm_iv_open_pct"] = round(ivs[0], 3)
        result["atm_iv_close_pct"] = round(ivs[-1], 3)
        result["atm_iv_mean_pct"] = round(statistics.mean(ivs), 3)
        result["atm_iv_min_pct"] = round(min(ivs), 3)
        result["atm_iv_max_pct"] = round(max(ivs), 3)
        result["iv_crush_pct"] = round(ivs[0] - ivs[-1], 3) if len(ivs) > 1 else 0
    if rvs:
        result["parkinson_rv_mean_pct"] = round(statistics.mean(rvs), 3)
        result["parkinson_rv_min_pct"] = round(min(rvs), 3)
        result["parkinson_rv_max_pct"] = round(max(rvs), 3)
    return result


def compute_intraday_spot_profile(cycle_rows):
    spots = [(c["cycle_time"], c["spot"]) for c in cycle_rows if c.get("spot") is not None]
    if not spots:
        return {}
    spot_values = [s[1] for s in spots]
    result = {
        "open": spot_values[0],
        "close": spot_values[-1],
        "high": max(spot_values),
        "low": min(spot_values),
        "range_pts": round(max(spot_values) - min(spot_values), 2),
        "range_pct": round((max(spot_values) - min(spot_values)) / spot_values[0] * 100, 3),
        "net_change_pts": round(spot_values[-1] - spot_values[0], 2),
        "net_change_pct": round((spot_values[-1] - spot_values[0]) / spot_values[0] * 100, 3),
        "direction": "UP" if spot_values[-1] > spot_values[0] else ("DOWN" if spot_values[-1] < spot_values[0] else "FLAT"),
    }
    if len(spot_values) >= 6:
        mid = len(spot_values) // 2
        first_half_range = max(spot_values[:mid]) - min(spot_values[:mid])
        second_half_range = max(spot_values[mid:]) - min(spot_values[mid:])
        result["first_half_range_pts"] = round(first_half_range, 2)
        result["second_half_range_pts"] = round(second_half_range, 2)
        result["volatility_expansion_second_half"] = second_half_range > first_half_range
    return result


def compute_adx_profile(cycle_rows):
    adx_vals = [c["adx"] for c in cycle_rows if c.get("adx") is not None]
    adx_conds = [c["adx_condition"] for c in cycle_rows if c.get("adx_condition")]
    if not adx_vals:
        return {}
    cond_counts = {}
    for cond in adx_conds:
        cond_counts[cond] = cond_counts.get(cond, 0) + 1
    return {
        "adx_open": round(adx_vals[0], 2),
        "adx_close": round(adx_vals[-1], 2),
        "adx_mean": round(statistics.mean(adx_vals), 2),
        "adx_max": round(max(adx_vals), 2),
        "adx_min": round(min(adx_vals), 2),
        "adx_condition_distribution": cond_counts,
        "trending_cycles": sum(1 for v in adx_vals if v > 25),
        "strong_trend_cycles": sum(1 for v in adx_vals if v > 32),
        "flat_cycles": sum(1 for v in adx_vals if v < 20),
    }


def compute_pcr_profile(cycle_rows):
    pcrs = [c["pcr"] for c in cycle_rows if c.get("pcr") is not None]
    if not pcrs:
        return {}
    result = {
        "pcr_open": round(pcrs[0], 3),
        "pcr_close": round(pcrs[-1], 3),
        "pcr_mean": round(statistics.mean(pcrs), 3),
        "pcr_min": round(min(pcrs), 3),
        "pcr_max": round(max(pcrs), 3),
        "pcr_change_open_to_close": round(pcrs[-1] - pcrs[0], 3),
        "extreme_fear_cycles": sum(1 for p in pcrs if p > 1.5),
        "extreme_greed_cycles": sum(1 for p in pcrs if p < 0.7),
        "neutral_cycles": sum(1 for p in pcrs if 0.8 <= p <= 1.3),
    }
    return result


def compute_skew_profile(cycle_rows):
    skews = [c["skew_ratio"] for c in cycle_rows if c.get("skew_ratio") is not None]
    if not skews:
        return {}
    return {
        "skew_open": round(skews[0], 3),
        "skew_close": round(skews[-1], 3),
        "skew_mean": round(statistics.mean(skews), 3),
        "skew_min": round(min(skews), 3),
        "skew_max": round(max(skews), 3),
        "fear_skew_cycles": sum(1 for s in skews if s > 1.25),
        "complacent_skew_cycles": sum(1 for s in skews if s < 0.95),
    }


def compute_historical_performance(prior_exits):
    if not prior_exits:
        return {}
    by_strategy = {}
    for e in prior_exits:
        sname = e.get("strategy_name") or "unknown"
        if sname not in by_strategy:
            by_strategy[sname] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "hold_times": [], "exit_reasons": {}}
        if e.get("result") == "WIN":
            by_strategy[sname]["wins"] += 1
        elif e.get("result") == "LOSS":
            by_strategy[sname]["losses"] += 1
        by_strategy[sname]["total_pnl"] += e.get("net_pnl_rupees") or 0
        if e.get("hold_minutes"):
            by_strategy[sname]["hold_times"].append(e["hold_minutes"])
        er = e.get("exit_reason") or "unknown"
        by_strategy[sname]["exit_reasons"][er] = by_strategy[sname]["exit_reasons"].get(er, 0) + 1
    summary = {}
    for sname, data in by_strategy.items():
        total = data["wins"] + data["losses"]
        summary[sname] = {
            "total_trades": total,
            "wins": data["wins"],
            "losses": data["losses"],
            "win_rate_pct": round(data["wins"] / total * 100, 1) if total > 0 else 0,
            "total_pnl_rupees": round(data["total_pnl"], 2),
            "avg_pnl_per_trade": round(data["total_pnl"] / total, 2) if total > 0 else 0,
            "avg_hold_minutes": round(statistics.mean(data["hold_times"]), 1) if data["hold_times"] else 0,
            "exit_reason_distribution": data["exit_reasons"],
        }
    all_pnls = [e.get("net_pnl_rupees") or 0 for e in prior_exits]
    wins_pnl = [p for p in all_pnls if p > 0]
    losses_pnl = [abs(p) for p in all_pnls if p < 0]
    overall = {
        "total_trades": len(prior_exits),
        "total_wins": sum(1 for e in prior_exits if e.get("result") == "WIN"),
        "total_losses": sum(1 for e in prior_exits if e.get("result") == "LOSS"),
        "total_pnl_rupees": round(sum(all_pnls), 2),
        "avg_win_rupees": round(statistics.mean(wins_pnl), 2) if wins_pnl else 0,
        "avg_loss_rupees": round(statistics.mean(losses_pnl), 2) if losses_pnl else 0,
        "profit_factor": round(sum(wins_pnl) / sum(losses_pnl), 3) if losses_pnl and sum(losses_pnl) > 0 else None,
        "win_rate_pct": round(sum(1 for e in prior_exits if e.get("result") == "WIN") / len(prior_exits) * 100, 1) if prior_exits else 0,
    }
    return {"by_strategy": summary, "overall_30d": overall}


def compute_exit_reason_analysis(trade_exits, trade_entries):
    if not trade_exits:
        return {}
    entries_by_pos = {t["position_id"]: t for t in trade_entries}
    by_reason = {}
    for e in trade_exits:
        reason = e.get("exit_reason") or "unknown"
        if reason not in by_reason:
            by_reason[reason] = {"count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "hold_times": [], "strategies": {}}
        by_reason[reason]["count"] += 1
        if e.get("result") == "WIN":
            by_reason[reason]["wins"] += 1
        elif e.get("result") == "LOSS":
            by_reason[reason]["losses"] += 1
        by_reason[reason]["total_pnl"] += e.get("net_pnl_rupees") or 0
        if e.get("hold_minutes"):
            by_reason[reason]["hold_times"].append(e["hold_minutes"])
        entry = entries_by_pos.get(e.get("position_id"), {})
        sname = entry.get("strategy_name") or e.get("strategy_name") or "unknown"
        by_reason[reason]["strategies"][sname] = by_reason[reason]["strategies"].get(sname, 0) + 1
    result = {}
    for reason, data in by_reason.items():
        result[reason] = {
            "count": data["count"],
            "wins": data["wins"],
            "losses": data["losses"],
            "total_pnl_rupees": round(data["total_pnl"], 2),
            "avg_pnl_rupees": round(data["total_pnl"] / data["count"], 2) if data["count"] > 0 else 0,
            "avg_hold_minutes": round(statistics.mean(data["hold_times"]), 1) if data["hold_times"] else 0,
            "strategies_involved": data["strategies"],
        }
    return result


def compute_signal_at_entry_analysis(trade_entries):
    if not trade_entries:
        return {}
    vrp_at_entry = [t["entry_vrp"] for t in trade_entries if t.get("entry_vrp") is not None]
    vix_at_entry = [t["entry_vix"] for t in trade_entries if t.get("entry_vix") is not None]
    adx_at_entry = [t["entry_adx"] for t in trade_entries if t.get("entry_adx") is not None]
    vol_conds = [t.get("volatility_condition") for t in trade_entries if t.get("volatility_condition")]
    trend_conds = [t.get("trend_condition") for t in trade_entries if t.get("trend_condition")]
    directions = [t.get("direction") for t in trade_entries if t.get("direction")]
    result = {}
    if vrp_at_entry:
        result["vrp_at_entry"] = {"values": vrp_at_entry, "mean": round(statistics.mean(vrp_at_entry), 3)}
    if vix_at_entry:
        result["vix_at_entry"] = {"values": vix_at_entry, "mean": round(statistics.mean(vix_at_entry), 3)}
    if adx_at_entry:
        result["adx_at_entry"] = {"values": adx_at_entry, "mean": round(statistics.mean(adx_at_entry), 3)}
    if vol_conds:
        result["volatility_condition_at_entry"] = {c: vol_conds.count(c) for c in set(vol_conds)}
    if trend_conds:
        result["trend_condition_at_entry"] = {c: trend_conds.count(c) for c in set(trend_conds)}
    if directions:
        result["direction_at_entry"] = {c: directions.count(c) for c in set(directions)}
    return result


def compute_pnl_curve(cycle_rows):
    curve = []
    for c in cycle_rows:
        if c.get("cycle_time") and c.get("daily_pnl_net") is not None:
            curve.append({"time": c["cycle_time"], "pnl": c["daily_pnl_net"], "spot": c.get("spot"), "vrp": c.get("vrp"), "adx": c.get("adx")})
    return curve


def compute_gate_blockage_analysis(decisions, cycle_rows):
    total_cycles = len(cycle_rows)
    total_decisions = len(decisions)
    no_trade_decisions = [d for d in decisions if d.get("action") == "NO_TRADE"]
    enter_decisions = [d for d in decisions if d.get("action") in ("ENTER", "STRATEGY_SELECTED")]
    reasons = aggregate_no_trade_reasons(decisions)
    gate_categories = {
        "risk_gates": ["daily_loss_limit", "max_entries", "max_concurrent", "consecutive_stops", "stop_cooldown", "projected_daily_loss"],
        "data_gates": ["vrp_unknown", "opening_range_not", "or_pending", "chain_unavailable", "vix_unknown"],
        "market_condition_gates": ["circuit_breaker", "vix_spike", "iv_expanding", "iv_spiking", "intraday_rv"],
        "timing_gates": ["before_entry_window", "past_entry_window", "entry_timing_wait", "only_", "0dte", "hard_exit"],
        "strategy_gates": ["no_conditions_met", "params_invalid", "strategy_rules_failed", "credit_", "net_credit", "dte="],
        "event_gates": ["event_day", "vix_suppressed", "very_wide_or"],
    }
    categorized = {cat: 0 for cat in gate_categories}
    categorized["other"] = 0
    for reason, count in reasons.items():
        matched = False
        for cat, keywords in gate_categories.items():
            if any(kw in reason for kw in keywords):
                categorized[cat] += count
                matched = True
                break
        if not matched:
            categorized["other"] += count
    return {
        "total_cycles": total_cycles,
        "total_decisions": total_decisions,
        "no_trade_count": len(no_trade_decisions),
        "enter_count": len(enter_decisions),
        "no_trade_rate_pct": round(len(no_trade_decisions) / total_decisions * 100, 1) if total_decisions > 0 else 0,
        "gate_category_counts": categorized,
        "top_10_reasons": dict(list(reasons.items())[:10]),
    }


def compute_nifty_intraday_benchmarks(cycle_rows, trade_exits):
    spots = [c["spot"] for c in cycle_rows if c.get("spot") is not None]
    if not spots:
        return {}
    day_range = max(spots) - min(spots)
    day_range_pct = day_range / spots[0] * 100 if spots[0] > 0 else 0
    vix_vals = [c["vix"] for c in cycle_rows if c.get("vix") is not None]
    avg_vix = statistics.mean(vix_vals) if vix_vals else None
    expected_daily_range_pct = (avg_vix / 100.0) / (252 ** 0.5) * 100 if avg_vix else None
    range_vs_expected = round(day_range_pct / expected_daily_range_pct, 2) if expected_daily_range_pct else None
    hold_times = [e.get("hold_minutes") for e in trade_exits if e.get("hold_minutes") is not None]
    return {
        "actual_day_range_pts": round(day_range, 2),
        "actual_day_range_pct": round(day_range_pct, 3),
        "expected_daily_range_pct_from_vix": round(expected_daily_range_pct, 3) if expected_daily_range_pct else None,
        "range_vs_expected_ratio": range_vs_expected,
        "day_classification": "HIGH_MOVE" if range_vs_expected and range_vs_expected > 1.5 else ("NORMAL_MOVE" if range_vs_expected and range_vs_expected > 0.7 else "LOW_MOVE"),
        "avg_hold_minutes_today": round(statistics.mean(hold_times), 1) if hold_times else None,
        "nifty_intraday_note": "NIFTY 50 weekly expiry is Tuesday. 0DTE gamma risk highest on Tuesday afternoon. Normal daily range for NIFTY in 2026 is 0.5-1.2% based on VIX 12-18 regime.",
    }


def detect_anomalies(session_state, cycle_rows, api_summary, gaps, daily_summary, decisions, trade_exits, vrp_stats, spot_profile):
    flags = []
    if session_state:
        if session_state.get("daily_halted"):
            flags.append(f"[FLAG] Daily trading halted. last_stop_reason={session_state.get('last_stop_reason')}")
        if session_state.get("circuit_breaker_suspected"):
            flags.append("[FLAG] Circuit breaker was suspected at some point today.")
        if session_state.get("vix_spike_detected"):
            flags.append("[FLAG] VIX spike was detected at some point today.")
        if not session_state.get("or_computed"):
            flags.append("[FLAG] Opening range was NEVER computed today.")
        if not session_state.get("session_initialized"):
            flags.append("[FLAG] Session was never initialized — opening IV baseline missing.")
    if cycle_rows:
        missing_vrp = sum(1 for c in cycle_rows if c.get("vrp") is None)
        if missing_vrp > len(cycle_rows) * 0.3:
            flags.append(f"[FLAG] {missing_vrp}/{len(cycle_rows)} cycles had missing VRP data.")
        missing_spot = sum(1 for c in cycle_rows if c.get("spot") is None)
        if missing_spot > 0:
            flags.append(f"[FLAG] {missing_spot} cycles had missing spot price.")
        unknown_vol = sum(1 for c in cycle_rows if c.get("volatility_condition") == "UNKNOWN")
        if unknown_vol > 3:
            flags.append(f"[FLAG] {unknown_vol} cycles had volatility_condition=UNKNOWN.")
        missing_pcr = sum(1 for c in cycle_rows if c.get("pcr") is None)
        if missing_pcr > len(cycle_rows) * 0.5:
            flags.append(f"[FLAG] {missing_pcr}/{len(cycle_rows)} cycles had missing PCR — directional bias unreliable.")
        missing_skew = sum(1 for c in cycle_rows if c.get("skew_ratio") is None)
        if missing_skew > len(cycle_rows) * 0.5:
            flags.append(f"[FLAG] {missing_skew}/{len(cycle_rows)} cycles had missing skew ratio.")
    if api_summary.get("error_count", 0) > 5:
        flags.append(f"[FLAG] {api_summary['error_count']} API errors occurred today.")
    if api_summary.get("rate_limited_count", 0) > 0:
        flags.append(f"[FLAG] {api_summary['rate_limited_count']} API calls were rate-limited.")
    if api_summary.get("slow_calls_over_2s", 0) > 3:
        flags.append(f"[FLAG] {api_summary['slow_calls_over_2s']} API calls took over 2 seconds — may cause cycle delays.")
    if gaps:
        largest = max(g[2] for g in gaps)
        flags.append(f"[FLAG] {len(gaps)} timing gap(s) in cycle_log (largest {largest:.0f} min) — possible downtime/restart.")
    if daily_summary and (daily_summary.get("stops_fired") or 0) >= 2:
        flags.append(f"[FLAG] {daily_summary['stops_fired']} stop-losses fired today.")
    if vrp_stats.get("vrp_negative_cycles", 0) > 0 and vrp_stats.get("total_vrp_cycles", 0) > 0:
        neg_pct = vrp_stats["vrp_negative_cycles"] / vrp_stats["total_vrp_cycles"] * 100
        if neg_pct > 30:
            flags.append(f"[FLAG] VRP was negative in {neg_pct:.0f}% of cycles — IV was BELOW realized vol, premium selling had no edge.")
    if spot_profile.get("range_pct", 0) > 1.5:
        flags.append(f"[FLAG] NIFTY moved {spot_profile.get('range_pct', 0):.2f}% intraday — high-move day, condor/butterfly risk elevated.")
    if spot_profile.get("range_pct", 0) < 0.3:
        flags.append(f"[FLAG] NIFTY moved only {spot_profile.get('range_pct', 0):.2f}% intraday — very low move day, premium may have been thin.")
    no_trade_reasons = aggregate_no_trade_reasons(decisions)
    if no_trade_reasons:
        top_reason, top_count = next(iter(no_trade_reasons.items()))
        total_decisions = len(decisions)
        if total_decisions and top_count > total_decisions * 0.5:
            flags.append(f"[FLAG] Single no-trade reason dominates: '{top_reason}' fired {top_count}/{total_decisions} times ({top_count/total_decisions*100:.0f}%) — gate may be miscalibrated.")
    stop_exits = [e for e in trade_exits if e.get("exit_reason") == "CLOSE_STOP"]
    if len(stop_exits) >= 2:
        flags.append(f"[FLAG] {len(stop_exits)} positions hit stop-loss today — review stop multiplier and entry conditions.")
    if not flags:
        flags.append("[OK] No major anomalies auto-detected.")
    return flags


def build_master_timeline(cycle_rows, decisions, trade_entries, trade_exits, audit_lines):
    events = []
    for c in cycle_rows:
        events.append((c.get("cycle_time") or "", "CYCLE", f"spot={c.get('spot')} vix={c.get('vix')} vrp={c.get('vrp')} vol={c.get('volatility_condition')} trend={c.get('trend_condition')} dir={c.get('direction')} adx={c.get('adx')} pcr={c.get('pcr')} skew={c.get('skew_ratio')} action={c.get('action_taken')} no_trade_reason={c.get('no_trade_reason')} open_pos={c.get('open_positions')} daily_pnl={c.get('daily_pnl_net')}"))
    for d in decisions:
        events.append((d.get("decision_time") or "", "DECISION", f"{d.get('action')} {d.get('strategy_name') or ''} — {d.get('reason')}"))
    for t in trade_entries:
        events.append((t.get("entry_time") or "", "ENTRY", f"{t.get('strategy_name')} lots={t.get('final_lots')} credit/debit={t.get('entry_credit') or t.get('entry_debit')} vrp={t.get('entry_vrp')} vix={t.get('entry_vix')} vol_cond={t.get('volatility_condition')} trend={t.get('trend_condition')} dir={t.get('direction')} reason={t.get('selection_reason')}"))
    for e in trade_exits:
        events.append((e.get("exit_time") or "", "EXIT", f"{e.get('strategy_name')} reason={e.get('exit_reason')} hold={e.get('hold_minutes')}min net_pnl={e.get('net_pnl_rupees')} gross_pnl={e.get('gross_pnl_rupees')} costs={e.get('total_costs_rupees')} result={e.get('result')}"))
    for line in filter_log_lines_by_level(audit_lines, {"WARNING", "ERROR", "CRITICAL"}):
        parts = line.split("|")
        ts = parts[0].strip() if parts else ""
        msg = "|".join(parts[2:]).strip() if len(parts) > 2 else line
        level = parts[1].strip() if len(parts) > 1 else "LOG"
        events.append((ts, f"LOG:{level}", msg))
    events.sort(key=lambda x: x[0])
    return events


def md_kv(d):
    if not d:
        return "_(none)_\n"
    lines = []
    for k, v in d.items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines) + "\n"


def md_table(rows, columns, max_rows=80):
    if not rows:
        return "_(no data)_\n"
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    display_rows = rows[:max_rows] if max_rows else rows
    for r in display_rows:
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
    if max_rows and len(rows) > max_rows:
        out += f"\n_... {len(rows) - max_rows} more row(s) omitted — see raw JSON export._\n"
    return out


def generate_report(target_date):
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"TARGET_DATE '{target_date}' is not in YYYY-MM-DD format.")

    conn = get_connection(DB_PATH)

    session_state = fetch_session_state(conn, target_date)
    cycle_rows = fetch_cycle_log(conn, target_date)
    decisions = fetch_strategy_decisions(conn, target_date)
    positions = fetch_positions(conn, target_date)
    position_ids = [p["position_id"] for p in positions]
    legs = fetch_position_legs(conn, position_ids)
    trade_entries = fetch_trade_entries(conn, target_date)
    trade_exits = fetch_trade_exits(conn, target_date)
    daily_summary = fetch_daily_summary(conn, target_date)
    chain_rows = fetch_option_chain_snapshot(conn, target_date)
    api_rows = fetch_api_call_log(conn, target_date)
    audit_db_rows = fetch_audit_log_db(conn, target_date)
    audit_file_lines = fetch_audit_log_file_lines(LOG_DIR, target_date)
    prior_days_summary = fetch_prior_days_summary(conn, target_date, n=10)
    prior_days_cycle_stats = fetch_prior_days_cycle_stats(conn, target_date, n=5)
    prior_exits = fetch_all_exits_for_strategy_analysis(conn, target_date, lookback_days=30)
    chain_atm_history = fetch_chain_atm_history(conn, target_date)
    chain_wing_history = fetch_chain_wing_history(conn, target_date)

    conn.close()

    gaps = detect_cycle_gaps(cycle_rows)
    no_trade_reasons = aggregate_no_trade_reasons(decisions)
    strategies_used = aggregate_strategies(trade_entries)
    cost_analysis = analyze_trade_costs(trade_entries, trade_exits)
    chain_summary = summarize_option_chain(chain_rows)
    api_summary = summarize_api_calls(api_rows)
    vrp_stats = compute_vrp_statistics(cycle_rows)
    spot_profile = compute_intraday_spot_profile(cycle_rows)
    adx_profile = compute_adx_profile(cycle_rows)
    pcr_profile = compute_pcr_profile(cycle_rows)
    skew_profile = compute_skew_profile(cycle_rows)
    historical_perf = compute_historical_performance(prior_exits)
    exit_reason_analysis = compute_exit_reason_analysis(trade_exits, trade_entries)
    signal_at_entry = compute_signal_at_entry_analysis(trade_entries)
    pnl_curve = compute_pnl_curve(cycle_rows)
    gate_analysis = compute_gate_blockage_analysis(decisions, cycle_rows)
    nifty_benchmarks = compute_nifty_intraday_benchmarks(cycle_rows, trade_exits)
    anomalies = detect_anomalies(session_state, cycle_rows, api_summary, gaps, daily_summary, decisions, trade_exits, vrp_stats, spot_profile)
    timeline = build_master_timeline(cycle_rows, decisions, trade_entries, trade_exits, audit_file_lines)
    warning_error_lines = filter_log_lines_by_level(audit_file_lines, {"WARNING", "ERROR", "CRITICAL"})

    legs_by_position = {}
    for leg in legs:
        legs_by_position.setdefault(leg["position_id"], []).append(leg)
    exits_by_position = {e["position_id"]: e for e in trade_exits}

    md = []
    md.append(f"# NIFTY Intraday Options Engine — EOD Forensic Report")
    md.append(f"**Target Date:** {target_date}  |  **Report Generated:** {datetime.now().isoformat()}\n")

    md.append("## 0. How to Read This Report\n")
    md.append("This report describes one trading day of a NIFTY-50 intraday options-selling/buying algo engine. The engine runs 5-minute cycles from 10:00-15:00 IST. It sells premium (Iron Condor, Iron Butterfly, Bull Put Spread, Bear Call Spread) when VRP is positive and buys premium (Bull/Bear debit spreads, Long Straddle) when IV is cheap. All positions are intraday only — no overnight holding. NIFTY weekly expiry is Tuesday. The engine uses VRP x Trend x Direction decision matrix. Key signals: VRP (ATM IV minus Parkinson RV), ADX (trend strength), VWAP distance, PCR, 25-delta skew. Exit triggers: premium stop (2x credit), price stop (spot moves), profit target (30-35% of credit), VWAP breach, ADX breach (>30), delta breach (short leg delta >0.40), hard exit at 15:00.\n")

    md.append("## 1. Table of Contents\n")
    md.append("2. Executive Summary\n3. Auto-Detected Anomalies\n4. Session Configuration\n5. NIFTY Intraday Profile\n6. VRP / Volatility Deep Dive\n7. ADX / Trend Profile\n8. PCR / Skew / Directional Profile\n9. Market Data Timeline (cycle_log)\n10. Gate Blockage Analysis\n11. Strategy Decision Log\n12. No-Trade Reason Frequency\n13. Trade-by-Trade Deep Dive\n14. Exit Reason Analysis\n15. Signal Conditions at Entry\n16. Position and Leg Raw Detail\n17. P&L Curve (intraday)\n18. Option Chain Statistics\n19. ATM IV Intraday History\n20. API Call Health\n21. Data Quality Checks\n22. Historical Performance (30-day lookback)\n23. Prior Days Comparison\n24. NIFTY Intraday Benchmarks\n25. Audit Log Warnings and Errors\n26. Unified Master Timeline\n27. Daily Summary\n28. LLM Optimization Notes\n29. Raw Data Export Manifest\n")

    md.append("## 2. Executive Summary\n")
    net_pnl = round(sum(e.get("net_pnl_rupees") or 0 for e in trade_exits), 2)
    gross_pnl = round(sum(e.get("gross_pnl_rupees") or 0 for e in trade_exits), 2)
    total_costs = round(sum(e.get("total_costs_rupees") or 0 for e in trade_exits), 2)
    exec_summary = {
        "Day label": session_state.get("day_label") if session_state else "N/A",
        "Day mode": session_state.get("day_mode") if session_state else "N/A",
        "VIX regime (final)": session_state.get("vix_regime") if session_state else "N/A",
        "OR condition / width": f"{session_state.get('or_condition')} / {session_state.get('or_width')}" if session_state else "N/A",
        "Trades attempted (decisions)": len(decisions),
        "Trades executed": len(trade_entries),
        "Trades closed": len(trade_exits),
        "Wins / Losses": f"{sum(1 for e in trade_exits if e.get('result')=='WIN')} / {sum(1 for e in trade_exits if e.get('result')=='LOSS')}",
        "Gross P&L (Rs)": gross_pnl,
        "Total Costs (Rs)": total_costs,
        "Net P&L (Rs, from trade_exits)": net_pnl,
        "Net P&L as pct of capital": f"{round(net_pnl / STARTING_CAPITAL * 100, 3)}%" if STARTING_CAPITAL else "N/A",
        "Realized daily_pnl (session_state)": session_state.get("daily_pnl") if session_state else "N/A",
        "Current capital (session_state)": session_state.get("current_capital") if session_state else "N/A",
        "Daily halted": session_state.get("daily_halted") if session_state else "N/A",
        "Consecutive stops": session_state.get("consecutive_stops") if session_state else "N/A",
        "Cycles logged": len(cycle_rows),
        "NIFTY open / close / range": f"{spot_profile.get('open')} / {spot_profile.get('close')} / {spot_profile.get('range_pts')}pts ({spot_profile.get('range_pct')}%)" if spot_profile else "N/A",
        "VRP mean today (pp)": vrp_stats.get("vrp_mean"),
        "ATM IV open / close (pct)": f"{vrp_stats.get('atm_iv_open_pct')} / {vrp_stats.get('atm_iv_close_pct')}",
        "IV crush today (pp)": vrp_stats.get("iv_crush_pct"),
        "Option chain snapshot rows": len(chain_rows),
        "API calls made": len(api_rows),
        "Audit log lines (file)": len(audit_file_lines),
    }
    md.append(md_kv(exec_summary))

    md.append("## 3. Auto-Detected Anomalies / Flags\n")
    md.append("\n".join(f"- {f}" for f in anomalies) + "\n")

    md.append("## 4. Session Configuration Snapshot\n")
    md.append(md_kv(session_state) if session_state else "_No session_state row found for this date._\n")

    md.append("## 5. NIFTY Intraday Profile\n")
    md.append(md_kv(spot_profile))
    md.append(md_kv(nifty_benchmarks))

    md.append("## 6. VRP / Volatility Deep Dive\n")
    md.append(md_kv(vrp_stats))
    md.append("\n**VRP interpretation for NIFTY options:** VRP > 3pp = RICH (sell premium). VRP 1.5-3pp = FAIR (reduced size). VRP < 0 = CHEAP (buy premium). NIFTY ATM IV in 2026 typically ranges 12-22% depending on VIX regime. IV crush on event days can be 15-30%. Normal daily IV change is -3 to +5%.\n")

    md.append("## 7. ADX / Trend Profile\n")
    md.append(md_kv(adx_profile))
    md.append("\n**ADX interpretation for NIFTY intraday:** ADX < 20 = flat/range (ideal for condors). ADX 20-25 = weak trend (spreads ok). ADX 25-32 = moderate trend (directional spreads only). ADX > 32 = strong trend (exit condors, avoid new sells). ADX > 40 = very strong trend (no premium selling at all).\n")

    md.append("## 8. PCR / Skew / Directional Profile\n")
    md.append(md_kv(pcr_profile))
    md.append(md_kv(skew_profile))
    md.append("\n**PCR interpretation for NIFTY:** PCR > 1.5 = extreme fear (contrarian bullish). PCR 1.0-1.5 = fear/put heavy. PCR 0.8-1.0 = neutral. PCR < 0.7 = greed/call heavy (contrarian bearish). **Skew interpretation:** Skew > 1.25 = put IV premium (fear), sell calls. Skew 0.95-1.10 = balanced. Skew < 0.95 = call IV premium (complacency), sell puts.\n")

    md.append("## 9. Market Data Timeline (cycle_log)\n")
    md.append(f"Total cycles: {len(cycle_rows)}. Gaps detected: {len(gaps)}.\n")
    if gaps:
        md.append("**Gaps:**\n")
        for g in gaps:
            md.append(f"- {g[0]} -> {g[1]} ({g[2]:.1f} min)\n")
    md.append(md_table(cycle_rows, ["cycle_time", "spot", "vix", "vrp", "atm_iv_pct", "parkinson_rv_pct", "adx", "adx_condition", "vwap_dist_pct", "pcr", "skew_ratio", "or_condition", "volatility_condition", "trend_condition", "direction", "action_taken", "no_trade_reason", "open_positions", "daily_pnl_net"], max_rows=100))

    md.append("## 10. Gate Blockage Analysis\n")
    md.append(md_kv(gate_analysis))
    md.append("\n**Gate analysis interpretation:** If risk_gates dominate, the engine is being too conservative on capital/loss limits. If timing_gates dominate, the trading window may be too narrow. If strategy_gates dominate, credit floors or ratio checks are blocking trades — review MIN_CREDITS and credit/width ratio thresholds. If data_gates dominate, there are API or data quality issues.\n")

    md.append("## 11. Strategy Decision Log\n")
    md.append(md_table(decisions, ["decision_time", "action", "strategy_name", "reason"], max_rows=100))
    selected = [d for d in decisions if d.get("action") in ("STRATEGY_SELECTED", "ENTER")]
    if selected:
        md.append("\n### 11a. Full Parameters for Selected Strategies\n")
        for d in selected:
            md.append(f"\n**{d.get('decision_time')} — {d.get('strategy_name')}**\n")
            try:
                params = json.loads(d["params_json"]) if d.get("params_json") else {}
            except Exception:
                params = {}
            if params:
                md.append("```json\n" + json.dumps(params, indent=2, default=str) + "\n```\n")

    md.append("## 12. No-Trade Reason Frequency\n")
    if no_trade_reasons:
        for reason, count in no_trade_reasons.items():
            md.append(f"- [{count}x] {reason}\n")
    else:
        md.append("_No NO_TRADE decisions recorded._\n")

    md.append("## 13. Trade-by-Trade Deep Dive\n")
    if not trade_entries:
        md.append("_No trades were entered on this date._\n")
    for t in trade_entries:
        md.append(f"\n### Position `{t['position_id']}` — {t['strategy_name']}\n")
        md.append(md_kv({
            "Entry time": t.get("entry_time"),
            "Day label": t.get("day_label"),
            "Selection reason": t.get("selection_reason"),
            "Entry spot / VIX / VRP": f"{t.get('entry_spot')} / {t.get('entry_vix')} / {t.get('entry_vrp')}",
            "ATM IV at entry": t.get("entry_atm_iv"),
            "Parkinson RV at entry": t.get("entry_parkinson_rv"),
            "ADX at entry": t.get("entry_adx"),
            "VWAP dist at entry": t.get("entry_vwap_dist"),
            "PCR at entry": t.get("entry_pcr"),
            "PCR change at entry": t.get("entry_pcr_change"),
            "Skew at entry": t.get("entry_skew_ratio"),
            "Volatility condition": t.get("volatility_condition"),
            "IV behavior": t.get("iv_behavior"),
            "Trend condition": t.get("trend_condition"),
            "ADX condition": t.get("adx_condition"),
            "Direction": t.get("direction"),
            "VWAP signal": t.get("vwap_signal"),
            "PCR signal": t.get("pcr_signal"),
            "Skew signal": t.get("skew_signal"),
            "Preferred sell side": t.get("preferred_sell_side"),
            "OR condition / width": f"{t.get('or_condition')} / {t.get('or_width')}",
            "Target expiry / DTE": f"{t.get('target_expiry')} / {t.get('actual_dte')}",
            "Entry credit/debit (pts)": t.get("entry_credit") or t.get("entry_debit"),
            "Gross credit (pts)": t.get("gross_credit"),
            "Slippage (pts)": t.get("total_slippage"),
            "Entry costs (Rs)": t.get("entry_costs_rupees"),
            "Stop premium (pts)": t.get("stop_premium"),
            "Target premium (pts)": t.get("target_premium"),
            "Price stop (pts)": t.get("price_stop_pts"),
            "Final lots": t.get("final_lots"),
            "Max loss/lot (Rs)": t.get("max_loss_per_lot"),
            "Total max risk (Rs)": t.get("total_max_risk"),
            "Capital at entry": t.get("capital_at_entry"),
            "Daily P&L at entry": t.get("daily_pnl_at_entry"),
            "Paper trade": t.get("paper_trade"),
        }))
        try:
            leg_list = json.loads(t["legs_json"]) if t.get("legs_json") else []
        except Exception:
            leg_list = []
        if leg_list:
            md.append("\n**Legs at entry:**\n")
            md.append(md_table(leg_list, ["action", "option_type", "strike", "exec_price", "delta", "gamma", "vega", "theta", "iv", "oi"]))
        exit_row = exits_by_position.get(t["position_id"])
        if exit_row:
            md.append("\n**Exit:**\n")
            md.append(md_kv({
                "Exit time": exit_row.get("exit_time"),
                "Exit reason": exit_row.get("exit_reason"),
                "Hold time (min)": exit_row.get("hold_minutes"),
                "Exit premium (pts)": exit_row.get("exit_premium"),
                "Exit spot": exit_row.get("exit_spot"),
                "Exit VIX": exit_row.get("exit_vix"),
                "Exit ADX": exit_row.get("exit_adx"),
                "Exit VWAP dist": exit_row.get("exit_vwap_dist"),
                "Gross P&L (pts / Rs)": f"{exit_row.get('gross_pnl_pts')} / {exit_row.get('gross_pnl_rupees')}",
                "Exit costs (Rs)": exit_row.get("exit_costs_rupees"),
                "Total costs (Rs)": exit_row.get("total_costs_rupees"),
                "Net P&L (pts / Rs / %)": f"{exit_row.get('net_pnl_pts')} / {exit_row.get('net_pnl_rupees')} / {exit_row.get('net_pnl_pct')}",
                "Result": exit_row.get("result"),
                "Profit pct of credit": exit_row.get("profit_pct_of_credit"),
            }))
        else:
            md.append(f"\n**Exit:** _No matching exit row — position may still be open or exit failed to record. Check positions.status for {t['position_id']}._\n")
        cost_row = next((c for c in cost_analysis if c["position_id"] == t["position_id"]), None)
        if cost_row:
            md.append("\n**Recomputed cost breakdown (independent cross-check):**\n")
            md.append(md_kv(cost_row["recomputed_entry_costs"]))
            md.append(f"- Recorded entry costs (Rs): {cost_row['recorded_entry_costs_rupees']}\n")
            md.append(f"- Cost discrepancy (Rs): {cost_row['cost_discrepancy_rupees']}\n")

    md.append("## 14. Exit Reason Analysis\n")
    if exit_reason_analysis:
        for reason, data in exit_reason_analysis.items():
            md.append(f"\n### Exit: {reason}\n")
            md.append(md_kv(data))
    else:
        md.append("_No exits recorded today._\n")
    md.append("\n**Exit reason benchmarks for NIFTY intraday options:** CLOSE_TARGET (profit at 30-35% of credit) = ideal outcome. CLOSE_STOP (premium doubled) = loss, review entry conditions. CLOSE_TIME (hard exit 15:00) = neutral, time decay captured. CLOSE_VWAP = directional risk management. CLOSE_ADX = trend risk management. CLOSE_DELTA = gamma risk management. EOD_CLOSE = position held too long, review entry timing.\n")

    md.append("## 15. Signal Conditions at Entry\n")
    md.append(md_kv(signal_at_entry))
    md.append("\n**Optimal entry conditions for NIFTY intraday premium selling:** VRP > 3pp, VIX 13-20, ADX < 25, OR condition NARROW or VERY_NARROW, volatility_condition RICH or VERY_RICH, iv_behavior STABLE or DECLINING, direction NEUTRAL or aligned with spread side, time 10:30-13:00 IST.\n")

    md.append("## 16. Position and Leg Raw Detail\n")
    md.append(md_table(positions, ["position_id", "strategy_name", "entry_time", "final_lots", "entry_credit", "entry_debit", "status", "exit_time", "exit_reason", "net_pnl_rupees", "paper_trade"]))
    md.append("\n### All Legs\n")
    md.append(md_table(legs, ["position_id", "strike", "option_type", "action", "qty", "entry_price", "exit_price", "entry_delta", "entry_gamma", "entry_vega", "entry_iv", "leg_status"]))

    md.append("## 17. P&L Curve (intraday)\n")
    if pnl_curve:
        md.append(md_table(pnl_curve, ["time", "pnl", "spot", "vrp", "adx"], max_rows=100))
    else:
        md.append("_No P&L curve data available._\n")

    md.append("## 18. Option Chain Statistics\n")
    md.append(md_kv(chain_summary))
    md.append(f"\n_Full chain ({len(chain_rows)} rows) in raw JSON export._\n")

    md.append("## 19. ATM IV Intraday History\n")
    if chain_atm_history:
        md.append(md_table(chain_atm_history, ["capture_time", "strike", "option_type", "bid", "ask", "ltp", "iv", "delta", "oi"], max_rows=60))
        md.append("\n### Wing IV History (25-delta strikes)\n")
        md.append(md_table(chain_wing_history, ["capture_time", "strike", "option_type", "bid", "ask", "ltp", "iv", "delta", "oi"], max_rows=60))
    else:
        md.append("_No ATM chain history available._\n")

    md.append("## 20. API Call Health\n")
    md.append(md_kv({k: v for k, v in api_summary.items() if k != "errors_sample"}))
    if api_summary.get("errors_sample"):
        md.append("\n**Sample API errors:**\n")
        md.append(md_table(api_summary["errors_sample"], ["call_time", "category", "endpoint", "status_code", "error_message"]))

    md.append("## 21. Data Quality Checks\n")
    dq = {
        "Cycles with missing spot": sum(1 for c in cycle_rows if c.get("spot") is None),
        "Cycles with missing VIX": sum(1 for c in cycle_rows if c.get("vix") is None),
        "Cycles with missing VRP": sum(1 for c in cycle_rows if c.get("vrp") is None),
        "Cycles with missing PCR": sum(1 for c in cycle_rows if c.get("pcr") is None),
        "Cycles with missing skew": sum(1 for c in cycle_rows if c.get("skew_ratio") is None),
        "Cycles with missing VWAP dist": sum(1 for c in cycle_rows if c.get("vwap_dist_pct") is None),
        "Cycles with volatility_condition=UNKNOWN": sum(1 for c in cycle_rows if c.get("volatility_condition") == "UNKNOWN"),
        "Cycles with trend_condition=OR_PENDING": sum(1 for c in cycle_rows if c.get("trend_condition") == "OR_PENDING"),
        "Cycles with adx_condition=INSUFFICIENT_DATA": sum(1 for c in cycle_rows if c.get("adx_condition") == "INSUFFICIENT_DATA"),
        "Option chain rows with zero bid/ask": chain_summary.get("zero_bid_ask_count", 0),
        "Timing gaps (>10min) in cycle_log": len(gaps),
        "Trades with no matching exit row": sum(1 for t in trade_entries if t["position_id"] not in exits_by_position),
        "Positions still OPEN at report time": sum(1 for p in positions if p.get("status") == "OPEN"),
        "Cost discrepancies detected": sum(1 for c in cost_analysis if c.get("cost_discrepancy_rupees") and abs(c["cost_discrepancy_rupees"]) > 10),
    }
    md.append(md_kv(dq))

    md.append("## 22. Historical Performance (30-day lookback)\n")
    if historical_perf:
        md.append("\n### Overall 30-day Stats\n")
        md.append(md_kv(historical_perf.get("overall_30d", {})))
        md.append("\n### By Strategy (30-day)\n")
        for sname, sdata in historical_perf.get("by_strategy", {}).items():
            md.append(f"\n**{sname}:**\n")
            md.append(md_kv(sdata))
    else:
        md.append("_No historical exit data available in 30-day lookback window._\n")

    md.append("## 23. Prior Days Comparison\n")
    if prior_days_summary:
        md.append(md_table(prior_days_summary, ["trading_date", "day_label", "trades_executed", "win_rate_pct", "net_pnl_rupees", "net_pnl_pct_capital", "vrp_mean", "or_condition", "stops_fired", "profit_factor", "capital_end"], max_rows=10))
    else:
        md.append("_No prior days summary data available._\n")
    if prior_days_cycle_stats:
        md.append("\n### Prior Days Signal Averages\n")
        md.append(md_table(prior_days_cycle_stats, ["trading_date", "avg_vrp", "avg_adx", "avg_vix", "avg_spot", "cycle_count"]))

    md.append("## 24. NIFTY Intraday Benchmarks\n")
    md.append(md_kv(nifty_benchmarks))

    md.append("## 25. Audit Log — Warnings and Errors\n")
    md.append(f"Total WARNING/ERROR/CRITICAL lines: {len(warning_error_lines)} of {len(audit_file_lines)} total ({len(audit_db_rows)} in DB table)\n\n")
    if warning_error_lines:
        md.append("```\n" + "\n".join(warning_error_lines[:200]) + "\n```\n")
        if len(warning_error_lines) > 200:
            md.append(f"_... {len(warning_error_lines) - 200} more lines in raw export._\n")
    else:
        md.append("_No warnings or errors logged today._\n")

    md.append("## 26. Unified Master Timeline\n")
    md.append(md_table([{"time": e[0], "type": e[1], "detail": e[2]} for e in timeline], ["time", "type", "detail"], max_rows=200))

    md.append("## 27. Daily Summary (engine EOD)\n")
    md.append(md_kv(daily_summary) if daily_summary else "_No daily_summary row found._\n")

    md.append("## 28. LLM Optimization Notes\n")
    md.append("This section summarizes what an LLM should focus on when analyzing this report to improve the engine:\n")
    md.append("1. **Gate calibration**: Check section 10 (Gate Blockage Analysis). If timing_gates or strategy_gates block >60% of decisions, thresholds need loosening. If risk_gates dominate, capital allocation or stop multiplier needs review.\n")
    md.append("2. **VRP quality**: Check section 6. If vrp_negative_cycles > 30% of total, the Parkinson RV computation may be overstating realized vol, or the day had genuine IV compression. Compare atm_iv_open vs atm_iv_close.\n")
    md.append("3. **Entry timing**: Check section 15. Optimal NIFTY intraday entry is 10:30-12:30 IST. Entries after 13:00 have reduced time for theta decay and higher gamma risk near expiry.\n")
    md.append("4. **Exit quality**: Check section 14. CLOSE_STOP exits indicate the stop multiplier (currently 2x credit) may be too tight for the day\'s volatility. CLOSE_TIME exits indicate positions were held too long without hitting target.\n")
    md.append("5. **ADX threshold**: Check section 7. If strong_trend_cycles > 30% of total cycles, the ADX exit threshold (currently 30) may need raising to 35 to avoid premature exits on choppy days.\n")
    md.append("6. **Credit floors**: Check section 12 (no-trade reasons). If net_credit_below_minimum appears frequently, MIN_CREDITS may be too high for the current VIX regime. In VIX 12-15 regime, NIFTY weekly ATM credit is typically 25-40pts for a 150pt wing condor.\n")
    md.append("7. **Cost drag**: Check section 13 cost discrepancy. Total round-trip costs for a 1-lot condor (4 legs) at current brokerage should be approximately Rs 200-350. If recorded costs differ by >10%, the cost computation has a bug.\n")
    md.append("8. **Chain data quality**: Check section 18. zero_bid_ask_pct > 20% indicates stale or missing chain data. avg_spread > 5pts for ATM strikes indicates poor liquidity or data quality issues.\n")
    md.append("9. **PCR and skew reliability**: Check section 8. If >50% of cycles have missing PCR or skew, the directional bias module is operating blind and direction=NEUTRAL will dominate, leading to more condor selections regardless of actual market direction.\n")
    md.append("10. **Capital carry-forward**: Check section 4 (session_state). current_capital should reflect yesterday\'s ending capital, not the flat starting_capital default. If they match exactly, the carry-forward logic may not have run.\n")

    md.append("## 29. Raw Data Export Manifest\n")
    md.append(f"All tables exported to: `eod_report_{target_date}_raw/`\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"eod_report_{target_date}.md"
    report_path.write_text("\n".join(md), encoding="utf-8")

    raw_dir = OUTPUT_DIR / f"eod_report_{target_date}_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_exports = {
        "session_state": session_state,
        "cycle_log": cycle_rows,
        "strategy_decisions": decisions,
        "positions": positions,
        "position_legs": legs,
        "trade_entries": trade_entries,
        "trade_exits": trade_exits,
        "daily_summary": daily_summary,
        "option_chain_snapshot": chain_rows,
        "api_call_log": api_rows,
        "audit_log_db": audit_db_rows,
        "audit_log_file_lines": audit_file_lines,
        "cost_analysis": cost_analysis,
        "master_timeline": timeline,
        "anomaly_flags": anomalies,
        "no_trade_reason_counts": no_trade_reasons,
        "strategies_used_counts": strategies_used,
        "chain_summary": chain_summary,
        "vrp_statistics": vrp_stats,
        "spot_profile": spot_profile,
        "adx_profile": adx_profile,
        "pcr_profile": pcr_profile,
        "skew_profile": skew_profile,
        "gate_blockage_analysis": gate_analysis,
        "exit_reason_analysis": exit_reason_analysis,
        "signal_at_entry_analysis": signal_at_entry,
        "pnl_curve": pnl_curve,
        "historical_performance_30d": historical_perf,
        "prior_days_summary": prior_days_summary,
        "prior_days_cycle_stats": prior_days_cycle_stats,
        "nifty_benchmarks": nifty_benchmarks,
        "chain_atm_history": chain_atm_history,
        "chain_wing_history": chain_wing_history,
        "api_summary": {k: v for k, v in api_summary.items() if k != "errors_sample"},
    }
    for name, data in raw_exports.items():
        (raw_dir / f"{name}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    print(f"Report written to: {report_path}")
    print(f"Raw data exported to: {raw_dir}/")
    print(f"\nQuick summary for {target_date}:")
    for k, v in exec_summary.items():
        print(f"  {k}: {v}")
    print("\nAnomaly flags:")
    for f in anomalies:
        print(f"  {f}")


if __name__ == "__main__":
    generate_report(TARGET_DATE)
