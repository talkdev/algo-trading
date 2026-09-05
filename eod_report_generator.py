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

LOT_SIZE             = int(_ENV.get("NIFTY_LOT_SIZE", "65") or 65)
STT_RATE             = float(_ENV.get("STT_RATE", "0.0015") or 0.0015)
EXCHANGE_TXN_RATE    = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003552") or 0.0003552)
BROKERAGE_PER_ORDER  = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)
STARTING_CAPITAL     = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1000000)
MAX_DAILY_LOSS_PCT   = float(_ENV.get("MAX_DAILY_LOSS_PCT", "0.02") or 0.02)


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


def column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


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
    return q(
        conn,
        f"SELECT * FROM position_legs WHERE position_id IN ({placeholders}) ORDER BY position_id, leg_id",
        tuple(position_ids),
    )


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


def fetch_option_chain_snapshot(conn, d):
    if not table_exists(conn, "option_chain_snapshot"):
        return []
    return q(
        conn,
        "SELECT * FROM option_chain_snapshot WHERE trading_date=? ORDER BY capture_time, strike, option_type",
        (d,),
    )


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
    return q(
        conn,
        "SELECT * FROM daily_summary WHERE trading_date < ? ORDER BY trading_date DESC LIMIT ?",
        (d, n),
    )


def fetch_intraday_candles(conn, d):
    if not table_exists(conn, "intraday_candles"):
        return []
    return q(
        conn,
        "SELECT candle_time, open, high, low, close, volume FROM intraday_candles WHERE trading_date=? AND interval_min=1 ORDER BY candle_time",
        (d,),
    )


def fetch_cumulative_performance(conn, d, lookback_days=90):
    if not table_exists(conn, "daily_summary"):
        return []
    cutoff = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return q(
        conn,
        "SELECT * FROM daily_summary WHERE trading_date >= ? AND trading_date <= ? ORDER BY trading_date",
        (cutoff, d),
    )


