"""
════════════════════════════════════════════════════════════════════════════
NIFTY INTRADAY OPTIONS ALGO — EOD / FORENSIC REPORT GENERATOR
════════════════════════════════════════════════════════════════════════════

Save as: eod_report_generator.py  (same directory as the 5 engine files)

PURPOSE:
    Generates a comprehensive, self-describing forensic report for ONE
    trading day, pulling from every SQLite table AND the file-based audit
    log, cross-referenced into a single unified timeline. Designed so that
    an AI/LLM given ONLY this report (no other context) can understand what
    the engine did, why, and where the data quality/behavioral issues are —
    for the purpose of debugging, improving, and optimizing the system.

STANDALONE: does not import any of the 5 engine files. Only reads the
SQLite DB (read-only where supported) and log files. Safe to run at any
time of day, including while the live engine is running.

USAGE:
    1. Edit TARGET_DATE below to the date you want to analyze.
    2. Run: python eod_report_generator.py
    3. Output:
         reports/eod_report_<date>.md              <- main report (read this)
         reports/eod_report_<date>_raw/*.json       <- full untruncated data
"""

import glob
import json
import statistics
import sqlite3
from pathlib import Path
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION — EDIT THIS
# ═══════════════════════════════════════════════════════════════════════
TARGET_DATE = "2026-01-15"     # <-- CHANGE THIS. Format must be YYYY-MM-DD.
# ═══════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "reports"


def load_env_simple(path: Path) -> dict:
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

LOT_SIZE = int(_ENV.get("NIFTY_LOT_SIZE", "65") or 65)
STT_RATE = float(_ENV.get("STT_RATE", "0.0015") or 0.0015)
EXCHANGE_TXN_RATE = float(_ENV.get("EXCHANGE_TXN_RATE", "0.0003552") or 0.0003552)
BROKERAGE_PER_ORDER = float(_ENV.get("BROKERAGE_PER_ORDER", "20.0") or 20.0)


# ═══════════════════════════════════════════════════════════════════════
# DB ACCESS
# ═══════════════════════════════════════════════════════════════════════
def get_connection(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}. Check DB_PATH in env.txt.")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


# ═══════════════════════════════════════════════════════════════════════
# PER-TABLE FETCHERS (handles tables that lack a direct trading_date column)
# ═══════════════════════════════════════════════════════════════════════
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
    return q(conn, f"SELECT * FROM position_legs WHERE position_id IN ({placeholders}) ORDER BY position_id, leg_id",
              tuple(position_ids))


def fetch_trade_entries(conn, d):
    if not table_exists(conn, "trade_entries"):
        return []
    return q(conn, "SELECT * FROM trade_entries WHERE trading_date=? ORDER BY entry_time", (d,))


def fetch_trade_exits(conn, d):
    # trade_exits has no trading_date column directly -- filter on exit_time prefix.
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
    return q(conn, "SELECT * FROM option_chain_snapshot WHERE trading_date=? "
                    "ORDER BY capture_time, strike, option_type", (d,))


def fetch_api_call_log(conn, d):
    if not table_exists(conn, "api_call_log"):
        return []
    return q(conn, "SELECT * FROM api_call_log WHERE call_time LIKE ? ORDER BY call_time", (f"{d}%",))


def fetch_audit_log_db(conn, d):
    if not table_exists(conn, "audit_log"):
        return []
    return q(conn, "SELECT * FROM audit_log WHERE log_time LIKE ? ORDER BY log_time", (f"{d}%",))


def fetch_audit_log_file_lines(log_dir: Path, d: str) -> list:
    """
    Scans ALL nifty_algo_audit.log* files (main + all rotated backups) and
    keeps only lines whose timestamp prefix matches the target date. This
    avoids depending on exact TimedRotatingFileHandler filename conventions.
    """
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


def filter_log_lines_by_level(lines: list, levels: set) -> list:
    out = []
    for line in lines:
        parts = line.split("|")
        if len(parts) >= 2 and parts[1].strip() in levels:
            out.append(line)
    return out


# ═══════════════════════════════════════════════════════════════════════
# DERIVED ANALYTICS
# ═══════════════════════════════════════════════════════════════════════
def detect_cycle_gaps(cycle_rows: list, threshold_minutes: float = 10.0) -> list:
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


