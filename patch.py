import ast
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def read_file(p):
    return p.read_text(encoding="utf-8")


def write_file(p, content):
    p.write_text(content, encoding="utf-8")


def verify_syntax(path):
    src = read_file(path)
    try:
        ast.parse(src)
        print(f"  SYNTAX OK: {path.name}")
        return True
    except SyntaxError as e:
        print(f"  SYNTAX ERROR in {path.name}: line {e.lineno}: {e.msg}")
        lines = src.split("\n")
        start = max(0, e.lineno - 5)
        end = min(len(lines), e.lineno + 5)
        for i, line in enumerate(lines[start:end], start=start + 1):
            marker = ">>>" if i == e.lineno else "   "
            print(f"    {marker} {i:4d}: {repr(line)}")
        return False


def rewrite_backtest():
    path = BASE_DIR / "backtest.py"
    content = '''import json
import sqlite3
import statistics
from pathlib import Path
from datetime import datetime

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
        env[k.strip()] = v.strip().strip(\'"\').strip("\'")
    return env


_ENV = load_env_simple(BASE_DIR / "env.txt")
DB_PATH = Path(_ENV.get("DB_PATH", "data/nifty_algo.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH

LOT_SIZE         = int(_ENV.get("NIFTY_LOT_SIZE", "65") or 65)
STARTING_CAPITAL = float(_ENV.get("STARTING_CAPITAL", "1000000") or 1000000)


def get_connection():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name):
    row = conn.execute("SELECT name FROM sqlite_master WHERE type=\'table\' AND name=?", (name,)).fetchone()
    return row is not None


def q(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def replay_day(conn, trading_date):
    cycle_rows    = q(conn, "SELECT * FROM cycle_log WHERE trading_date=? ORDER BY cycle_id", (trading_date,))
    trade_entries = q(conn, "SELECT * FROM trade_entries WHERE trading_date=? ORDER BY entry_time", (trading_date,))
    trade_exits   = q(conn, "SELECT te.* FROM trade_exits te JOIN positions p ON te.position_id=p.position_id WHERE p.trading_date=? ORDER BY te.exit_time", (trading_date,)) if table_exists(conn, "trade_exits") else []
    regime_decs   = q(conn, "SELECT * FROM regime_decisions WHERE date=? ORDER BY timestamp", (trading_date,)) if table_exists(conn, "regime_decisions") else []
    vix_rows      = q(conn, "SELECT * FROM vix_history WHERE date=? ORDER BY timestamp", (trading_date,)) if table_exists(conn, "vix_history") else []
    mkt_snaps     = q(conn, "SELECT * FROM market_snapshots WHERE date=? ORDER BY timestamp", (trading_date,)) if table_exists(conn, "market_snapshots") else []

    exits_by_pos = {e["position_id"]: e for e in trade_exits}

    vrp_vals = [c["vrp"] for c in cycle_rows if c.get("vrp") is not None]
    vix_vals = [r["vix_value"] for r in vix_rows if r.get("vix_value")]
    or_cond  = next((c["or_condition"] for c in cycle_rows if c.get("or_condition")), "UNKNOWN")

    day = {
        "trading_date": trading_date,
        "cycles": len(cycle_rows),
        "regime_decisions": len(regime_decs),
        "trades_entered": len(trade_entries),
        "trades_closed": len(trade_exits),
        "wins": 0, "losses": 0, "breakevens": 0,
        "gross_pnl_rupees": 0.0,
        "total_costs_rupees": 0.0,
        "net_pnl_rupees": 0.0,
        "net_pnl_pct": 0.0,
        "avg_hold_minutes": 0.0,
        "stops_fired": 0,
        "target_exits": 0,
        "time_exits": 0,
        "iv_crush_trades": 0,
        "strategies_used": {},
        "regime_performance": {},
        "vrp_condition_performance": {},
        "vix_open": vix_vals[0] if vix_vals else None,
        "vix_close": vix_vals[-1] if vix_vals else None,
        "vrp_mean": round(statistics.mean(vrp_vals), 3) if vrp_vals else None,
        "or_condition": or_cond,
        "trade_details": [],
    }

    hold_mins = []
    entry_vrps = []
    day_iv_crush_by_strategy = {}

    for t in trade_entries:
        pid      = t.get("position_id")
        ex       = exits_by_pos.get(pid)
        strat    = str(t.get("strategy_name") or "UNKNOWN")
        regime   = str(t.get("final_regime_at_entry") or "UNKNOWN")
        vol_cond = str(t.get("volatility_condition") or "UNKNOWN")
        entry_vrp = t.get("entry_vrp")

        if strat not in day["strategies_used"]:
            day["strategies_used"][strat] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        day["strategies_used"][strat]["count"] += 1

        if regime not in day["regime_performance"]:
            day["regime_performance"][regime] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        day["regime_performance"][regime]["count"] += 1

        if vol_cond not in day["vrp_condition_performance"]:
            day["vrp_condition_performance"][vol_cond] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        day["vrp_condition_performance"][vol_cond]["count"] += 1

        if entry_vrp:
            entry_vrps.append(entry_vrp)

        detail = {
            "position_id": pid,
            "strategy_name": strat,
            "entry_time": t.get("entry_time"),
            "entry_spot": t.get("entry_spot"),
            "entry_vix": t.get("entry_vix"),
            "entry_vrp": entry_vrp,
            "entry_credit": t.get("entry_credit") or t.get("entry_debit"),
            "final_lots": t.get("final_lots"),
            "actual_dte": t.get("actual_dte"),
            "final_regime": regime,
            "confidence": t.get("confidence_at_entry"),
            "volatility_condition": vol_cond,
            "trend_condition": t.get("trend_condition"),
            "direction": t.get("direction"),
            "exit_time": None, "exit_reason": None, "hold_minutes": None,
            "gross_pnl_rupees": None, "total_costs_rupees": None,
            "net_pnl_rupees": None, "result": None,
            "calibration_tier_at_trade": None,
        }

        if ex:
            net_pnl   = ex.get("net_pnl_rupees", 0) or 0
            gross_pnl = ex.get("gross_pnl_rupees", 0) or 0
            tot_costs = ex.get("total_costs_rupees", 0) or 0
            result    = ex.get("result", "UNKNOWN")
            hold_min  = ex.get("hold_minutes", 0) or 0
            exit_rsn  = ex.get("exit_reason", "")

            day["gross_pnl_rupees"]   += gross_pnl
            day["total_costs_rupees"] += tot_costs
            day["net_pnl_rupees"]     += net_pnl

            if result == "WIN":
                day["wins"] += 1
                day["strategies_used"][strat]["wins"] += 1
                day["regime_performance"][regime]["wins"] += 1
                day["vrp_condition_performance"][vol_cond]["wins"] += 1
            elif result == "LOSS":
                day["losses"] += 1
                day["strategies_used"][strat]["losses"] += 1
                day["regime_performance"][regime]["losses"] += 1
                day["vrp_condition_performance"][vol_cond]["losses"] += 1
            else:
                day["breakevens"] += 1

            day["strategies_used"][strat]["net_pnl"]             += net_pnl
            day["regime_performance"][regime]["net_pnl"]          += net_pnl
            day["vrp_condition_performance"][vol_cond]["net_pnl"] += net_pnl

            if exit_rsn == "CLOSE_STOP":
                day["stops_fired"] += 1
            elif exit_rsn == "CLOSE_TARGET":
                day["target_exits"] += 1
            elif exit_rsn in ("CLOSE_TIME", "HARD_EXIT_15:00", "EOD_CLOSE"):
                day["time_exits"] += 1

            hold_mins.append(hold_min)

            entry_iv = t.get("entry_atm_iv")
            if entry_iv and entry_iv > 0:
                exit_cycles = [c for c in cycle_rows if str(c.get("cycle_time", "")) >= str(ex.get("exit_time", ""))]
                if exit_cycles:
                    exit_iv_pct = exit_cycles[0].get("atm_iv_pct")
                    if exit_iv_pct and (exit_iv_pct / 100.0) < entry_iv:
                        day["iv_crush_trades"] += 1
                        if strat not in day_iv_crush_by_strategy:
                            day_iv_crush_by_strategy[strat] = {"crush_count": 0, "total": 0}
                        day_iv_crush_by_strategy[strat]["crush_count"] += 1

            if strat not in day_iv_crush_by_strategy:
                day_iv_crush_by_strategy[strat] = {"crush_count": 0, "total": 0}
            day_iv_crush_by_strategy[strat]["total"] += 1

            entry_credit = t.get("entry_credit") or t.get("entry_debit") or 0
            net_pnl_pts  = ex.get("net_pnl_pts", 0) or 0
            _cal_rows = q(conn, "SELECT calibration_tier FROM calibration_state WHERE calibrated_at <= ? ORDER BY calibrated_at DESC LIMIT 1", (t.get("entry_time", "9999"),))
            _cal_tier = _cal_rows[0]["calibration_tier"] if _cal_rows else 0
            detail["calibration_tier_at_trade"] = _cal_tier
            detail.update({
                "exit_time": ex.get("exit_time"),
                "exit_reason": exit_rsn,
                "hold_minutes": hold_min,
                "gross_pnl_rupees": gross_pnl,
                "total_costs_rupees": tot_costs,
                "net_pnl_rupees": net_pnl,
                "net_pnl_pct_credit": round(net_pnl_pts / entry_credit * 100, 1) if entry_credit > 0 else None,
                "result": result,
            })

        day["trade_details"].append(detail)

    if hold_mins:
        day["avg_hold_minutes"] = round(statistics.mean(hold_mins), 1)
    if entry_vrps:
        day["avg_entry_vrp"] = round(statistics.mean(entry_vrps), 3)

    total_t = day["wins"] + day["losses"] + day["breakevens"]
    day["win_rate_pct"] = round(day["wins"] / total_t * 100, 1) if total_t else 0.0
    day["net_pnl_pct"]  = round(day["net_pnl_rupees"] / STARTING_CAPITAL * 100, 3)

    gw = sum(e.get("net_pnl_rupees", 0) or 0 for e in trade_exits if (e.get("net_pnl_rupees") or 0) > 0)
    gl = abs(sum(e.get("net_pnl_rupees", 0) or 0 for e in trade_exits if (e.get("net_pnl_rupees") or 0) < 0))
    day["profit_factor"] = round(gw / gl, 3) if gl > 0 else None

    if day["gross_pnl_rupees"] > 0:
        day["cost_efficiency_pct"] = round(day["total_costs_rupees"] / day["gross_pnl_rupees"] * 100, 1)
    else:
        day["cost_efficiency_pct"] = None

    return day, day_iv_crush_by_strategy


def run_walkforward_backtest(from_date=None, to_date=None):
    conn = get_connection()

    if not table_exists(conn, "trade_entries"):
        print("No trade_entries table. Run engine first.")
        conn.close()
        return None

    all_dates = [r["trading_date"] for r in q(conn, "SELECT DISTINCT trading_date FROM trade_entries ORDER BY trading_date")]
    if from_date:
        all_dates = [d for d in all_dates if d >= from_date]
    if to_date:
        all_dates = [d for d in all_dates if d <= to_date]

    if not all_dates:
        print("No trading dates found.")
        conn.close()
        return None

    print(f"Walk-forward backtest: {len(all_dates)} trading days ({all_dates[0]} to {all_dates[-1]})")
    print()

    all_days = []
    capital = STARTING_CAPITAL
    peak_capital = STARTING_CAPITAL
    max_drawdown = 0.0
    cum_pnl = 0.0
    consec_losses = 0
    max_consec_losses = 0
    iv_crush_by_strategy = {}

    for d in all_dates:
        day, day_iv_crush = replay_day(conn, d)
        day["capital_start"] = capital
        capital += day["net_pnl_rupees"]
        day["capital_end"] = capital
        cum_pnl += day["net_pnl_rupees"]
        day["cumulative_pnl"] = round(cum_pnl, 2)

        if capital > peak_capital:
            peak_capital = capital
        dd = peak_capital - capital
        if dd > max_drawdown:
            max_drawdown = dd

        if day["losses"] > 0 and day["wins"] == 0:
            consec_losses += 1
            max_consec_losses = max(max_consec_losses, consec_losses)
        else:
            consec_losses = 0

        for strat, vals in day_iv_crush.items():
            if strat not in iv_crush_by_strategy:
                iv_crush_by_strategy[strat] = {"crush_count": 0, "total": 0}
            iv_crush_by_strategy[strat]["crush_count"] += vals.get("crush_count", 0)
            iv_crush_by_strategy[strat]["total"]       += vals.get("total", 0)

        all_days.append(day)
        status = "WIN" if day["net_pnl_rupees"] > 0 else ("LOSS" if day["net_pnl_rupees"] < 0 else "FLAT")
        _or_str  = str(day.get("or_condition") or "?")[:12]
        _vix_str = str(day.get("vix_open") or "?")[:5]
        _vrp_str = str(day.get("vrp_mean") or "?")[:6]
        print(f"  {d} [{_or_str:<12}] VIX={_vix_str:<5} "
              f"VRP={_vrp_str:<6} "
              f"T={day[\'trades_entered\']:2d} "
              f"P&L=Rs{day[\'net_pnl_rupees\']:8.0f} [{status}] "
              f"Cap=Rs{capital:,.0f}")

    conn.close()

    total_days   = len(all_days)
    prof_days    = sum(1 for d in all_days if d["net_pnl_rupees"] > 0)
    loss_days    = sum(1 for d in all_days if d["net_pnl_rupees"] < 0)
    total_trades = sum(d["trades_entered"] for d in all_days)
    total_wins   = sum(d["wins"] for d in all_days)
    total_losses = sum(d["losses"] for d in all_days)
    total_gross  = sum(d["gross_pnl_rupees"] for d in all_days)
    total_costs  = sum(d["total_costs_rupees"] for d in all_days)
    total_net    = sum(d["net_pnl_rupees"] for d in all_days)
    total_stops  = sum(d["stops_fired"] for d in all_days)
    total_tgts   = sum(d["target_exits"] for d in all_days)
    total_time   = sum(d["time_exits"] for d in all_days)
    total_iv_crush = sum(d["iv_crush_trades"] for d in all_days)

    gw_total = sum(d["net_pnl_rupees"] for d in all_days if d["net_pnl_rupees"] > 0)
    gl_total = abs(sum(d["net_pnl_rupees"] for d in all_days if d["net_pnl_rupees"] < 0))
    overall_pf = round(gw_total / gl_total, 3) if gl_total > 0 else None
    trade_wr   = round(total_wins / (total_wins + total_losses) * 100, 1) if (total_wins + total_losses) > 0 else 0

    regime_agg   = {}
    strategy_agg = {}
    vrp_agg      = {}

    for day in all_days:
        for k, v in day.get("regime_performance", {}).items():
            if k not in regime_agg:
                regime_agg[k] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
            for f in ["count", "wins", "losses"]:
                regime_agg[k][f] += v.get(f, 0)
            regime_agg[k]["net_pnl"] += v.get("net_pnl", 0.0)
        for k, v in day.get("strategies_used", {}).items():
            if k not in strategy_agg:
                strategy_agg[k] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
            for f in ["count", "wins", "losses"]:
                strategy_agg[k][f] += v.get(f, 0)
            strategy_agg[k]["net_pnl"] += v.get("net_pnl", 0.0)
        for k, v in day.get("vrp_condition_performance", {}).items():
            if k not in vrp_agg:
                vrp_agg[k] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
            for f in ["count", "wins", "losses"]:
                vrp_agg[k][f] += v.get(f, 0)
            vrp_agg[k]["net_pnl"] += v.get("net_pnl", 0.0)

    def add_win_rate(d):
        result_d = {}
        for k, v in sorted(d.items(), key=lambda x: -x[1]["net_pnl"]):
            enriched = dict(v)
            enriched["win_rate_pct"] = round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0
            enriched["avg_pnl"] = round(v["net_pnl"] / v["count"], 2) if v["count"] else 0
            result_d[k] = enriched
        return result_d

    summary = {
        "backtest_metadata": {
            "generated_at": datetime.now().isoformat(),
            "from_date": all_dates[0],
            "to_date": all_dates[-1],
            "total_trading_days": total_days,
            "starting_capital": STARTING_CAPITAL,
            "lot_size": LOT_SIZE,
            "engine_version": "v2.0_patched",
        },
        "overall_performance": {
            "profitable_days": prof_days,
            "loss_days": loss_days,
            "flat_days": total_days - prof_days - loss_days,
            "day_win_rate_pct": round(prof_days / total_days * 100, 1) if total_days else 0,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "trade_win_rate_pct": trade_wr,
            "total_gross_pnl_rupees": round(total_gross, 2),
            "total_costs_rupees": round(total_costs, 2),
            "total_net_pnl_rupees": round(total_net, 2),
            "total_return_pct": round(total_net / STARTING_CAPITAL * 100, 3),
            "profit_factor": overall_pf,
            "max_drawdown_rupees": round(max_drawdown, 2),
            "max_drawdown_pct": round(max_drawdown / STARTING_CAPITAL * 100, 3),
            "max_consecutive_losses": max_consec_losses,
            "capital_final": round(capital, 2),
            "total_stops_fired": total_stops,
            "total_target_exits": total_tgts,
            "total_time_exits": total_time,
            "iv_crush_trades": total_iv_crush,
            "iv_crush_rate_pct": round(total_iv_crush / total_trades * 100, 1) if total_trades else 0,
            "cost_as_pct_gross": round(total_costs / total_gross * 100, 1) if total_gross > 0 else None,
            "avg_daily_pnl_rupees": round(total_net / total_days, 2) if total_days else 0,
            "avg_trades_per_day": round(total_trades / total_days, 2) if total_days else 0,
            "stop_rate_pct": round(total_stops / total_trades * 100, 1) if total_trades else 0,
            "target_rate_pct": round(total_tgts / total_trades * 100, 1) if total_trades else 0,
        },
        "regime_performance": add_win_rate(regime_agg),
        "strategy_performance": add_win_rate(strategy_agg),
        "vrp_condition_performance": add_win_rate(vrp_agg),
        "iv_crush_by_strategy": {
            k: {
                "crush_count": v.get("crush_count", 0),
                "total": v.get("total", 0),
                "crush_rate_pct": round(v.get("crush_count", 0) / v.get("total", 1) * 100, 1) if v.get("total", 0) > 0 else 0,
            }
            for k, v in iv_crush_by_strategy.items()
        },
        "daily_results": all_days,
        "equity_curve": [
            {"date": d["trading_date"], "capital": d["capital_end"], "pnl": d["net_pnl_rupees"], "cumulative_pnl": d["cumulative_pnl"]}
            for d in all_days
        ],
        "llm_analysis_context": {
            "summary": f"Walk-forward backtest of NIFTY intraday options engine v2.0 over {total_days} trading days.",
            "key_findings": {
                "best_regime": max(regime_agg, key=lambda k: regime_agg[k]["net_pnl"]) if regime_agg else None,
                "worst_regime": min(regime_agg, key=lambda k: regime_agg[k]["net_pnl"]) if regime_agg else None,
                "best_strategy": max(strategy_agg, key=lambda k: strategy_agg[k]["net_pnl"]) if strategy_agg else None,
                "best_vrp_condition": max(vrp_agg, key=lambda k: vrp_agg[k].get("net_pnl", 0)) if vrp_agg else None,
                "stop_rate_pct": round(total_stops / total_trades * 100, 1) if total_trades else 0,
                "target_rate_pct": round(total_tgts / total_trades * 100, 1) if total_trades else 0,
                "cost_drag_pct": round(total_costs / STARTING_CAPITAL * 100, 3),
                "iv_crush_rate_pct": round(total_iv_crush / total_trades * 100, 1) if total_trades else 0,
            },
            "questions_for_llm": [
                "Which regime produced the best risk-adjusted returns?",
                "Is the profit factor sustainable given the sample size?",
                "Are transaction costs proportionate to gross P&L?",
                "Which VRP condition had the best win rate?",
                "What is the optimal stop loss multiplier based on actual stop outcomes?",
                "Are there patterns in losing days (VIX level, OR condition, day of week)?",
                "Does IV crush consistently occur in winning trades?",
                "What is the optimal entry window based on actual trade timing data?",
                "Is the Tuesday 0DTE entry window (11:00-12:30) producing better results?",
                "Are calibrated day sizes improving performance vs hardcoded sizes?",
            ],
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"backtest_{all_dates[0]}_to_{all_dates[-1]}.json"
    report_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print()
    print("=" * 65)
    print("WALK-FORWARD BACKTEST RESULTS")
    print("=" * 65)
    print(f"  Period    : {all_dates[0]} to {all_dates[-1]} ({total_days} days)")
    print(f"  Trades    : {total_trades} | Win Rate: {trade_wr}% | Day Win: {round(prof_days/total_days*100,1) if total_days else 0}%")
    print(f"  Net P&L   : Rs{total_net:,.2f} ({total_net/STARTING_CAPITAL*100:.3f}%)")
    print(f"  Profit Factor: {overall_pf}")
    print(f"  Max Drawdown : Rs{max_drawdown:,.2f} ({max_drawdown/STARTING_CAPITAL*100:.3f}%)")
    print(f"  Total Costs  : Rs{total_costs:,.2f}")
    print(f"  Stops/Targets/Time: {total_stops}/{total_tgts}/{total_time}")
    print(f"  IV Crush Trades: {total_iv_crush}/{total_trades} ({round(total_iv_crush/total_trades*100,1) if total_trades else 0}%)")
    print()
    if regime_agg:
        print("  Regime Performance:")
        for regime, perf in sorted(regime_agg.items(), key=lambda x: -x[1]["net_pnl"]):
            wr = round(perf["wins"] / perf["count"] * 100, 1) if perf["count"] else 0
            regime_str = str(regime) if regime is not None else "UNKNOWN"
            print(f"    {regime_str:<30} n={perf[\'count\']:3d} wr={wr:5.1f}% pnl=Rs{perf[\'net_pnl\']:8.0f}")
    print()
    if strategy_agg:
        print("  Strategy Performance:")
        for strat, perf in sorted(strategy_agg.items(), key=lambda x: -x[1]["net_pnl"]):
            wr = round(perf["wins"] / perf["count"] * 100, 1) if perf["count"] else 0
            strat_str = str(strat) if strat is not None else "UNKNOWN"
            print(f"    {strat_str:<25} n={perf[\'count\']:3d} wr={wr:5.1f}% pnl=Rs{perf[\'net_pnl\']:8.0f}")
    print()
    if iv_crush_by_strategy:
        print("  IV Crush by Strategy:")
        for strat, vals in sorted(iv_crush_by_strategy.items()):
            rate = round(vals.get("crush_count", 0) / vals.get("total", 1) * 100, 1) if vals.get("total", 0) > 0 else 0
            print(f"    {strat:<25} crush={vals.get(\'crush_count\',0)}/{vals.get(\'total\',0)} ({rate}%)")
    print()
    print(f"  Full report: {report_path}")

    return summary


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    from_d = to_d = None
    for i, a in enumerate(args):
        if a == "--from" and i + 1 < len(args):
            from_d = args[i + 1]
        if a == "--to" and i + 1 < len(args):
            to_d = args[i + 1]
    run_walkforward_backtest(from_date=from_d, to_date=to_d)
'''
    write_file(path, content)
    print("  WRITTEN: backtest.py (complete rewrite with correct scope)")


def main():
    bt_path = BASE_DIR / "backtest.py"
    if not bt_path.exists():
        print("MISSING: backtest.py")
        sys.exit(1)

    print("Rewriting backtest.py with correct variable scope...")
    rewrite_backtest()
    print()

    print("Verifying syntax...")
    ok = verify_syntax(bt_path)
    print()

    if ok:
        print("backtest.py rewritten and verified.")
        print()
        print("Key fix: iv_crush_by_strategy moved to replay_day scope")
        print("  replay_day now returns (day, day_iv_crush_by_strategy)")
        print("  run_walkforward_backtest aggregates iv_crush across all days")
        print("  No more NameError: iv_crush_by_strategy not defined")
        print()
        print("Run: python backtest.py")
    else:
        print("Syntax error in backtest.py")
        sys.exit(1)


if __name__ == "__main__":
    main()