def fetch_vix_history_today(conn, d):
    if not table_exists(conn, "vix_history"):
        return []
    return q(conn, "SELECT * FROM vix_history WHERE date=? ORDER BY timestamp", (d,))


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
        "open":  closes[0]  if closes else None,
        "close": closes[-1] if closes else None,
        "high":  max(highs) if highs else None,
        "low":   min(lows)  if lows  else None,
        "total_volume":       sum(volumes) if volumes else 0,
        "avg_bar_range_pts":  round(statistics.mean(ranges), 3) if ranges else None,
        "max_bar_range_pts":  round(max(ranges), 3) if ranges else None,
        "zero_volume_bars":   sum(1 for v in volumes if v == 0),
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
    total_pnl   = sum(pnls)
    gross_wins  = sum(p for p in pnls if p > 0)
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
    chain_pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else None
    return {
        "total_rows":            len(chain_rows),
        "unique_capture_times":  len(capture_times),
        "unique_strikes":        len(strikes),
        "strike_range":          [min(strikes), max(strikes)] if strikes else None,
        "zero_bid_ask_count":    zero_bid_ask,
        "zero_bid_ask_pct":      round(zero_bid_ask / len(chain_rows) * 100, 1) if chain_rows else 0,
        "avg_spread":            round(statistics.mean(spreads), 3) if spreads else None,
        "max_spread":            round(max(spreads), 3) if spreads else None,
        "avg_iv_pct":            round(statistics.mean(ivs) * 100, 2) if ivs else None,
        "iv_range_pct":          [round(min(ivs) * 100, 2), round(max(ivs) * 100, 2)] if ivs else None,
        "total_call_oi":         total_call_oi,
        "total_put_oi":          total_put_oi,
        "chain_pcr":             chain_pcr,
        "first_capture":         capture_times[0]  if capture_times else None,
        "last_capture":          capture_times[-1] if capture_times else None,
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
        "vrp_mean":            round(statistics.mean(vrps), 3),
        "vrp_min":             round(min(vrps), 3),
        "vrp_max":             round(max(vrps), 3),
        "vrp_stdev":           round(statistics.stdev(vrps), 3) if len(vrps) > 1 else 0,
        "vrp_positive_cycles": sum(1 for v in vrps if v > 0),
        "vrp_negative_cycles": sum(1 for v in vrps if v <= 0),
        "vrp_rich_cycles":     sum(1 for v in vrps if v > 3.0),
        "vrp_very_rich_cycles":sum(1 for v in vrps if v > 5.0),
        "vrp_cheap_cycles":    sum(1 for v in vrps if v < 0),
        "total_vrp_cycles":    len(vrps),
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


def compute_regime_timeline(regime_decisions, cycle_rows):
    if not regime_decisions and not cycle_rows:
        return []
    timeline = []
    for r in regime_decisions:
        timeline.append({
            "time":           r.get("timestamp", ""),
            "source":         "regime_engine",
            "final_regime":   r.get("final_regime"),
            "confidence":     r.get("confidence"),
            "size_multiplier":r.get("size_multiplier"),
            "vol_regime":     r.get("volatility_regime"),
            "price_regime_15":r.get("price_regime_15"),
            "positioning":    r.get("positioning_regime"),
            "adx_15":         r.get("adx_15"),
            "ema_structure":  r.get("ema_structure"),
            "event_day":      r.get("event_day"),
            "calibration_tier":r.get("calibration_tier"),
            "notes":          r.get("notes"),
        })
    for c in cycle_rows:
        if c.get("final_regime"):
            timeline.append({
                "time":           c.get("cycle_time", ""),
                "source":         "cycle_log",
                "final_regime":   c.get("final_regime"),
                "confidence":     c.get("confidence"),
                "size_multiplier":None,
                "vol_regime":     c.get("volatility_condition"),
                "price_regime_15":c.get("price_regime_15"),
                "positioning":    None,
                "adx_15":         c.get("adx_15") or c.get("adx"),
                "ema_structure":  c.get("ema_structure"),
                "event_day":      None,
                "calibration_tier":None,
                "notes":          c.get("no_trade_reason"),
            })
    timeline.sort(key=lambda x: x["time"])
    return timeline


def compute_intraday_spot_profile(cycle_rows):
    spots = [(c["cycle_time"], c["spot"]) for c in cycle_rows if c.get("spot") is not None]
    if not spots:
        return {}
    spot_values = [s[1] for s in spots]
    result = {
        "open":            spot_values[0],
        "close":           spot_values[-1],
        "high":            max(spot_values),
        "low":             min(spot_values),
        "range_pts":       round(max(spot_values) - min(spot_values), 2),
        "range_pct":       round((max(spot_values) - min(spot_values)) / spot_values[0] * 100, 3),
        "net_change_pts":  round(spot_values[-1] - spot_values[0], 2),
        "net_change_pct":  round((spot_values[-1] - spot_values[0]) / spot_values[0] * 100, 3),
        "direction":       "UP" if spot_values[-1] > spot_values[0] else ("DOWN" if spot_values[-1] < spot_values[0] else "FLAT"),
    }
    if len(spot_values) >= 6:
        mid = len(spot_values) // 2
        fh  = max(spot_values[:mid]) - min(spot_values[:mid])
        sh  = max(spot_values[mid:]) - min(spot_values[mid:])
        result["first_half_range_pts"]  = round(fh, 2)
        result["second_half_range_pts"] = round(sh, 2)
        result["volatility_expansion_second_half"] = sh > fh
    return result


def compute_adx_profile(cycle_rows):
    adx_col  = "adx_15" if any(c.get("adx_15") for c in cycle_rows) else "adx"
    adx_vals = [c[adx_col] for c in cycle_rows if c.get(adx_col) is not None]
    if not adx_vals:
        return {}
    return {
        "adx_open":                round(adx_vals[0], 2),
        "adx_close":               round(adx_vals[-1], 2),
        "adx_mean":                round(statistics.mean(adx_vals), 2),
        "adx_max":                 round(max(adx_vals), 2),
        "adx_min":                 round(min(adx_vals), 2),
        "trending_cycles":         sum(1 for v in adx_vals if v > 25),
        "strong_trend_cycles":     sum(1 for v in adx_vals if v > 35),
        "flat_cycles":             sum(1 for v in adx_vals if v < 20),
    }


def compute_pcr_profile(cycle_rows):
    pcrs = [c["pcr"] for c in cycle_rows if c.get("pcr") is not None]
    if not pcrs:
        return {}
    return {
        "pcr_open":                round(pcrs[0], 3),
        "pcr_close":               round(pcrs[-1], 3),
        "pcr_mean":                round(statistics.mean(pcrs), 3),
        "pcr_min":                 round(min(pcrs), 3),
        "pcr_max":                 round(max(pcrs), 3),
        "pcr_change_open_to_close":round(pcrs[-1] - pcrs[0], 3),
        "extreme_fear_cycles":     sum(1 for p in pcrs if p > 1.5),
        "extreme_greed_cycles":    sum(1 for p in pcrs if p < 0.7),
        "neutral_cycles":          sum(1 for p in pcrs if 0.8 <= p <= 1.3),
    }


def compute_skew_profile(cycle_rows):
    skew_col  = "skew" if any(c.get("skew") for c in cycle_rows) else "skew_ratio"
    skews = [c[skew_col] for c in cycle_rows if c.get(skew_col) is not None]
    if not skews:
        return {}
    return {
        "skew_open":            round(skews[0], 3),
        "skew_close":           round(skews[-1], 3),
        "skew_mean":            round(statistics.mean(skews), 3),
        "skew_min":             round(min(skews), 3),
        "skew_max":             round(max(skews), 3),
        "fear_skew_cycles":     sum(1 for s in skews if s > 3.0),
        "complacent_skew_cycles":sum(1 for s in skews if s < 0.95),
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


def compute_calibration_summary(calibration_row):
    if not calibration_row:
        return {"status": "No calibration data found"}
    return {
        "calibration_tier":    calibration_row.get("calibration_tier", 0),
        "is_valid":            bool(calibration_row.get("is_valid")),
        "n_trading_days":      calibration_row.get("n_trading_days"),
        "n_tuesday_expiries":  calibration_row.get("n_tuesday_expiries"),
        "calibrated_at":       calibration_row.get("calibrated_at"),
        "vix_p25":             calibration_row.get("vix_p25"),
        "vix_p50":             calibration_row.get("vix_p50"),
        "vix_p75":             calibration_row.get("vix_p75"),
        "vix_p90":             calibration_row.get("vix_p90"),
        "skew_bearish_threshold": calibration_row.get("skew_bearish_threshold"),
        "skew_bullish_threshold": calibration_row.get("skew_bullish_threshold"),
        "oi_buildup_threshold":   calibration_row.get("oi_buildup_threshold"),
        "oi_unwind_threshold":    calibration_row.get("oi_unwind_threshold"),
        "oi_wall_strong_cal":     calibration_row.get("oi_wall_strong_cal"),
        "oi_wall_moderate_cal":   calibration_row.get("oi_wall_moderate_cal"),
        "straddle_ratio_sell":    calibration_row.get("straddle_ratio_sell"),
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
        counts[t["strategy_name"]] = counts.get(t["strategy_name"], 0) + 1
    return counts


def compute_pnl_curve(cycle_rows):
    curve = []
    for c in cycle_rows:
        if c.get("cycle_time") and c.get("daily_pnl_net") is not None:
            curve.append({
                "time":    c["cycle_time"],
                "pnl":     c["daily_pnl_net"],
                "spot":    c.get("spot"),
                "vrp":     c.get("vrp"),
                "adx":     c.get("adx_15") or c.get("adx"),
                "regime":  c.get("final_regime"),
            })
    return curve


def detect_anomalies(
    session_state, cycle_rows, api_summary, daily_summary,
    decisions, trade_exits, vrp_stats, spot_profile,
    regime_decisions, calibration_summary
):
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
        missing_vrp   = sum(1 for c in cycle_rows if c.get("vrp") is None)
        missing_spot  = sum(1 for c in cycle_rows if c.get("spot") is None)
        unknown_vol   = sum(1 for c in cycle_rows if c.get("volatility_condition") == "UNKNOWN")
        missing_pcr   = sum(1 for c in cycle_rows if c.get("pcr") is None)
        missing_skew  = sum(1 for c in cycle_rows if c.get("skew") is None and c.get("skew_ratio") is None)
        no_regime     = sum(1 for c in cycle_rows if not c.get("final_regime"))
        if missing_vrp > len(cycle_rows) * 0.3:
            flags.append(f"[FLAG] {missing_vrp}/{len(cycle_rows)} cycles had missing VRP data.")
        if missing_spot > 0:
            flags.append(f"[FLAG] {missing_spot} cycles had missing spot price.")
        if unknown_vol > 3:
            flags.append(f"[FLAG] {unknown_vol} cycles had volatility_condition=UNKNOWN.")
        if missing_pcr > len(cycle_rows) * 0.5:
            flags.append(f"[FLAG] {missing_pcr}/{len(cycle_rows)} cycles had missing PCR.")
        if missing_skew > len(cycle_rows) * 0.5:
            flags.append(f"[FLAG] {missing_skew}/{len(cycle_rows)} cycles had missing skew.")
        if no_regime > len(cycle_rows) * 0.5:
            flags.append(f"[FLAG] {no_regime}/{len(cycle_rows)} cycles had no final_regime — regime engine output not merged into cycle_log.")

    if api_summary.get("error_count", 0) > 5:
        flags.append(f"[FLAG] {api_summary['error_count']} API errors occurred today.")
    if api_summary.get("rate_limited_count", 0) > 0:
        flags.append(f"[FLAG] {api_summary['rate_limited_count']} API calls were rate-limited.")
    if api_summary.get("slow_calls_over_2s", 0) > 3:
        flags.append(f"[FLAG] {api_summary['slow_calls_over_2s']} API calls took over 2 seconds.")

    if daily_summary and (daily_summary.get("stops_fired") or 0) >= 2:
        flags.append(f"[FLAG] {daily_summary['stops_fired']} stop-losses fired today.")

    if vrp_stats.get("vrp_negative_cycles", 0) > 0 and vrp_stats.get("total_vrp_cycles", 0) > 0:
        neg_pct = vrp_stats["vrp_negative_cycles"] / vrp_stats["total_vrp_cycles"] * 100
        if neg_pct > 30:
            flags.append(f"[FLAG] VRP was negative in {neg_pct:.0f}% of cycles — IV was BELOW realized vol.")

    if spot_profile.get("range_pct", 0) > 1.5:
        flags.append(f"[FLAG] NIFTY moved {spot_profile.get('range_pct', 0):.2f}% intraday — high-move day.")
    if spot_profile.get("range_pct", 0) < 0.3:
        flags.append(f"[FLAG] NIFTY moved only {spot_profile.get('range_pct', 0):.2f}% — very low move day.")

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
                    f"[FLAG] Single no-trade reason dominates: '{top_reason}' "
                    f"fired {top_count}/{total_decisions} times "
                    f"({top_count/total_decisions*100:.0f}%) — gate may be miscalibrated."
                )
            else:
                flags.append(
                    f"[OK] Single position engine working: '{top_reason}' "
                    f"fired {top_count}/{total_decisions} times — expected behavior."
                )

    stop_exits = [e for e in trade_exits if e.get("exit_reason") == "CLOSE_STOP"]
    if len(stop_exits) >= 2:
        flags.append(f"[FLAG] {len(stop_exits)} positions hit stop-loss today.")

    if not regime_decisions:
        flags.append("[FLAG] No regime_decisions rows found — regime engine output not being persisted. Check regime_bridge integration.")
    else:
        emergency = [r for r in regime_decisions if r.get("final_regime") == "EMERGENCY_EXIT"]
        if emergency:
            flags.append(f"[FLAG] {len(emergency)} EMERGENCY_EXIT regime decisions recorded today.")

    cal_tier = calibration_summary.get("calibration_tier", 0)
    cal_valid = calibration_summary.get("is_valid", False)
    if not cal_valid:
        flags.append(
            f"[INFO] Calibration tier={cal_tier} — engine operating on Config estimates. "
            f"Need {20} trading days for Tier 2 calibration."
        )
    else:
        flags.append(f"[OK] Calibration tier={cal_tier} VALID — thresholds are data-derived.")

    if not flags:
        flags.append("[OK] No major anomalies auto-detected.")
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
        events.append((
            c.get("cycle_time") or "",
            "CYCLE",
            f"spot={c.get('spot')} vix={c.get('vix')} vrp={c.get('vrp')} "
            f"vol={c.get('volatility_condition')} trend={c.get('trend_condition')} "
            f"dir={c.get('direction')} adx={c.get('adx_15') or c.get('adx')} "
            f"pcr={c.get('pcr')} skew={c.get('skew') or c.get('skew_ratio')} "
            f"regime={c.get('final_regime')} conf={c.get('confidence')} "
            f"action={c.get('action_taken')} pnl={c.get('daily_pnl_net')}"
        ))
    for r in regime_decisions:
        events.append((
            r.get("timestamp") or "",
            "REGIME",
            f"{r.get('final_regime')} conf={r.get('confidence')} "
            f"size={r.get('size_multiplier')} vol={r.get('volatility_regime')} "
            f"price15={r.get('price_regime_15')} pos={r.get('positioning_regime')} "
            f"adx15={r.get('adx_15')} ema={r.get('ema_structure')} "
            f"tier={r.get('calibration_tier')} notes={r.get('notes')}"
        ))
    for d in decisions:
        events.append((
            d.get("decision_time") or "",
            "DECISION",
            f"{d.get('action')} {d.get('strategy_name') or ''} — {d.get('reason')}"
        ))
    for t in trade_entries:
        events.append((
            t.get("entry_time") or "",
            "ENTRY",
            f"{t.get('strategy_name')} lots={t.get('final_lots')} "
            f"credit/debit={t.get('entry_credit') or t.get('entry_debit')} "
            f"vrp={t.get('entry_vrp')} vix={t.get('entry_vix')} "
            f"vol={t.get('volatility_condition')} trend={t.get('trend_condition')} "
            f"regime={t.get('final_regime_at_entry')} conf={t.get('confidence_at_entry')} "
            f"event={t.get('event_day')} defined_risk={t.get('defined_risk_only')}"
        ))
    for e in trade_exits:
        events.append((
            e.get("exit_time") or "",
            "EXIT",
            f"{e.get('strategy_name')} reason={e.get('exit_reason')} "
            f"hold={e.get('hold_minutes')}min net_pnl={e.get('net_pnl_rupees')} "
            f"result={e.get('result')}"
        ))
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
    lines = []
    for k, v in d.items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines) + "\n"


def md_table(rows, columns, max_rows=80):
    if not rows:
        return "_(no data)_\n"
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
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
        out += f"\n_... {len(rows) - max_rows} more row(s) omitted._\n"
    return out


def generate_report(target_date):
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"TARGET_DATE '{target_date}' must be YYYY-MM-DD format.")

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
    vix_history_today  = fetch_vix_history_today(conn, target_date)

    conn.close()

    no_trade_reasons   = aggregate_no_trade_reasons(decisions)
    strategies_used    = aggregate_strategies(trade_entries)
    chain_summary      = summarize_option_chain(chain_rows)
    api_summary        = summarize_api_calls(api_rows)
    vrp_stats          = compute_vrp_statistics(cycle_rows)
    spot_profile       = compute_intraday_spot_profile(cycle_rows)
    adx_profile        = compute_adx_profile(cycle_rows)
    pcr_profile        = compute_pcr_profile(cycle_rows)
    skew_profile       = compute_skew_profile(cycle_rows)
    pnl_curve          = compute_pnl_curve(cycle_rows)
    candle_stats       = compute_candle_statistics(intraday_candles)
    equity_curve       = compute_equity_curve(cumulative_days)
    regime_timeline    = compute_regime_timeline(regime_decisions, cycle_rows)
    regime_dist        = compute_regime_distribution(regime_decisions, cycle_rows)
    calibration_summary = compute_calibration_summary(calibration_row)
    anomalies          = detect_anomalies(
        session_state, cycle_rows, api_summary, daily_summary,
        decisions, trade_exits, vrp_stats, spot_profile,
        regime_decisions, calibration_summary
    )
    timeline = build_master_timeline(
        cycle_rows, decisions, trade_entries, trade_exits,
        regime_decisions, audit_file_lines
    )
    warning_error_lines = filter_log_lines_by_level(
        audit_file_lines, {"WARNING", "ERROR", "CRITICAL"}
    )

    exits_by_position = {e["position_id"]: e for e in trade_exits}
    legs_by_position  = {}
    for leg in legs:
        legs_by_position.setdefault(leg["position_id"], []).append(leg)

    md = []
    md.append(f"# NIFTY Intraday Options Engine v2.0 — EOD Forensic Report")
    md.append(f"**Target Date:** {target_date}  |  **Report Generated:** {datetime.now().isoformat()}\n")

    md.append("## 0. Engine Architecture Note\n")
    md.append(
        "This report covers the integrated v2.0 engine. The regime engine "
        "(regime_engine.py) classifies volatility, price, and positioning regimes "
        "using ADX, EMA, HH/HL, ORB, OI change, skew, and PCR. Its output is merged "
        "into signals via regime_bridge.py before strategy selection. "
        "Calibration is tiered (0-3) and improves automatically as data accumulates. "
        "All positions are intraday only — no overnight holding.\n"
    )

    md.append("## 1. Table of Contents\n")
    md.append(
        "2. Executive Summary\n3. Auto-Detected Anomalies\n"
        "4. Calibration Status\n5. Session Configuration\n"
        "6. NIFTY Intraday Profile\n7. VRP / Volatility Deep Dive\n"
        "8. ADX / Trend Profile\n9. PCR / Skew / Directional Profile\n"
        "10. Regime Engine Timeline\n11. Regime Distribution\n"
        "12. Market Data Timeline (cycle_log)\n13. Gate Blockage Analysis\n"
        "14. Strategy Decision Log\n15. No-Trade Reason Frequency\n"
        "16. Trade-by-Trade Deep Dive\n17. P&L Curve (intraday)\n"
        "18. Option Chain Statistics\n19. Intraday Candle Statistics\n"
        "20. API Call Health\n21. Data Quality Checks\n"
        "22. Cumulative Performance (90-day)\n23. Prior Days Comparison\n"
        "24. Audit Log Warnings and Errors\n25. Unified Master Timeline\n"
        "26. Daily Summary\n27. Raw Data Export Manifest\n"
    )

    md.append("## 2. Executive Summary\n")
    net_pnl      = round(sum(e.get("net_pnl_rupees") or 0 for e in trade_exits), 2)
    gross_pnl    = round(sum(e.get("gross_pnl_rupees") or 0 for e in trade_exits), 2)
    total_costs  = round(sum(e.get("total_costs_rupees") or 0 for e in trade_exits), 2)
    exec_summary = {
        "Day label":                   session_state.get("day_label") if session_state else "N/A",
        "Day mode":                    session_state.get("day_mode") if session_state else "N/A",
        "VIX regime (final)":          session_state.get("vix_regime") if session_state else "N/A",
        "OR condition / width":        f"{session_state.get('or_condition')} / {session_state.get('or_width')}" if session_state else "N/A",
        "Calibration tier":            calibration_summary.get("calibration_tier"),
        "Calibration valid":           calibration_summary.get("is_valid"),
        "Trades attempted (decisions)":len(decisions),
        "Trades executed":             len(trade_entries),
        "Trades closed":               len(trade_exits),
        "Wins / Losses":               f"{sum(1 for e in trade_exits if e.get('result')=='WIN')} / {sum(1 for e in trade_exits if e.get('result')=='LOSS')}",
        "Gross P&L (Rs)":              gross_pnl,
        "Total Costs (Rs)":            total_costs,
        "Net P&L (Rs)":                net_pnl,
        "Net P&L as pct of capital":   f"{round(net_pnl / STARTING_CAPITAL * 100, 3)}%" if STARTING_CAPITAL else "N/A",
        "Daily halted":                session_state.get("daily_halted") if session_state else "N/A",
        "Consecutive stops":           session_state.get("consecutive_stops") if session_state else "N/A",
        "Cycles logged":               len(cycle_rows),
        "Regime decisions logged":     len(regime_decisions),
        "NIFTY open / close / range":  f"{spot_profile.get('open')} / {spot_profile.get('close')} / {spot_profile.get('range_pts')}pts ({spot_profile.get('range_pct')}%)" if spot_profile else "N/A",
        "VRP mean today (pp)":         vrp_stats.get("vrp_mean"),
        "ATM IV open / close (pct)":   f"{vrp_stats.get('atm_iv_open_pct')} / {vrp_stats.get('atm_iv_close_pct')}",
        "IV crush today (pp)":         vrp_stats.get("iv_crush_pct"),
        "Option chain snapshot rows":  len(chain_rows),
        "API calls made":              len(api_rows),
        "Audit log lines (file)":      len(audit_file_lines),
        "VIX history rows today":      len(vix_history_today),
    }
    md.append(md_kv(exec_summary))

    md.append("## 3. Auto-Detected Anomalies / Flags\n")
    md.append("\n".join(f"- {f}" for f in anomalies) + "\n")

    md.append("## 4. Calibration Status\n")
    md.append(md_kv(calibration_summary))
    md.append(
        "\n**Calibration tier guide:** Tier 0 = Config estimates only. "
        "Tier 1 = VIX percentiles from live data (5+ days). "
        "Tier 2 = All thresholds data-derived (20+ days) — is_calibrated=True. "
        "Tier 3 = Robust calibration (60+ days). "
        "Skew, OI change, and OI wall thresholds are calibrated from market_snapshots table.\n"
    )

    md.append("## 5. Session Configuration Snapshot\n")
    md.append(md_kv(session_state) if session_state else "_No session_state row found._\n")

    md.append("## 6. NIFTY Intraday Profile\n")
    md.append(md_kv(spot_profile))

    md.append("## 7. VRP / Volatility Deep Dive\n")
    md.append(md_kv(vrp_stats))
    md.append(
        "\n**VRP interpretation (NIFTY 2026):** VRP > 3pp = RICH (sell premium). "
        "VRP 1.5-3pp = FAIR (reduced size). VRP < 0 = CHEAP (buy premium). "
        "NIFTY ATM IV typically 12-22% in normal VIX regime. "
        "IV crush on event days can be 15-30%.\n"
    )

    md.append("## 8. ADX / Trend Profile\n")
    md.append(md_kv(adx_profile))
    md.append(
        "\n**ADX thresholds (v2.0):** < 20 = flat/range. "
        "20-25 = weak trend. 25-35 = moderate trend (trend threshold). "
        "> 35 = strong trend (strong threshold). "
        "ADX is Wilder-smoothed on 15-min and 60-min bars.\n"
    )

    md.append("## 9. PCR / Skew / Directional Profile\n")
    md.append(md_kv(pcr_profile))
    md.append(md_kv(skew_profile))
    md.append(
        "\n**PCR (NIFTY 2026):** > 1.55 = extreme fear. 1.28-1.55 = bearish. "
        "0.72-1.28 = neutral. 0.60-0.72 = bullish. < 0.60 = extreme greed. "
        "**Skew:** OTM PE IV minus OTM CE IV (ATM-50 vs ATM+50). "
        "> 3.0 = bearish fear premium. < -1.5 = bullish complacency.\n"
    )

    md.append("## 10. Regime Engine Timeline\n")
    if regime_timeline:
        md.append(
            f"Total regime decisions: {len(regime_decisions)}. "
            f"Timeline entries: {len(regime_timeline)}.\n"
        )
        md.append(md_table(
            regime_timeline,
            ["time", "source", "final_regime", "confidence", "size_multiplier",
             "vol_regime", "price_regime_15", "positioning", "adx_15",
             "ema_structure", "calibration_tier", "notes"],
            max_rows=100
        ))
    else:
        md.append(
            "_No regime_decisions rows found. "
            "Ensure regime_bridge.merge_regime_into_signals() is called in main.py "
            "and regime_engine.process_signals() persists to regime_decisions table._\n"
        )

    md.append("## 11. Regime Distribution Today\n")
    if regime_dist:
        for regime, count in regime_dist.items():
            md.append(f"- **{regime}**: {count} decisions\n")
    else:
        md.append("_No regime distribution data available._\n")

    md.append("## 12. Market Data Timeline (cycle_log)\n")
    md.append(f"Total cycles: {len(cycle_rows)}.\n")
    md.append(md_table(
        cycle_rows,
        ["cycle_time", "spot", "vix", "vrp", "atm_iv_pct", "parkinson_rv_pct",
         "adx", "adx_condition", "vwap_dist_pct", "pcr", "skew_ratio",
         "or_condition", "volatility_condition", "trend_condition", "direction",
         "final_regime", "confidence", "action_taken", "no_trade_reason",
         "open_positions", "daily_pnl_net"],
        max_rows=100
    ))

    md.append("## 13. Gate Blockage Analysis\n")
    gate_categories = {
        "risk_gates":       ["daily_loss_limit", "max_entries", "max_concurrent", "consecutive_stops", "stop_cooldown", "projected_daily_loss"],
        "data_gates":       ["vrp_unknown", "opening_range_not", "or_pending", "chain_unavailable", "vix_unknown"],
        "market_condition": ["circuit_breaker", "vix_spike", "iv_expanding", "iv_spiking"],
        "timing_gates":     ["before_entry_window", "past_entry_window", "entry_timing", "0dte", "hard_exit", "13:00"],
        "strategy_gates":   ["no_conditions_met", "params_invalid", "strategy_rules_failed", "credit_", "net_credit", "dte="],
        "event_gates":      ["event_day", "vix_suppressed", "very_wide_or"],
        "regime_gates":     ["regime_engine", "EMERGENCY_EXIT", "NO_TRADE: Choppy", "NO_TRADE: Before", "NO_TRADE: Past"],
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

    md.append("## 14. Strategy Decision Log\n")
    md.append(md_table(decisions, ["decision_time", "action", "strategy_name", "reason"], max_rows=100))

    md.append("## 15. No-Trade Reason Frequency\n")
    if no_trade_reasons:
        for reason, count in no_trade_reasons.items():
            md.append(f"- [{count}x] {reason}\n")
    else:
        md.append("_No NO_TRADE decisions recorded._\n")

    md.append("## 16. Trade-by-Trade Deep Dive\n")
    if not trade_entries:
        md.append("_No trades were entered on this date._\n")
    for t in trade_entries:
        md.append(f"\n### Position `{t['position_id']}` — {t['strategy_name']}\n")
        md.append(md_kv({
            "Entry time":                t.get("entry_time"),
            "Day label":                 t.get("day_label"),
            "Selection reason":          t.get("selection_reason"),
            "Entry spot / VIX / VRP":    f"{t.get('entry_spot')} / {t.get('entry_vix')} / {t.get('entry_vrp')}",
            "ATM IV at entry":           t.get("entry_atm_iv"),
            "ADX at entry":              t.get("entry_adx"),
            "VWAP dist at entry":        t.get("entry_vwap_dist"),
            "PCR at entry":              t.get("entry_pcr"),
            "Skew at entry":             t.get("entry_skew_ratio"),
            "Volatility condition":      t.get("volatility_condition"),
            "IV behavior":               t.get("iv_behavior"),
            "Trend condition":           t.get("trend_condition"),
            "ADX condition":             t.get("adx_condition"),
            "Direction":                 t.get("direction"),
            "Final regime at entry":     t.get("final_regime_at_entry"),
            "Confidence at entry":       t.get("confidence_at_entry"),
            "Defined risk only":         t.get("defined_risk_only"),
            "Event day":                 t.get("event_day"),
            "OR condition / width":      f"{t.get('or_condition')} / {t.get('or_width')}",
            "Target expiry / DTE":       f"{t.get('target_expiry')} / {t.get('actual_dte')}",
            "Entry credit/debit (pts)":  t.get("entry_credit") or t.get("entry_debit"),
            "Gross credit (pts)":        t.get("gross_credit"),
            "Entry costs (Rs)":          t.get("entry_costs_rupees"),
            "Stop premium (pts)":        t.get("stop_premium"),
            "Target premium (pts)":      t.get("target_premium"),
            "Price stop (pts)":          t.get("price_stop_pts"),
            "Final lots":                t.get("final_lots"),
            "Max loss/lot (Rs)":         t.get("max_loss_per_lot"),
            "Total max risk (Rs)":       t.get("total_max_risk"),
            "Capital at entry":          t.get("capital_at_entry"),
            "Daily P&L at entry":        t.get("daily_pnl_at_entry"),
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
                "Exit time":              exit_row.get("exit_time"),
                "Exit reason":            exit_row.get("exit_reason"),
                "Hold time (min)":        exit_row.get("hold_minutes"),
                "Exit premium (pts)":     exit_row.get("exit_premium"),
                "Exit spot":              exit_row.get("exit_spot"),
                "Exit VIX":               exit_row.get("exit_vix"),
                "Exit ADX":               exit_row.get("exit_adx"),
                "Gross P&L (pts / Rs)":   f"{exit_row.get('gross_pnl_pts')} / {exit_row.get('gross_pnl_rupees')}",
                "Exit costs (Rs)":        exit_row.get("exit_costs_rupees"),
                "Total costs (Rs)":       exit_row.get("total_costs_rupees"),
                "Net P&L (pts / Rs / %)": f"{exit_row.get('net_pnl_pts')} / {exit_row.get('net_pnl_rupees')} / {exit_row.get('net_pnl_pct')}",
                "Result":                 exit_row.get("result"),
                "Profit pct of credit":   exit_row.get("profit_pct_of_credit"),
            }))
        else:
            md.append(f"\n**Exit:** _No matching exit row — position may still be open._\n")

    md.append("## 17. P&L Curve (intraday)\n")
    if pnl_curve:
        md.append(md_table(pnl_curve, ["time", "pnl", "spot", "vrp", "adx", "regime"], max_rows=100))
    else:
        md.append("_No P&L curve data available._\n")

    md.append("## 18. Option Chain Statistics\n")
    md.append(md_kv(chain_summary))
    md.append(f"\n_Full chain ({len(chain_rows)} rows) in raw JSON export._\n")

    md.append("## 19. Intraday 1-Minute Candle Statistics\n")
    md.append(md_kv(candle_stats) if candle_stats else "_No intraday candle data stored._\n")
    md.append(f"\n_Total 1-min bars in DB: {len(intraday_candles)}._\n")

    md.append("## 20. API Call Health\n")
    md.append(md_kv({k: v for k, v in api_summary.items() if k != "errors_sample"}))
    if api_summary.get("errors_sample"):
        md.append("\n**Sample API errors:**\n")
        md.append(md_table(api_summary["errors_sample"], ["call_time", "category", "endpoint", "status_code", "error_message"]))

    md.append("## 21. Data Quality Checks\n")
    dq = {
        "Cycles with missing spot":                sum(1 for c in cycle_rows if c.get("spot") is None),
        "Cycles with missing VIX":                 sum(1 for c in cycle_rows if c.get("vix") is None),
        "Cycles with missing VRP":                 sum(1 for c in cycle_rows if c.get("vrp") is None),
        "Cycles with missing PCR":                 sum(1 for c in cycle_rows if c.get("pcr") is None),
        "Cycles with missing skew":                sum(1 for c in cycle_rows if c.get("skew") is None and c.get("skew_ratio") is None),
        "Cycles with no final_regime":             sum(1 for c in cycle_rows if not c.get("final_regime")),
        "Cycles with volatility_condition=UNKNOWN":sum(1 for c in cycle_rows if c.get("volatility_condition") == "UNKNOWN"),
        "Cycles with trend_condition=OR_PENDING":  sum(1 for c in cycle_rows if c.get("trend_condition") == "OR_PENDING"),
        "Regime decisions logged":                 len(regime_decisions),
        "Option chain rows with zero bid/ask":     chain_summary.get("zero_bid_ask_count", 0),
        "Trades with no matching exit row":        sum(1 for t in trade_entries if t["position_id"] not in exits_by_position),
        "Positions still OPEN at report time":     sum(1 for p in positions if p.get("status") == "OPEN"),
        "VIX history rows today":                  len(vix_history_today),
        "Calibration tier":                        calibration_summary.get("calibration_tier"),
        "Calibration valid":                       calibration_summary.get("is_valid"),
    }
    md.append(md_kv(dq))

    md.append("## 22. Cumulative Engine Performance (90-day)\n")
    if equity_curve:
        md.append(md_kv({k: v for k, v in equity_curve.items() if k != "daily_pnl_series"}))
        if equity_curve.get("daily_pnl_series"):
            md.append("\n**Daily P&L Series:**\n")
            md.append(md_table(equity_curve["daily_pnl_series"], ["date", "pnl", "capital"], max_rows=90))
    else:
        md.append("_No cumulative data yet._\n")

    md.append("## 23. Prior Days Comparison\n")
    if prior_days_summary:
        md.append(md_table(
            prior_days_summary,
            ["trading_date", "day_label", "trades_executed", "win_rate_pct",
             "net_pnl_rupees", "net_pnl_pct_capital", "vrp_mean",
             "or_condition", "stops_fired", "profit_factor", "capital_end"],
            max_rows=10
        ))
    else:
        md.append("_No prior days summary data available._\n")

    md.append("## 24. Audit Log — Warnings and Errors\n")
    md.append(
        f"Total WARNING/ERROR/CRITICAL lines: {len(warning_error_lines)} of "
        f"{len(audit_file_lines)} total ({len(audit_db_rows)} in DB table)\n\n"
    )
    if warning_error_lines:
        md.append("```\n" + "\n".join(warning_error_lines[:200]) + "\n```\n")
        if len(warning_error_lines) > 200:
            md.append(f"_... {len(warning_error_lines) - 200} more lines in raw export._\n")
    else:
        md.append("_No warnings or errors logged today._\n")

    md.append("## 25. Unified Master Timeline\n")
    md.append(md_table(
        [{"time": e[0], "type": e[1], "detail": e[2]} for e in timeline],
        ["time", "type", "detail"],
        max_rows=200
    ))

    md.append("## 26. Daily Summary (engine EOD)\n")
    md.append(md_kv(daily_summary) if daily_summary else "_No daily_summary row found._\n")

    md.append("## 27. Raw Data Export Manifest\n")
    md.append(f"All tables exported to: `eod_report_{target_date}_raw/`\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"eod_report_{target_date}.md"
    report_path.write_text("\n".join(md), encoding="utf-8")

    raw_dir = OUTPUT_DIR / f"eod_report_{target_date}_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_exports = {
        "session_state":         session_state,
        "cycle_log":             cycle_rows,
        "strategy_decisions":    decisions,
        "positions":             positions,
        "position_legs":         legs,
        "trade_entries":         trade_entries,
        "trade_exits":           trade_exits,
        "daily_summary":         daily_summary,
        "option_chain_snapshot": chain_rows,
        "api_call_log":          api_rows,
        "audit_log_db":          audit_db_rows,
        "audit_log_file_lines":  audit_file_lines,
        "regime_decisions":      regime_decisions,
        "calibration_state":     calibration_row,
        "vix_history_today":     vix_history_today,
        "master_timeline":       timeline,
        "anomaly_flags":         anomalies,
        "no_trade_reason_counts":no_trade_reasons,
        "strategies_used_counts":strategies_used,
        "chain_summary":         chain_summary,
        "vrp_statistics":        vrp_stats,
        "spot_profile":          spot_profile,
        "adx_profile":           adx_profile,
        "pcr_profile":           pcr_profile,
        "skew_profile":          skew_profile,
        "pnl_curve":             pnl_curve,
        "regime_timeline":       regime_timeline,
        "regime_distribution":   regime_dist,
        "calibration_summary":   calibration_summary,
        "intraday_candles_1min": intraday_candles,
        "candle_statistics":     candle_stats,
        "cumulative_performance_90d": equity_curve,
        "api_summary":           {k: v for k, v in api_summary.items() if k != "errors_sample"},
    }
    for name, data in raw_exports.items():
        (raw_dir / f"{name}.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    print(f"Report written to: {report_path}")
    print(f"Raw data exported to: {raw_dir}/")
    print(f"\nQuick summary for {target_date}:")
    for k, v in exec_summary.items():
        print(f"  {k}: {v}")
    print("\nAnomaly flags:")
    for f in anomalies:
        print(f"  {f}")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    td = TARGET_DATE
    for i, a in enumerate(args):
        if a == "--date" and i + 1 < len(args):
            td = args[i + 1]
    generate_report(td)