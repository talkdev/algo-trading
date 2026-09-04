import sqlite3
import json
import math
import statistics
import csv
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "backtest_results"


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

LOT_SIZE = int(_ENV.get("NIFTY_LOT_SIZE", "65") or 65)
STT_RATE = float(_ENV.get("STT_RATE", "0.0015") or 0.0015)
EXCHANGE_TXN_RATE = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003552") or 0.0003552)
BROKERAGE_PER_ORDER = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)
SEBI_RATE = float(_ENV.get("SEBI_RATE", "0.000001") or 0.000001)
STAMP_DUTY = float(_ENV.get("STAMP_DUTY_BUY_OPTIONS", "0.00003") or 0.00003)
STARTING_CAPITAL = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1000000)
NIFTY_STRIKE_STEP = int(_ENV.get("NIFTY_STRIKE_STEP", "50") or 50)

MIN_SAMPLES_SIGNIFICANCE = 10
MIN_SAMPLES_RELIABLE = 30
RISK_FREE_ANNUAL = 0.065
TRADING_DAYS_YEAR = 252


def get_conn():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def q(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def compute_costs(legs, lots):
    sell_prem = sum(l.get("exec_price", 0) for l in legs if l.get("action") == "SELL")
    buy_prem = sum(l.get("exec_price", 0) for l in legs if l.get("action") == "BUY")
    turnover = sell_prem + buy_prem
    n_orders = len(legs)
    stt = sell_prem * LOT_SIZE * lots * STT_RATE
    exchange = turnover * LOT_SIZE * lots * EXCHANGE_TXN_RATE
    sebi = turnover * LOT_SIZE * lots * SEBI_RATE
    stamp = buy_prem * LOT_SIZE * lots * STAMP_DUTY
    brokerage = BROKERAGE_PER_ORDER * n_orders
    gst = (brokerage + exchange + sebi) * 0.18
    return round(stt + exchange + sebi + stamp + brokerage + gst, 2)


def find_delta_strike(chain, opt_type, target_delta, tolerance=0.10):
    best = None
    best_diff = float("inf")
    for strike, legs in chain.items():
        leg = legs.get(opt_type, {})
        delta = leg.get("delta")
        if delta is None:
            continue
        diff = abs(abs(delta) - target_delta)
        if diff < best_diff:
            best_diff = diff
            best = strike
    return best if best_diff <= tolerance else None


def exec_price(chain, strike, opt_type, action):
    leg = chain.get(strike, {}).get(opt_type, {})
    bid = leg.get("bid", 0) or 0
    ask = leg.get("ask", 0) or 0
    ltp = leg.get("ltp", 0) or 0
    if bid > 0 and ask > 0:
        return bid if action == "SELL" else ask
    return ltp


def mark_price(chain, strike, opt_type):
    leg = chain.get(strike, {}).get(opt_type, {})
    bid = leg.get("bid", 0) or 0
    ask = leg.get("ask", 0) or 0
    ltp = leg.get("ltp", 0) or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return ltp


def load_chain(conn, cycle_id, capture_time, trading_date, expiry):
    chain = {}
    if cycle_id:
        try:
            rows = q(conn,
                "SELECT strike, option_type, bid, ask, ltp, iv, delta, gamma, vega, theta, oi "
                "FROM option_chain_snapshot WHERE cycle_id=? AND expiry=?",
                (cycle_id, expiry)
            )
            for r in rows:
                s = r["strike"]
                if s not in chain:
                    chain[s] = {}
                chain[s][r["option_type"]] = {k: r[k] for k in ("bid","ask","ltp","iv","delta","gamma","vega","theta","oi")}
        except Exception:
            chain = {}

    if not chain and capture_time:
        try:
            rows = q(conn,
                "SELECT strike, option_type, bid, ask, ltp, iv, delta, gamma, vega, theta, oi "
                "FROM option_chain_snapshot "
                "WHERE trading_date=? AND expiry=? AND capture_time >= ? "
                "ORDER BY capture_time ASC LIMIT 174",
                (trading_date, expiry, capture_time)
            )
            for r in rows:
                s = r["strike"]
                if s not in chain:
                    chain[s] = {}
                chain[s][r["option_type"]] = {k: r[k] for k in ("bid","ask","ltp","iv","delta","gamma","vega","theta","oi")}
        except Exception:
            chain = {}

    return chain


def dte_adjusted_delta(base, dte):
    if dte is None:
        return base
    if dte <= 0:
        return max(0.10, base - 0.12)
    if dte == 1:
        return max(0.12, base - 0.10)
    if dte == 2:
        return max(0.15, base - 0.07)
    if dte == 3:
        return max(0.18, base - 0.05)
    return base


def build_legs(strategy, chain, spot, wing, dte):
    step = NIFTY_STRIKE_STEP
    if not chain or spot is None:
        return None
    td = dte_adjusted_delta(0.25, dte)

    if strategy == "IRON_CONDOR":
        sc = find_delta_strike(chain, "call", td)
        sp = find_delta_strike(chain, "put", td)
        if sc is None or sp is None:
            return None
        lc = sc + wing
        lp = sp - wing
        if lc not in chain:
            lc = min(chain.keys(), key=lambda k: abs(k - (sc + wing)))
        if lp not in chain:
            lp = min(chain.keys(), key=lambda k: abs(k - (sp - wing)))
        if lc <= sc or lp >= sp:
            return None
        return [
            {"strike": sc, "option_type": "call", "action": "SELL"},
            {"strike": sp, "option_type": "put", "action": "SELL"},
            {"strike": lc, "option_type": "call", "action": "BUY"},
            {"strike": lp, "option_type": "put", "action": "BUY"},
        ]

    elif strategy == "BULL_PUT_SPREAD":
        sp = find_delta_strike(chain, "put", td)
        if sp is None:
            return None
        lp = sp - wing
        if lp not in chain:
            lp = min(chain.keys(), key=lambda k: abs(k - (sp - wing)))
        if lp >= sp:
            return None
        return [
            {"strike": sp, "option_type": "put", "action": "SELL"},
            {"strike": lp, "option_type": "put", "action": "BUY"},
        ]

    elif strategy == "BEAR_CALL_SPREAD":
        sc = find_delta_strike(chain, "call", td)
        if sc is None:
            return None
        lc = sc + wing
        if lc not in chain:
            lc = min(chain.keys(), key=lambda k: abs(k - (sc + wing)))
        if lc <= sc:
            return None
        return [
            {"strike": sc, "option_type": "call", "action": "SELL"},
            {"strike": lc, "option_type": "call", "action": "BUY"},
        ]

    return None


def select_strategy(cycle):
    vol = cycle.get("volatility_condition") or "UNKNOWN"
    trend = cycle.get("trend_condition") or "OR_PENDING"
    dirn = cycle.get("direction") or "NEUTRAL"
    iv_beh = cycle.get("iv_behavior") or "UNKNOWN"
    vix_reg = cycle.get("vix_regime") or "NORMAL"
    vrp = cycle.get("vrp") or 0
    vwap_dist = cycle.get("vwap_dist_pct") or 0

    if vol == "UNKNOWN" or trend in ("OR_PENDING", "UNKNOWN"):
        return "NO_TRADE", "insufficient_data"
    if vix_reg == "SUPPRESSED" and vrp < 3.0:
        return "NO_TRADE", "vix_suppressed_low_vrp"
    if iv_beh in ("SPIKING", "EXPANDING"):
        return "NO_TRADE", "iv_expanding_or_spiking"

    if vwap_dist > 0.35:
        vwap_sig = "BULLISH_EXTENDED"
    elif vwap_dist > 0.10:
        vwap_sig = "BULLISH"
    elif vwap_dist < -0.35:
        vwap_sig = "BEARISH_EXTENDED"
    elif vwap_dist < -0.10:
        vwap_sig = "BEARISH"
    else:
        vwap_sig = "NEUTRAL"

    sell_ok = vol in ("RICH", "VERY_RICH") or (vol == "FAIR" and vrp > 1.5)
    buy_ok = vol in ("CHEAP", "INVERTED")

    if vol in ("VERY_RICH", "RICH") and sell_ok:
        if trend in ("RANGE_BOUND", "MILD_RANGE", "RANGE_ASSUMED", "UNCERTAIN"):
            if dirn == "NEUTRAL":
                return "IRON_CONDOR", f"neutral+{vol}+{trend}"
            elif dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                return "BULL_PUT_SPREAD", f"bullish+{vol}+{trend}"
            elif dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                return "BEAR_CALL_SPREAD", f"bearish+{vol}+{trend}"
        elif trend in ("MILD_TREND", "TRENDING", "STRONG_TREND"):
            if dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                return "BULL_PUT_SPREAD", f"bullish_trend+{vol}+{trend}"
            elif dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                return "BEAR_CALL_SPREAD", f"bearish_trend+{vol}+{trend}"
            elif dirn == "NEUTRAL":
                return "IRON_CONDOR", f"neutral_trend+{vol}+{trend}"

    elif vol == "FAIR" and sell_ok:
        if trend in ("RANGE_BOUND", "MILD_RANGE", "RANGE_ASSUMED", "UNCERTAIN"):
            if dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                return "BULL_PUT_SPREAD", f"bullish+fair+range"
            elif dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                return "BEAR_CALL_SPREAD", f"bearish+fair+range"
            elif dirn == "NEUTRAL":
                return "IRON_CONDOR", f"neutral+fair+range"
        elif trend in ("MILD_TREND", "TRENDING"):
            if dirn in ("BULLISH", "MILD_BULLISH") and vwap_sig not in ("BEARISH", "BEARISH_EXTENDED"):
                return "BULL_PUT_SPREAD", f"bullish+fair+trend"
            elif dirn in ("BEARISH", "MILD_BEARISH") and vwap_sig not in ("BULLISH", "BULLISH_EXTENDED"):
                return "BEAR_CALL_SPREAD", f"bearish+fair+trend"

    elif vol in ("CHEAP", "INVERTED") and buy_ok:
        if trend in ("TRENDING", "STRONG_TREND"):
            if dirn in ("BULLISH", "MILD_BULLISH"):
                return "BULL_CALL_SPREAD", f"bullish+cheap+trend"
            elif dirn in ("BEARISH", "MILD_BEARISH"):
                return "BEAR_PUT_SPREAD", f"bearish+cheap+trend"

    return "NO_TRADE", f"no_match:{vol}+{trend}+{dirn}"


def simulate_trade(entry_cycle, subsequent, strategy, legs_spec,
                   gross_credit, net_credit, stop_mult, target_pct, lots):
    entry_costs = compute_costs(
        [{"exec_price": l.get("exec_price", 0), "action": l["action"]} for l in legs_spec],
        lots
    )
    stop_prem = gross_credit * stop_mult
    target_prem = net_credit * (1.0 - target_pct)
    price_stop = 80
    entry_spot = entry_cycle.get("spot")
    max_adverse = 0.0
    exit_reason = "HARD_EXIT"
    exit_prem = net_credit

    for cyc_data in subsequent:
        chain = cyc_data.get("chain", {})
        if not chain:
            continue
        meta = cyc_data.get("meta", {})

        cur_prem = 0.0
        for leg in legs_spec:
            mp = mark_price(chain, leg["strike"], leg["option_type"])
            if mp <= 0:
                mp = leg.get("exec_price", 0) or 0
            cur_prem += mp if leg["action"] == "SELL" else -mp

        if cur_prem > max_adverse:
            max_adverse = cur_prem

        if cur_prem >= stop_prem:
            exit_reason = "CLOSE_STOP"
            exit_prem = cur_prem
            break

        if cur_prem <= target_prem:
            exit_reason = "CLOSE_TARGET"
            exit_prem = cur_prem
            break

        adx = meta.get("adx") or 0
        if strategy in ("IRON_CONDOR", "IRON_BUTTERFLY") and adx > 28:
            exit_reason = "CLOSE_ADX"
            exit_prem = cur_prem
            break

        vwap_dist = meta.get("vwap_dist") or 0
        if strategy == "BULL_PUT_SPREAD" and vwap_dist < -0.25:
            exit_reason = "CLOSE_VWAP"
            exit_prem = cur_prem
            break
        if strategy == "BEAR_CALL_SPREAD" and vwap_dist > 0.25:
            exit_reason = "CLOSE_VWAP"
            exit_prem = cur_prem
            break

        spot = meta.get("spot")
        if entry_spot and spot and abs(spot - entry_spot) >= price_stop:
            exit_reason = "CLOSE_PRICE_STOP"
            exit_prem = cur_prem
            break

        for leg in legs_spec:
            if leg["action"] == "SELL":
                leg_data = chain.get(leg["strike"], {}).get(leg["option_type"], {})
                cur_delta = abs(leg_data.get("delta", 0) or 0)
                if cur_delta > 0.42:
                    exit_reason = "CLOSE_DELTA"
                    exit_prem = cur_prem
                    break

    _sell_legs = [l for l in legs_spec if l["action"] == "SELL"]
    _buy_legs = [l for l in legs_spec if l["action"] == "BUY"]
    _n_sell = max(len(_sell_legs), 1)
    _n_buy = max(len(_buy_legs), 1)
    _exit_sell_price = exit_prem / _n_sell if _n_sell > 0 else exit_prem
    exit_costs = compute_costs(
        [{"exec_price": _exit_sell_price, "action": "BUY" if l["action"] == "SELL" else "SELL"}
         for l in legs_spec],
        lots
    )

    gross_pnl_pts = net_credit - exit_prem
    gross_pnl_rs = gross_pnl_pts * LOT_SIZE * lots
    total_costs = entry_costs + exit_costs
    net_pnl_rs = gross_pnl_rs - total_costs
    result = "WIN" if net_pnl_rs > 0 else ("LOSS" if net_pnl_rs < 0 else "BREAKEVEN")

    return {
        "exit_reason": exit_reason,
        "exit_premium": round(exit_prem, 3),
        "gross_pnl_pts": round(gross_pnl_pts, 3),
        "gross_pnl_rs": round(gross_pnl_rs, 2),
        "entry_costs_rs": round(entry_costs, 2),
        "exit_costs_rs": round(exit_costs, 2),
        "total_costs_rs": round(total_costs, 2),
        "net_pnl_rs": round(net_pnl_rs, 2),
        "result": result,
        "max_adverse_premium": round(max_adverse, 3),
        "adverse_move_pts": round(max_adverse - net_credit, 3),
    }


def cell_stats(trades):
    if not trades:
        return {}
    pnls = [t["net_pnl_rs"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    win_rate = len(wins) / n if n > 0 else 0
    avg_pnl = statistics.mean(pnls) if pnls else 0
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = statistics.mean(losses) if losses else 0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else None

    sharpe = None
    if n >= MIN_SAMPLES_SIGNIFICANCE:
        rf = RISK_FREE_ANNUAL / TRADING_DAYS_YEAR
        excess = [p / STARTING_CAPITAL - rf for p in pnls]
        if len(excess) >= 2:
            std = statistics.stdev(excess)
            if std > 0:
                raw = statistics.mean(excess) / std * math.sqrt(TRADING_DAYS_YEAR)
                sharpe = round(max(-10.0, min(10.0, raw)), 3)

    adverse = [t["adverse_move_pts"] for t in trades]
    adverse_sorted = sorted(adverse)
    p75 = adverse_sorted[int(len(adverse_sorted) * 0.75)] if adverse_sorted else 0
    p90 = adverse_sorted[int(len(adverse_sorted) * 0.90)] if adverse_sorted else 0
    p95 = adverse_sorted[int(len(adverse_sorted) * 0.95)] if adverse_sorted else 0

    credits = [t.get("gross_credit", 0) for t in trades if t.get("gross_credit")]
    avg_credit = statistics.mean(credits) if credits else 0

    costs = [t["total_costs_rs"] for t in trades]
    avg_costs_rs = statistics.mean(costs) if costs else 0
    avg_costs_pts = avg_costs_rs / (LOT_SIZE * statistics.mean([t.get("lots", 1) for t in trades])) if trades else 0

    exit_dist = {}
    for t in trades:
        r = t.get("exit_reason", "UNKNOWN")
        exit_dist[r] = exit_dist.get(r, 0) + 1

    stop_mult_rec = None
    if p90 > 0 and avg_credit > 0 and n >= 3:
        stop_mult_rec = round((p90 + avg_credit) / avg_credit * 1.1, 2)
        stop_mult_rec = max(1.5, min(stop_mult_rec, 4.0))

    _cost_floor = round(avg_costs_pts * 4.0, 1) if avg_costs_pts > 0 else None
    _profit_floor = round(avg_costs_pts * 8.0, 1) if avg_costs_pts > 0 else None
    min_cred_rec = _profit_floor

    kelly = None
    if avg_win > 0 and avg_loss < 0 and n >= MIN_SAMPLES_SIGNIFICANCE:
        wlr = avg_win / abs(avg_loss)
        kelly = round(max(0, min(win_rate - (1 - win_rate) / wlr, 0.25)), 4)

    size_mult = None
    if n >= MIN_SAMPLES_SIGNIFICANCE:
        if sharpe is not None and sharpe >= 1.5:
            size_mult = round(min(sharpe / 1.5, 2.0), 2)
        elif sharpe is not None and sharpe >= 0.5:
            size_mult = 1.0
        elif sharpe is not None and sharpe >= 0:
            size_mult = 0.75
        elif sharpe is not None:
            size_mult = 0.0

    has_edge = (
        n >= MIN_SAMPLES_SIGNIFICANCE
        and avg_pnl > 0
        and win_rate > 0.45
        and (pf is None or pf > 1.0)
    )

    if n < MIN_SAMPLES_SIGNIFICANCE:
        quality = "INSUFFICIENT"
        sharpe_note = f"need_{MIN_SAMPLES_SIGNIFICANCE}_have_{n}"
        action = "WATCH"
    elif n < MIN_SAMPLES_RELIABLE:
        quality = "INDICATIVE"
        sharpe_note = f"indicative_need_{MIN_SAMPLES_RELIABLE}_for_reliable"
        action = "KEEP" if has_edge else "KILL"
    else:
        quality = "RELIABLE"
        sharpe_note = "reliable"
        action = "KEEP" if has_edge else "KILL"

    return {
        "n_trades": n,
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_pnl_rs": round(avg_pnl, 2),
        "avg_win_rs": round(avg_win, 2),
        "avg_loss_rs": round(avg_loss, 2),
        "profit_factor": round(pf, 3) if pf else None,
        "sharpe": sharpe,
        "sharpe_note": sharpe_note,
        "total_pnl_rs": round(sum(pnls), 2),
        "avg_credit_pts": round(avg_credit, 2),
        "avg_costs_rs": round(avg_costs_rs, 2),
        "avg_costs_pts": round(avg_costs_pts, 3),
        "p75_adverse_pts": round(p75, 2),
        "p90_adverse_pts": round(p90, 2),
        "p95_adverse_pts": round(p95, 2),
        "exit_reason_distribution": exit_dist,
        "has_edge": has_edge,
        "data_quality": quality,
        "recommended_stop_multiplier": stop_mult_rec,
        "recommended_min_credit_pts": min_cred_rec,
        "cost_coverage_floor_pts": _cost_floor,
        "kelly_fraction": kelly,
        "recommended_size_multiplier": size_mult,
        "action": action,
    }


def overall_summary(all_trades, daily_pnls):
    if not all_trades:
        return {"total_trades": 0, "message": "No trades simulated"}
    pnls = [t["net_pnl_rs"] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    daily_returns = list(daily_pnls.values())
    sharpe = None
    if len(daily_returns) >= 5:
        rf = RISK_FREE_ANNUAL / TRADING_DAYS_YEAR
        excess = [r / STARTING_CAPITAL - rf for r in daily_returns]
        if statistics.stdev(excess) > 0:
            sharpe = round(statistics.mean(excess) / statistics.stdev(excess) * math.sqrt(TRADING_DAYS_YEAR), 3)
    equity = []
    capital = STARTING_CAPITAL
    for d in sorted(daily_pnls.keys()):
        capital += daily_pnls[d]
        equity.append({"date": d, "capital": round(capital, 2), "daily_pnl": round(daily_pnls[d], 2)})
    peak = STARTING_CAPITAL
    max_dd = 0.0
    for e in equity:
        if e["capital"] > peak:
            peak = e["capital"]
        dd = peak - e["capital"]
        if dd > max_dd:
            max_dd = dd
    return {
        "total_trades": len(all_trades),
        "total_days": len(daily_pnls),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        "total_pnl_rs": round(sum(pnls), 2),
        "avg_pnl_per_trade_rs": round(statistics.mean(pnls), 2) if pnls else 0,
        "avg_win_rs": round(statistics.mean(wins), 2) if wins else 0,
        "avg_loss_rs": round(statistics.mean(losses), 2) if losses else 0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) != 0 else None,
        "overall_sharpe": sharpe,
        "max_drawdown_rs": round(max_dd, 2),
        "final_capital_rs": round(capital, 2) if equity else STARTING_CAPITAL,
        "total_return_pct": round((capital - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 3) if equity else 0,
        "equity_curve": equity,
    }


def run_backtest(start_date=None, end_date=None, stop_mult=2.5, target_pct=0.50, lots=2, wing_width=None, verbose=True):
    conn = get_conn()
    if start_date is None:
        row = conn.execute("SELECT MIN(trading_date) FROM cycle_log").fetchone()
        start_date = row[0] if row and row[0] else "2026-01-01"
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    if verbose:
        print(f"Backtesting from {start_date} to {end_date}")
        print(f"Parameters: stop={stop_mult}x gross_credit, target={target_pct*100:.0f}%, lots={lots}")
        print()

    trading_dates = q(conn,
        "SELECT DISTINCT trading_date FROM cycle_log WHERE trading_date >= ? AND trading_date <= ? ORDER BY trading_date",
        (start_date, end_date)
    )
    if verbose:
        print(f"Found {len(trading_dates)} trading days")

    all_trades = []
    cell_trades_map = {}
    daily_pnls = {}
    total_sim = 0

    for day_row in trading_dates:
        td = day_row["trading_date"]
        cycles = q(conn, "SELECT * FROM cycle_log WHERE trading_date=? ORDER BY cycle_id", (td,))

        expiry_row = conn.execute(
            "SELECT DISTINCT expiry FROM option_chain_snapshot WHERE trading_date=? ORDER BY expiry ASC LIMIT 1",
            (td,)
        ).fetchone()
        if not expiry_row:
            continue
        expiry = expiry_row[0]

        all_cyc = []
        for cyc in cycles:
            chain = load_chain(conn, cyc.get("cycle_id"), cyc.get("cycle_time"), td, expiry)
            all_cyc.append({
                "cycle": cyc,
                "chain": chain,
                "meta": {
                    "spot": cyc.get("spot"),
                    "vix": cyc.get("vix"),
                    "vrp": cyc.get("vrp"),
                    "adx": cyc.get("adx"),
                    "vwap_dist": cyc.get("vwap_dist_pct"),
                    "trend": cyc.get("trend_condition"),
                    "direction": cyc.get("direction"),
                }
            })

        day_pnl = 0.0
        day_trades = 0
        session_position_open = False
        session_entry_cycle_idx = -1

        for i, cyc_data in enumerate(all_cyc):
            cyc = cyc_data["cycle"]
            chain = cyc_data["chain"]
            if not chain:
                continue

            ct_str = cyc.get("cycle_time", "")
            try:
                ct = datetime.fromisoformat(ct_str)
                if ct.hour < 10 or ct.hour >= 14:
                    continue
            except Exception:
                continue

            if session_position_open:
                continue

            strategy, reason = select_strategy(cyc)
            if strategy == "NO_TRADE":
                continue

            spot = cyc.get("spot")
            if not spot:
                continue

            actual_dte = None
            try:
                exp_d = datetime.strptime(expiry, "%Y-%m-%d").date()
                trd_d = datetime.strptime(td, "%Y-%m-%d").date()
                actual_dte = (exp_d - trd_d).days
            except Exception:
                pass

            eff_wing = wing_width
            if eff_wing is None:
                atm_straddle = cyc.get("atm_straddle_price")
                if atm_straddle and atm_straddle > 20:
                    raw = atm_straddle * 0.85
                    eff_wing = int(round(raw / NIFTY_STRIKE_STEP) * NIFTY_STRIKE_STEP)
                    eff_wing = max(100, min(eff_wing, 400))
                else:
                    eff_wing = 150

            legs = build_legs(strategy, chain, spot, eff_wing, actual_dte)
            if not legs:
                continue

            for leg in legs:
                leg["exec_price"] = exec_price(chain, leg["strike"], leg["option_type"], leg["action"])

            gross_credit = sum(l["exec_price"] for l in legs if l["action"] == "SELL") -                            sum(l["exec_price"] for l in legs if l["action"] == "BUY")
            if gross_credit <= 0:
                continue

            entry_costs_pts = compute_costs(
                [{"exec_price": l["exec_price"], "action": l["action"]} for l in legs], lots
            ) / (LOT_SIZE * lots)

            slip = sum(
                min((chain.get(l["strike"], {}).get(l["option_type"], {}).get("ask", 0) -
                     chain.get(l["strike"], {}).get(l["option_type"], {}).get("bid", 0)) / 2.0, 3.0)
                for l in legs
            )
            net_credit = gross_credit - entry_costs_pts - slip
            if net_credit <= 0:
                continue

            subsequent = [s for s in all_cyc[i+1:] if s["chain"]]
            if not subsequent:
                continue

            result = simulate_trade(
                entry_cycle=cyc,
                subsequent=subsequent,
                strategy=strategy,
                legs_spec=legs,
                gross_credit=gross_credit,
                net_credit=net_credit,
                stop_mult=stop_mult,
                target_pct=target_pct,
                lots=lots,
            )

            cell_key = f"{cyc.get('volatility_condition')}|{cyc.get('trend_condition')}|{cyc.get('direction')}|{strategy}"
            trade_rec = {
                "trading_date": td,
                "cycle_time": ct_str,
                "strategy_name": strategy,
                "selection_reason": reason,
                "cell_key": cell_key,
                "volatility_condition": cyc.get("volatility_condition"),
                "trend_condition": cyc.get("trend_condition"),
                "direction": cyc.get("direction"),
                "vix_regime": cyc.get("vix_regime"),
                "actual_dte": actual_dte,
                "spot": spot,
                "vrp": cyc.get("vrp"),
                "adx": cyc.get("adx"),
                "gross_credit": round(gross_credit, 3),
                "net_credit": round(net_credit, 3),
                "wing_width": eff_wing,
                "lots": lots,
                **result,
            }

            all_trades.append(trade_rec)
            cell_trades_map.setdefault(cell_key, []).append(trade_rec)
            day_pnl += result["net_pnl_rs"]
            day_trades += 1
            total_sim += 1
            session_position_open = True
            session_entry_cycle_idx = i

        if day_trades > 0:
            daily_pnls[td] = day_pnl

    conn.close()

    cs = {k: cell_stats(v) for k, v in cell_trades_map.items()}
    summary = overall_summary(all_trades, daily_pnls)

    if verbose:
        print(f"\nBacktest complete:")
        print(f"  Total simulated trades: {total_sim}")
        print(f"  Unique cells tested: {len(cs)}")

    return {"all_trades": all_trades, "cell_stats": cs, "daily_pnls": daily_pnls, "summary": summary}


def print_summary(s):
    print("\n" + "=" * 60)
    print("OVERALL BACKTEST SUMMARY")
    print("=" * 60)
    for k, v in s.items():
        if k != "equity_curve":
            print(f"  {k:<30}: {v}")
    n = s.get("total_trades", 0)
    if n < MIN_SAMPLES_SIGNIFICANCE:
        print(f"\n  WARNING: Only {n} trades — need {MIN_SAMPLES_SIGNIFICANCE}+ for significance")
        print(f"  Run paper trading for 30-60 more days before trusting these results")
    elif n < MIN_SAMPLES_RELIABLE:
        print(f"\n  NOTE: {n} trades — results are indicative, need {MIN_SAMPLES_RELIABLE}+ for reliable Sharpe")
    else:
        print(f"\n  Data quality: RELIABLE ({n} trades)")


def print_calibration(cs):
    W = 155
    print("\n" + "=" * W)
    print("CALIBRATION TABLE — Per-Cell Analysis")
    print(f"  Min samples for significance: {MIN_SAMPLES_SIGNIFICANCE} | For reliable Sharpe: {MIN_SAMPLES_RELIABLE}")
    print(f"  MinCr(prof) = 8x cost floor (profitability minimum) | CostFlr = 4x cost floor (break-even minimum)")
    print("=" * W)
    hdr = (
        f"{'Cell Key':<55} {'N':>4} {'WR%':>5} {'AvgPnL':>8} {'Sharpe':>7} "
        f"{'PF':>5} {'Stop':>6} {'MinCr(prof)':>11} {'CostFlr':>7} {'Size':>6} "
        f"{'Quality':<12} {'Action':<6} {'Note':<25}"
    )
    print(hdr)
    print("-" * W)

    for ck, st in sorted(cs.items(), key=lambda x: x[1].get("sharpe") or -999, reverse=True):
        n = st.get("n_trades", 0)
        wr = st.get("win_rate_pct", 0)
        ap = st.get("avg_pnl_rs", 0)
        sh = st.get("sharpe")
        pf = st.get("profit_factor")
        sm = st.get("recommended_stop_multiplier")
        mc = st.get("recommended_min_credit_pts")
        cf = st.get("cost_coverage_floor_pts")
        sz = st.get("recommended_size_multiplier")
        ql = st.get("data_quality", "UNKNOWN")
        ac = st.get("action", "WATCH")
        nt = (st.get("sharpe_note") or "")[:25]

        sh_s = f"{sh:.2f}" if sh is not None else "N/A"
        pf_s = f"{pf:.2f}" if pf is not None else "N/A"
        sm_s = f"{sm:.2f}" if sm is not None else "N/A"
        mc_s = f"{mc:.1f}" if mc is not None else "N/A"
        cf_s = f"{cf:.1f}" if cf is not None else "N/A"
        sz_s = f"{sz:.2f}" if sz is not None else "N/A"

        print(
            f"{ck[:55]:<55} {n:>4} {wr:>5.1f} {ap:>8.0f} {sh_s:>7} "
            f"{pf_s:>5} {sm_s:>6} {mc_s:>11} {cf_s:>7} {sz_s:>6} "
            f"{ql:<12} {ac:<6} {nt:<25}"
        )

    print("=" * W)
    keep = [k for k, v in cs.items() if v.get("action") == "KEEP"]
    kill = [k for k, v in cs.items() if v.get("action") == "KILL"]
    watch = [k for k, v in cs.items() if v.get("action") == "WATCH"]
    print(f"\nSummary: {len(keep)} KEEP  {len(kill)} KILL  {len(watch)} WATCH")

    if kill:
        print("\nCells to KILL (negative expectancy with sufficient data):")
        for c in kill:
            print(f"  {c}")
    if keep:
        print("\nTop cells to KEEP (positive expectancy):")
        for c in sorted(keep, key=lambda x: cs[x].get("sharpe") or 0, reverse=True)[:5]:
            st = cs[c]
            print(f"  {c}")
            print(
                f"    Sharpe={st.get('sharpe')} WR={st.get('win_rate_pct')}% "
                f"AvgPnL=Rs{st.get('avg_pnl_rs'):.0f} "
                f"Stop={st.get('recommended_stop_multiplier')} "
                f"MinCr={st.get('recommended_min_credit_pts')} "
                f"CostFlr={st.get('cost_coverage_floor_pts')} "
                f"Size={st.get('recommended_size_multiplier')}"
            )


def save_results(results):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    for name, data in [
        (f"backtest_trades_{ts}.json", results["all_trades"]),
        (f"backtest_cell_stats_{ts}.json", results["cell_stats"]),
    ]:
        p = OUTPUT_DIR / name
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"Saved: {p}")

    summary_out = {k: v for k, v in results["summary"].items() if k != "equity_curve"}
    p = OUTPUT_DIR / f"backtest_summary_{ts}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary_out, f, indent=2, default=str)
    print(f"Saved: {p}")

    if results["cell_stats"]:
        p = OUTPUT_DIR / f"calibration_table_{ts}.csv"
        fields = ["cell_key","n_trades","win_rate_pct","avg_pnl_rs","total_pnl_rs",
                  "avg_win_rs","avg_loss_rs","profit_factor","sharpe","sharpe_note",
                  "avg_credit_pts","avg_costs_rs","avg_costs_pts",
                  "p75_adverse_pts","p90_adverse_pts","p95_adverse_pts",
                  "recommended_stop_multiplier","recommended_min_credit_pts",
                  "cost_coverage_floor_pts",
                  "kelly_fraction","recommended_size_multiplier",
                  "data_quality","action","has_edge"]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for ck, st in sorted(results["cell_stats"].items(), key=lambda x: x[1].get("sharpe") or -999, reverse=True):
                row = {"cell_key": ck}
                row.update(st)
                row.pop("exit_reason_distribution", None)
                w.writerow(row)
        print(f"Saved: {p}")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    start_date = end_date = None
    stop_mult = 2.5
    target_pct = 0.50
    lots = 2

    for i, a in enumerate(args):
        if a == "--start" and i+1 < len(args):
            start_date = args[i+1]
        elif a == "--end" and i+1 < len(args):
            end_date = args[i+1]
        elif a == "--stop-mult" and i+1 < len(args):
            stop_mult = float(args[i+1])
        elif a == "--target-pct" and i+1 < len(args):
            target_pct = float(args[i+1])
        elif a == "--lots" and i+1 < len(args):
            lots = int(args[i+1])

    print("NIFTY Options Intraday Algo — Backtesting Module")
    print("=" * 60)

    results = run_backtest(
        start_date=start_date,
        end_date=end_date,
        stop_mult=stop_mult,
        target_pct=target_pct,
        lots=lots,
        verbose=True,
    )

    print_summary(results["summary"])
    print_calibration(results["cell_stats"])
    save_results(results)

    print("\nUsage:")
    print("  python backtest.py")
    print("  python backtest.py --start 2026-09-01 --end 2026-09-30")
    print("  python backtest.py --stop-mult 2.0 --target-pct 0.45 --lots 1")