def aggregate_no_trade_reasons(decisions: list) -> dict:
    counts = {}
    for d in decisions:
        if d.get("action") == "NO_TRADE":
            r = d.get("reason") or "unknown"
            counts[r] = counts.get(r, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def aggregate_strategies(trade_entries: list) -> dict:
    counts = {}
    for t in trade_entries:
        counts[t["strategy_name"]] = counts.get(t["strategy_name"], 0) + 1
    return counts


def recompute_cost_breakdown(sell_pts, buy_pts, num_orders, lots, lot_size, stt_rate, exch_rate, brokerage) -> dict:
    turnover = sell_pts + buy_pts
    stt = sell_pts * lot_size * lots * stt_rate
    exchange = turnover * lot_size * lots * exch_rate
    brokerage_total = brokerage * num_orders
    sebi = turnover * lot_size * lots * 0.000001
    stamp = buy_pts * lot_size * lots * 0.00003
    gst = (brokerage_total + exchange + sebi) * 0.18
    total = stt + exchange + brokerage_total + sebi + stamp + gst
    return {"stt": round(stt, 2), "exchange": round(exchange, 2), "brokerage": round(brokerage_total, 2),
            "sebi": round(sebi, 4), "stamp": round(stamp, 4), "gst": round(gst, 2), "total": round(total, 2)}


def analyze_trade_costs(trade_entries: list, trade_exits: list) -> list:
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
        breakdown = recompute_cost_breakdown(sell_pts, buy_pts, len(legs), lots, LOT_SIZE,
                                              STT_RATE, EXCHANGE_TXN_RATE, BROKERAGE_PER_ORDER)
        exit_row = exits_by_pos.get(t["position_id"])
        results.append({
            "position_id": t["position_id"], "strategy": t["strategy_name"],
            "recomputed_entry_costs": breakdown, "recorded_entry_costs_rupees": t.get("entry_costs_rupees"),
            "recorded_exit_costs_rupees": exit_row.get("exit_costs_rupees") if exit_row else None,
            "net_pnl_rupees": exit_row.get("net_pnl_rupees") if exit_row else None,
            "result": exit_row.get("result") if exit_row else "STILL_OPEN_OR_MISSING_EXIT",
        })
    return results


def summarize_option_chain(chain_rows: list) -> dict:
    if not chain_rows:
        return {"total_rows": 0}
    capture_times = sorted(set(r["capture_time"] for r in chain_rows if r.get("capture_time")))
    strikes = sorted(set(r["strike"] for r in chain_rows if r.get("strike") is not None))
    zero_bid_ask = sum(1 for r in chain_rows if (r.get("bid") or 0) == 0 and (r.get("ask") or 0) == 0)
    spreads = [(r["ask"] - r["bid"]) for r in chain_rows if (r.get("bid") or 0) > 0 and (r.get("ask") or 0) > 0]
    return {
        "total_rows": len(chain_rows), "unique_capture_times": len(capture_times),
        "unique_strikes": len(strikes),
        "strike_range": [min(strikes), max(strikes)] if strikes else None,
        "zero_bid_ask_count": zero_bid_ask,
        "zero_bid_ask_pct": round(zero_bid_ask / len(chain_rows) * 100, 1) if chain_rows else 0,
        "avg_spread": round(statistics.mean(spreads), 3) if spreads else None,
        "max_spread": round(max(spreads), 3) if spreads else None,
        "first_capture": capture_times[0] if capture_times else None,
        "last_capture": capture_times[-1] if capture_times else None,
    }


def summarize_api_calls(api_rows: list) -> dict:
    if not api_rows:
        return {"total_calls": 0}
    by_category = {}
    errors, response_times = [], []
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
        "total_calls": len(api_rows), "by_category": by_category,
        "error_count": len(errors), "rate_limited_count": rate_limited,
        "avg_response_ms": round(statistics.mean(response_times), 1) if response_times else None,
        "max_response_ms": round(max(response_times), 1) if response_times else None,
        "errors_sample": errors[:20],
    }


def detect_anomalies(session_state, cycle_rows, api_summary, gaps, daily_summary, decisions) -> list:
    flags = []
    if session_state:
        if session_state.get("daily_halted"):
            flags.append(f"[FLAG] Daily trading halted. last_stop_reason="
                          f"{session_state.get('last_stop_reason')}")
        if session_state.get("circuit_breaker_suspected"):
            flags.append("[FLAG] Circuit breaker was suspected at some point today.")
        if session_state.get("vix_spike_detected"):
            flags.append("[FLAG] VIX spike was detected at some point today.")
        if not session_state.get("or_computed"):
            flags.append("[FLAG] Opening range was NEVER computed today — check early-session data availability.")

    if cycle_rows:
        missing_vrp = sum(1 for c in cycle_rows if c.get("vrp") is None)
        if missing_vrp > len(cycle_rows) * 0.3:
            flags.append(f"[FLAG] {missing_vrp}/{len(cycle_rows)} cycles ({missing_vrp/len(cycle_rows)*100:.0f}%) "
                          f"had missing VRP data.")
        missing_spot = sum(1 for c in cycle_rows if c.get("spot") is None)
        if missing_spot > 0:
            flags.append(f"[FLAG] {missing_spot} cycles had missing spot price.")
        unknown_vol = sum(1 for c in cycle_rows if c.get("volatility_condition") == "UNKNOWN")
        if unknown_vol > 3:
            flags.append(f"[FLAG] {unknown_vol} cycles had volatility_condition=UNKNOWN.")

    if api_summary.get("error_count", 0) > 5:
        flags.append(f"[FLAG] {api_summary['error_count']} API errors occurred today.")
    if api_summary.get("rate_limited_count", 0) > 0:
        flags.append(f"[FLAG] {api_summary['rate_limited_count']} API calls were rate-limited.")
    if gaps:
        largest = max(g[2] for g in gaps)
        flags.append(f"[FLAG] {len(gaps)} timing gap(s) in cycle_log detected "
                      f"(possible downtime/restart) — largest gap {largest:.0f} min.")
    if daily_summary and (daily_summary.get("stops_fired") or 0) >= 3:
        flags.append(f"[FLAG] {daily_summary['stops_fired']} stop-losses fired today.")

    no_trade_reasons = aggregate_no_trade_reasons(decisions)
    if no_trade_reasons:
        top_reason, top_count = next(iter(no_trade_reasons.items()))
        total_decisions = len(decisions)
        if total_decisions and top_count > total_decisions * 0.5:
            flags.append(f"[FLAG] A single no-trade reason dominates the day: "
                          f"'{top_reason}' fired {top_count}/{total_decisions} times "
                          f"({top_count/total_decisions*100:.0f}%) — worth reviewing this gate's calibration.")

    if not flags:
        flags.append("[OK] No major anomalies auto-detected.")
    return flags


def build_master_timeline(cycle_rows, decisions, trade_entries, trade_exits, audit_lines) -> list:
    events = []
    for c in cycle_rows:
        events.append((c.get("cycle_time") or "", "CYCLE",
                        f"spot={c.get('spot')} vix={c.get('vix')} vrp={c.get('vrp')} "
                        f"vol={c.get('volatility_condition')} trend={c.get('trend_condition')} "
                        f"dir={c.get('direction')} action={c.get('action_taken')} "
                        f"no_trade_reason={c.get('no_trade_reason')}"))
    for d in decisions:
        events.append((d.get("decision_time") or "", "DECISION",
                        f"{d.get('action')} {d.get('strategy_name') or ''} — {d.get('reason')}"))
    for t in trade_entries:
        events.append((t.get("entry_time") or "", "ENTRY",
                        f"{t.get('strategy_name')} lots={t.get('final_lots')} "
                        f"credit/debit={t.get('entry_credit') or t.get('entry_debit')} "
                        f"reason={t.get('selection_reason')}"))
    for e in trade_exits:
        events.append((e.get("exit_time") or "", "EXIT",
                        f"{e.get('strategy_name')} reason={e.get('exit_reason')} "
                        f"net_pnl={e.get('net_pnl_rupees')} result={e.get('result')}"))
    for line in filter_log_lines_by_level(audit_lines, {"WARNING", "ERROR", "CRITICAL"}):
        parts = line.split("|")
        ts = parts[0].strip() if parts else ""
        msg = "|".join(parts[2:]).strip() if len(parts) > 2 else line
        level = parts[1].strip() if len(parts) > 1 else "LOG"
        events.append((ts, f"LOG:{level}", msg))

    events.sort(key=lambda x: x[0])
    return events


# ═══════════════════════════════════════════════════════════════════════
# MARKDOWN RENDERING HELPERS
# ═══════════════════════════════════════════════════════════════════════
def md_kv(d: dict) -> str:
    if not d:
        return "_(none)_\n"
    lines = []
    for k, v in d.items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines) + "\n"


def md_table(rows: list, columns: list, max_rows: int = 80) -> str:
    if not rows:
        return "_(no data)_\n"
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    display_rows = rows[:max_rows] if max_rows else rows
    for r in display_rows:
        vals = []
        for c in columns:
            v = r.get(c, "")
            if v is None:
                v = ""
            if isinstance(v, float):
                v = f"{v:.4f}"
            vals.append(str(v).replace("|", "\\|").replace("\n", " ")[:120])
        lines.append("| " + " | ".join(vals) + " |")
    out = "\n".join(lines) + "\n"
    if max_rows and len(rows) > max_rows:
        out += f"\n_... {len(rows) - max_rows} more row(s) omitted here — see full data in the raw JSON export folder._\n"
    return out


# ═══════════════════════════════════════════════════════════════════════
# MAIN REPORT ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════
def generate_report(target_date: str) -> None:
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

    conn.close()

    # ── Derived analytics ──
    gaps = detect_cycle_gaps(cycle_rows)
    no_trade_reasons = aggregate_no_trade_reasons(decisions)
    strategies_used = aggregate_strategies(trade_entries)
    cost_analysis = analyze_trade_costs(trade_entries, trade_exits)
    chain_summary = summarize_option_chain(chain_rows)
    api_summary = summarize_api_calls(api_rows)
    anomalies = detect_anomalies(session_state, cycle_rows, api_summary, gaps, daily_summary, decisions)
    timeline = build_master_timeline(cycle_rows, decisions, trade_entries, trade_exits, audit_file_lines)
    warning_error_lines = filter_log_lines_by_level(audit_file_lines, {"WARNING", "ERROR", "CRITICAL"})

    legs_by_position = {}
    for l in legs:
        legs_by_position.setdefault(l["position_id"], []).append(l)
    exits_by_position = {e["position_id"]: e for e in trade_exits}

    # ── Build markdown ──
    md = []
    md.append(f"# NIFTY Intraday Options Engine — EOD Forensic Report")
    md.append(f"**Target Date:** {target_date}  |  **Report Generated:** {datetime.now().isoformat()}\n")

    md.append("## 0. How to Read This Report (context for an LLM/analyst with no prior context)\n")
    md.append(
        "This report describes one trading day of a NIFTY-50 intraday options-selling/buying algo "
        "engine. The engine architecture is 5 files: (1) core infra — SQLite DB + Upstox API client + "
        "logging, (2) market data engine — computes VRP (implied vol minus realized vol), Parkinson "
        "realized volatility, ADX/trend condition, opening range, VWAP/PCR/skew directional bias, (3) "
        "strategy engine — selects a strategy (Iron Condor/Butterfly, Bull Put/Bear Call spreads, debit "
        "spreads, straddles, or NO_TRADE) based on a VRP x Trend x Direction decision matrix, computes "
        "strikes/credit/sizing/stops, (4) execution engine — pre-trade validation, paper/live order "
        "routing, per-cycle position monitoring (stop/target/VWAP/ADX/delta breach exits), (5) main "
        "engine — orchestration loop (5-min cycles, 10:00-15:00 trading window), EOD summary. "
        "Every table referenced below comes directly from the engine's SQLite database "
        "(`session_state`, `cycle_log`, `strategy_decisions`, `positions`, `position_legs`, "
        "`trade_entries`, `trade_exits`, `daily_summary`, `option_chain_snapshot`, `api_call_log`, "
        "`audit_log`) plus the file-based audit log. Full untruncated raw data for every table is "
        f"exported alongside this report in `eod_report_{target_date}_raw/`.\n"
    )

    md.append("## 1. Table of Contents\n")
    md.append(
        "2. Executive Summary\n"
        "3. Auto-Detected Anomalies / Flags\n"
        "4. Session Configuration Snapshot\n"
        "5. Market Data Timeline (cycle_log)\n"
        "6. Strategy Decision Log\n"
        "7. No-Trade Reason Frequency\n"
        "8. Trade-by-Trade Deep Dive\n"
        "9. Position & Leg Raw Detail\n"
        "10. Option Chain Snapshot Statistics\n"
        "11. API Call Health\n"
        "12. Data Quality Checks\n"
        "13. Audit Log — Warnings & Errors\n"
        "14. Unified Master Timeline\n"
        "15. Daily Summary (as computed by the engine itself)\n"
        "16. Raw Data Export Manifest\n"
    )

    # 2. Executive summary
    md.append("## 2. Executive Summary\n")
    exec_summary = {
        "Day label": session_state.get("day_label") if session_state else "N/A",
        "Day mode": session_state.get("day_mode") if session_state else "N/A",
        "VIX regime (final)": session_state.get("vix_regime") if session_state else "N/A",
        "OR condition / width": f"{session_state.get('or_condition')} / {session_state.get('or_width')}"
                                  if session_state else "N/A",
        "Trades attempted (decisions)": len(decisions),
        "Trades executed": len(trade_entries),
        "Trades closed": len(trade_exits),
        "Wins / Losses": f"{sum(1 for e in trade_exits if e.get('result')=='WIN')} / "
                          f"{sum(1 for e in trade_exits if e.get('result')=='LOSS')}",
        "Net P&L (Rs, from trade_exits)": round(sum(e.get('net_pnl_rupees') or 0 for e in trade_exits), 2),
        "Realized daily_pnl (session_state)": session_state.get("daily_pnl") if session_state else "N/A",
        "Current capital (session_state)": session_state.get("current_capital") if session_state else "N/A",
        "Daily halted": session_state.get("daily_halted") if session_state else "N/A",
        "Consecutive stops": session_state.get("consecutive_stops") if session_state else "N/A",
        "Cycles logged": len(cycle_rows),
        "Option chain snapshot rows": len(chain_rows),
        "API calls made": len(api_rows),
        "Audit log lines (file)": len(audit_file_lines),
    }
    md.append(md_kv(exec_summary))

    # 3. Anomalies
    md.append("## 3. Auto-Detected Anomalies / Flags\n")
    md.append("\n".join(f"- {f}" for f in anomalies) + "\n")

    # 4. Session config
    md.append("## 4. Session Configuration Snapshot (full session_state row)\n")
    md.append(md_kv(session_state) if session_state else "_No session_state row found for this date — "
                                                            "the engine likely never ran on this date._\n")

    # 5. Cycle log
    md.append("## 5. Market Data Timeline (cycle_log — every 5-minute cycle)\n")
    md.append(f"Total cycles recorded: {len(cycle_rows)}. "
              f"Timing gaps (>10 min between consecutive cycles) detected: {len(gaps)}.\n")
    if gaps:
        md.append("**Gap details:**\n")
        for g in gaps:
            md.append(f"- {g[0]} -> {g[1]} ({g[2]:.1f} minutes)\n")
    md.append(md_table(cycle_rows, [
        "cycle_time", "spot", "vix", "vrp", "atm_iv_pct", "parkinson_rv_pct", "adx", "adx_condition",
        "vwap_dist_pct", "pcr", "skew_ratio", "or_condition", "volatility_condition", "trend_condition",
        "direction", "action_taken", "no_trade_reason", "open_positions", "daily_pnl_net",
    ], max_rows=100))

    # 6. Strategy decisions
    md.append("## 6. Strategy Decision Log (every decision, chronological)\n")
    md.append(md_table(decisions, ["decision_time", "action", "strategy_name", "reason"], max_rows=100))

    selected = [d for d in decisions if d.get("action") in ("STRATEGY_SELECTED", "ENTER")]
    if selected:
        md.append("\n### 6a. Full Parameter Detail for Selected/Entered Strategies\n")
        for d in selected:
            md.append(f"\n**Decision at {d.get('decision_time')} — {d.get('strategy_name')}**\n")
            md.append(f"- Reason: {d.get('reason')}\n")
            try:
                params = json.loads(d["params_json"]) if d.get("params_json") else {}
            except Exception:
                params = {}
            if params:
                md.append("```json\n" + json.dumps(params, indent=2, default=str) + "\n```\n")

    # 7. No-trade reasons
    md.append("## 7. No-Trade Reason Frequency\n")
    if no_trade_reasons:
        for reason, count in no_trade_reasons.items():
            md.append(f"- [{count}x] {reason}\n")
    else:
        md.append("_No NO_TRADE decisions recorded (or no decisions at all)._\n")

    # 8. Trade-by-trade deep dive
    md.append("## 8. Trade-by-Trade Deep Dive\n")
    if not trade_entries:
        md.append("_No trades were entered on this date._\n")
    for t in trade_entries:
        md.append(f"\n### Position `{t['position_id']}` — {t['strategy_name']}\n")
        md.append(md_kv({
            "Entry time": t.get("entry_time"), "Day label": t.get("day_label"),
            "Selection reason": t.get("selection_reason"),
            "Entry spot / VIX / VRP": f"{t.get('entry_spot')} / {t.get('entry_vix')} / {t.get('entry_vrp')}",
            "Volatility / trend / direction at entry": f"{t.get('volatility_condition')} / "
                                                          f"{t.get('trend_condition')} / {t.get('direction')}",
            "Target expiry / DTE": f"{t.get('target_expiry')} / {t.get('actual_dte')}",
            "Entry credit/debit (pts)": t.get("entry_credit") or t.get("entry_debit"),
            "Gross credit (pts)": t.get("gross_credit"), "Slippage assumed (pts)": t.get("total_slippage"),
            "Entry costs (Rs)": t.get("entry_costs_rupees"),
            "Stop / target (pts)": f"{t.get('stop_premium')} / {t.get('target_premium')}",
            "Price stop (pts)": t.get("price_stop_pts"), "Final lots": t.get("final_lots"),
            "Max loss/lot (Rs)": t.get("max_loss_per_lot"), "Total max risk (Rs)": t.get("total_max_risk"),
            "Capital at entry": t.get("capital_at_entry"), "Paper trade": t.get("paper_trade"),
        }))
        try:
            leg_list = json.loads(t["legs_json"]) if t.get("legs_json") else []
        except Exception:
            leg_list = []
        if leg_list:
            md.append("\n**Legs at entry:**\n")
            md.append(md_table(leg_list, ["action", "option_type", "strike", "exec_price", "delta", "iv", "oi"]))

        exit_row = exits_by_position.get(t["position_id"])
        if exit_row:
            md.append("\n**Exit:**\n")
            md.append(md_kv({
                "Exit time": exit_row.get("exit_time"), "Exit reason": exit_row.get("exit_reason"),
                "Hold time (min)": exit_row.get("hold_minutes"), "Exit premium (pts)": exit_row.get("exit_premium"),
                "Gross P&L (pts / Rs)": f"{exit_row.get('gross_pnl_pts')} / {exit_row.get('gross_pnl_rupees')}",
                "Exit costs (Rs)": exit_row.get("exit_costs_rupees"),
                "Total costs (Rs)": exit_row.get("total_costs_rupees"),
                "Net P&L (pts / Rs / %)": f"{exit_row.get('net_pnl_pts')} / {exit_row.get('net_pnl_rupees')} / "
                                            f"{exit_row.get('net_pnl_pct')}",
                "Result": exit_row.get("result"), "Profit % of credit": exit_row.get("profit_pct_of_credit"),
            }))
        else:
            md.append("\n**Exit:** _No matching trade_exits row found — position may still be open, "
                       "or an exit failed to record. Check `positions.status` and audit log for this "
                       f"position_id ({t['position_id']})._\n")

        cost_row = next((c for c in cost_analysis if c["position_id"] == t["position_id"]), None)
        if cost_row:
            md.append("\n**Recomputed entry cost breakdown** (independently recalculated from stored leg "
                        "prices, for cross-checking against the recorded total):\n")
            md.append(md_kv(cost_row["recomputed_entry_costs"]))
            md.append(f"- Recorded entry costs (Rs): {cost_row['recorded_entry_costs_rupees']}\n")

    # 9. Position & leg raw detail
    md.append("## 9. Position & Leg Raw Detail\n")
    md.append(md_table(positions, [
        "position_id", "strategy_name", "entry_time", "final_lots", "entry_credit", "entry_debit",
        "status", "exit_time", "exit_reason", "net_pnl_rupees", "paper_trade",
    ]))
    md.append("\n### All Legs\n")
    md.append(md_table(legs, [
        "position_id", "strike", "option_type", "action", "qty", "entry_price", "exit_price",
        "entry_delta", "leg_status",
    ]))

    # 10. Option chain stats
    md.append("## 10. Option Chain Snapshot Statistics\n")
    md.append(md_kv(chain_summary))
    md.append(f"\n_Full option chain snapshot ({len(chain_rows)} rows) exported to raw JSON — "
              f"too large to inline here._\n")

    # 11. API health
    md.append("## 11. API Call Health\n")
    md.append(md_kv({k: v for k, v in api_summary.items() if k != "errors_sample"}))
    if api_summary.get("errors_sample"):
        md.append("\n**Sample of API errors:**\n")
        md.append(md_table(api_summary["errors_sample"], ["call_time", "category", "endpoint", "status_code",
                                                             "error_message"]))

    # 12. Data quality checks
    md.append("## 12. Data Quality Checks\n")
    dq = {
        "Cycles with missing spot": sum(1 for c in cycle_rows if c.get("spot") is None),
        "Cycles with missing VIX": sum(1 for c in cycle_rows if c.get("vix") is None),
        "Cycles with missing VRP": sum(1 for c in cycle_rows if c.get("vrp") is None),
        "Cycles with volatility_condition=UNKNOWN": sum(1 for c in cycle_rows
                                                           if c.get("volatility_condition") == "UNKNOWN"),
        "Cycles with trend_condition=OR_PENDING": sum(1 for c in cycle_rows
                                                          if c.get("trend_condition") == "OR_PENDING"),
        "Option chain rows with zero bid/ask": chain_summary.get("zero_bid_ask_count", 0),
        "Timing gaps (>10min) in cycle_log": len(gaps),
        "Trades with no matching exit row": sum(1 for t in trade_entries
                                                    if t["position_id"] not in exits_by_position),
    }
    md.append(md_kv(dq))

    # 13. Warnings & errors
    md.append("## 13. Audit Log — Warnings & Errors (file-based log, filtered)\n")
    md.append(f"Total WARNING/ERROR/CRITICAL lines today: {len(warning_error_lines)} "
              f"(of {len(audit_file_lines)} total log lines; {len(audit_db_rows)} rows in audit_log DB table)\n\n")
    if warning_error_lines:
        md.append("```\n" + "\n".join(warning_error_lines[:200]) + "\n```\n")
        if len(warning_error_lines) > 200:
            md.append(f"_... {len(warning_error_lines) - 200} more warning/error lines omitted — "
                      f"see raw export for full list._\n")
    else:
        md.append("_No warnings or errors logged today._\n")

    # 14. Master timeline
    md.append("## 14. Unified Master Timeline (all events merged chronologically)\n")
    md.append(md_table(
        [{"time": e[0], "type": e[1], "detail": e[2]} for e in timeline],
        ["time", "type", "detail"], max_rows=150,
    ))

    # 15. Daily summary
    md.append("## 15. Daily Summary (as computed by the engine's own EOD process)\n")
    md.append(md_kv(daily_summary) if daily_summary else
              "_No daily_summary row found — EOD tasks may not have run yet for this date "
              "(e.g., if the engine is still mid-session or was killed before EOD).\n")

    # 16. Manifest
    md.append("## 16. Raw Data Export Manifest\n")
    md.append(f"All tables for {target_date} were exported in full (untruncated) to: "
              f"`eod_report_{target_date}_raw/`\n")

    # ── Write files ──
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"eod_report_{target_date}.md"
    report_path.write_text("\n".join(md), encoding="utf-8")

    raw_dir = OUTPUT_DIR / f"eod_report_{target_date}_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_exports = {
        "session_state": session_state, "cycle_log": cycle_rows, "strategy_decisions": decisions,
        "positions": positions, "position_legs": legs, "trade_entries": trade_entries,
        "trade_exits": trade_exits, "daily_summary": daily_summary, "option_chain_snapshot": chain_rows,
        "api_call_log": api_rows, "audit_log_db": audit_db_rows, "audit_log_file_lines": audit_file_lines,
        "cost_analysis": cost_analysis, "master_timeline": timeline, "anomaly_flags": anomalies,
        "no_trade_reason_counts": no_trade_reasons, "strategies_used_counts": strategies_used,
        "chain_summary": chain_summary, "api_summary": {k: v for k, v in api_summary.items()
                                                          if k != "errors_sample"},
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