#!/usr/bin/env python3
# ============================================================================
#  strategy_lab.py — counterfactual strategy laboratory
# ============================================================================
#
#  PURPOSE: answer "what would a different STRUCTURE / entry time / wing /
#  stop-target have earned on a recorded session" WITHOUT changing the
#  engine's decision code.
#
#  WHAT IS REAL (identical to backtest_engine.run_day):
#    * MarketDataEngine.run_cycle() signals from recorded snapshots
#    * the forced legs are filled by the same FillModel (bid/ask recorded)
#    * strategy_engine cost math (_compute_costs)
#    * ExecutionEngine.monitor_position() — the full 7-priority exit ladder
#      (delta breach, spot proximity, price/premium stop, profit lock,
#       cheap buyback, time target / 0DTE gamma derisk, hard exit)
#
#  WHAT IS FORCED (and therefore NOT an engine decision):
#    * structure type, strikes, wings, entry time, lots, stop/target params.
#      These are built geometrically around ATM, labelled LAB_*.
#
#  Every printed rupee is a simulated fill against RECORDED quotes. It still
#  inherits the harness caveats (no partial fills / latency, snapshot
#  granularity biases optimistic).
#
#  USAGE:
#    python strategy_lab.py --db data/per_day/nifty_algo_2026-09-10.db \
#        --date 2026-09-10 --lots 3
# ============================================================================

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import core  # noqa: E402
from core import Config, Database, load_config  # noqa: E402
import backtest_engine as bt  # noqa: E402


# ---------------------------------------------------------------------------
#  candidate catalogue
# ---------------------------------------------------------------------------
def atm50(spot: float) -> float:
    return round(spot / 50.0) * 50.0


def build_candidates(spot: float, chain: dict, dte: int) -> List[dict]:
    """Return list of {name, wing, shorts, legs} built from the live chain."""
    a = atm50(spot)
    out: List[dict] = []

    def q(k, t):
        return (chain.get(float(k)) or {}).get(t)

    def ok(k, t):
        x = q(k, t)
        return bool(x) and float(x.get("bid") or 0) > 0 and float(x.get("ask") or 0) > 0

    def add(name: str, wing: Optional[float], specs):
        legs = []
        for k, t, act in specs:
            if not ok(k, t):
                return
            legs.append({"strike": float(k), "option_type": t, "action": act,
                         "exec_price": q(k, t).get("ltp")})
        out.append({"name": name, "wing": wing, "legs": legs})

    # defined-risk verticals (w = wing width)
    for n in (150, 200, 250):
        for w in (100, 150):
            add(f"BC n+{n} w{w}", float(w),
                [(a + n, "call", "SELL"), (a + n + w, "call", "BUY")])
            add(f"BP n-{n} w{w}", float(w),
                [(a - n, "put", "SELL"), (a - n - w, "put", "BUY")])

    # iron condors (defined-risk strangles)
    for n in (150, 200, 250):
        for w in (100, 150):
            add(f"IC +-{n} w{w}", float(w),
                [(a + n, "call", "SELL"), (a + n + w, "call", "BUY"),
                 (a - n, "put", "SELL"), (a - n - w, "put", "BUY")])

    # iron butterflies (sell ATM straddle, buy wings) — name MUST contain
    # "BUTTERFLY" so the execution ladder uses the fly's proximity logic
    for w in (100, 150):
        add(f"IRON_BUTTERFLY ATM w{w}", float(w),
            [(a, "call", "SELL"), (a + w, "call", "BUY"),
             (a, "put", "SELL"), (a - w, "put", "BUY")])

    # naked short straddle (LAB reference: risk controlled ONLY by stops)
    add("STRADDLE naked", None,
        [(a, "call", "SELL"), (a, "put", "SELL")])

    # naked short strangles (LAB reference)
    for n in (100, 150, 200):
        add(f"STRANGLE n{n} naked", None,
            [(a + n, "call", "SELL"), (a - n, "put", "SELL")])

    return out


# ---------------------------------------------------------------------------
#  parameter construction (mirrors strategy_engine.compute_strategy_params)
# ---------------------------------------------------------------------------
def make_params(runner: "bt.BacktestRunner", cand: dict, signals: dict,
                lots: int, expiry: str, hard_exit: str,
                strict_liq: bool = True) -> Optional[dict]:
    se, cfg = runner.se, runner.config
    dte = int(signals.get("actual_dte") or 0)
    spot = float(signals.get("spot") or 0)
    legs = cand["legs"]
    wing = cand["wing"]

    # tradability: enforce the SAME oi / spread / premium gates the real
    # engine's leg builder enforces, so every result could actually be traded
    if strict_liq:
        chain_live = runner.me.last_chain
        for leg in legs:
            okv, why = se._validate_leg(
                chain_live, float(leg["strike"]), leg["option_type"],
                leg["action"])
            if not okv:
                return None

    # prices / costs identical to the harness open path
    filled = []
    credit = 0.0
    for leg in legs:
        k = float(leg["strike"])
        q = (runner.me.last_chain.get(k) or {}).get(leg["option_type"]) or {}
        px = runner.fills.price(q, leg["action"])
        if px is None or px <= 0:
            return None
        filled.append({**leg, "exec_price": px,
                       "bid": float(q.get("bid") or 0), "ask": float(q.get("ask") or 0),
                       "delta": float(q.get("delta") or 0),
                       "gamma": float(q.get("gamma") or 0)})
        credit += px if leg["action"] == "SELL" else -px
    entry_costs = se._compute_costs(filled, lots, "ENTRY")["total_rupees"]
    entry_costs_pts = entry_costs / (cfg.lot_size * lots)
    net_credit = credit - entry_costs_pts
    if net_credit <= 0:
        return None

    # stop multiple (DTE aware, same constants as strategy engine)
    if dte == 0:
        stop_mult = float(getattr(cfg, "stop_mult_dte0", 1.40))
    elif dte == 1:
        stop_mult = float(getattr(cfg, "stop_mult_dte1", 1.55))
    else:
        stop_mult = float(getattr(cfg, "stop_mult_dte2p", 1.70))
    stop_premium = max(net_credit * stop_mult, net_credit * 1.10)
    if wing:
        stop_premium = min(stop_premium, float(wing))

    target_pct = se._get_target_pct(dte, signals)
    target_premium = net_credit * (1.0 - target_pct)

    # spot backstop
    short_dists = [abs(float(l["strike"]) - spot) for l in filled
                   if l["action"] == "SELL"]
    min_dist = min(short_dists) if short_dists else 0.0
    wing_for_stop = float(wing) if wing else max(150.0, 1.5 * min_dist)
    pstop = se._price_stop_pts(wing_for_stop, min_dist, spot)
    atm_stop = max(wing_for_stop * 0.55, pstop)
    call_lvl = put_lvl = None
    for l in filled:
        if l["action"] != "SELL":
            continue
        k = float(l["strike"])
        if l["option_type"] == "call":
            lvl = k - pstop
            if lvl <= spot + 5.0:
                lvl = k + atm_stop
            call_lvl = lvl
        else:
            lvl = k + pstop
            if lvl >= spot - 5.0:
                lvl = k - atm_stop
            put_lvl = lvl

    opening_straddle = float(signals.get("opening_straddle_pts") or 0)

    return {
        "strategy_name": f"LAB_{cand['name'].replace(' ', '_').replace('+','p').replace('-','m')}",
        "strategy_type": "SELL",
        "selection_reason": "lab",
        "legs": legs,
        "final_lots": lots,
        "num_legs": len(legs),
        "wing_width": wing if wing else 0,
        "actual_dte": dte,
        "target_expiry": expiry,
        "entry_spot": spot,
        "entry_vix": signals.get("vix"),
        "hard_exit_time": hard_exit,
        "final_regime_at_entry": signals.get("final_regime"),
        "gross_credit": round(credit, 3),
        "opening_straddle_at_entry": opening_straddle,
        "stop_premium": round(stop_premium, 3),
        "target_premium": round(target_premium, 3),
        "price_stop_level_call": call_lvl,
        "price_stop_level_put": put_lvl,
        "max_loss_per_lot": 0,
        "total_max_risk": 0,
        "_lab_net_credit": net_credit,
        "_lab_target_pct": target_pct,
    }


# ---------------------------------------------------------------------------
#  one session replay carrying a whole book of forced trials
# ---------------------------------------------------------------------------
def run_lab(store, cfg: Config, trading_date: str, entry_times: List[str],
            lots: int, hard_exit: Optional[str], only: Optional[str] = None,
            sequential: bool = False) -> List[dict]:
    day = store.load_day(trading_date)
    if len(day.cycles) < 5:
        print(f"{trading_date}: only {len(day.cycles)} snapshots")
        return []

    runner = bt.BacktestRunner(store, cfg, bt.FillModel(0.25, 0.5), verbose=False)
    runner._build()
    sink = io.StringIO()
    trades: List[dict] = []
    opened_entries: set = set()
    open_trials: List[tuple] = []  # (cand_name, entry_time, live, params)
    try:
        with contextlib.redirect_stdout(sink):
            for capture_time in day.cycles:
                dt = day.cycle_dt(capture_time)
                runner.clock.set(dt)
                runner.client.point(day, capture_time)
                try:
                    signals = runner.me.run_cycle()
                    signals = runner._classify(signals)
                except Exception:
                    continue
                chain = runner.me.last_chain or {}
                expiry = runner.me.last_chain_expiry
                expiry_s = expiry.isoformat() if expiry else None
                spot = float(signals.get("spot") or 0)
                if spot <= 0 or not chain:
                    continue
                hhmm = dt.strftime("%H:%M")

                # open books whose entry time has arrived (once)
                for et in entry_times:
                    if et in opened_entries:
                        continue
                    if hhmm < et:
                        continue
                    # sequential policy: at most one open position at a time,
                    # exactly how the live engine runs
                    if sequential and open_trials:
                        continue
                    dte = int(signals.get("actual_dte") or 0)
                    he = hard_exit or "15:00"
                    cands = build_candidates(spot, chain, dte)
                    n_opened = 0
                    for cand in cands:
                        if only and only not in cand["name"]:
                            continue
                        params = make_params(runner, cand, signals, lots,
                                             expiry_s, he)
                        if params is None:
                            continue
                        live = runner._open(params, signals, day)
                        if live is not None:
                            open_trials.append((cand["name"], et, live, params))
                            n_opened += 1
                            if sequential:
                                break
                    if n_opened or not only or hhmm >= "14:30":
                        opened_entries.add(et)  # slot resolved
                    print(f"[lab] {trading_date} {hhmm} opened {n_opened} "
                          f"trials (dte={dte}, spot={spot:.0f})", file=sys.stderr,
                          flush=True)

                # monitor every open trial through the REAL exit ladder
                still: List[tuple] = []
                for name, et, live, params in open_trials:
                    row = runner.db.query_one(
                        "SELECT * FROM positions WHERE position_id=?",
                        (live["position_id"],))
                    # wrong-expiry chain (e.g. 0DTE feed switch): cannot mark
                    if expiry_s and expiry_s != row["target_expiry"]:
                        still.append((name, et, live, params))
                        continue
                    try:
                        action, priority, ctx = runner.xe.monitor_position(
                            dict(row), signals)
                    except Exception:
                        action, priority, ctx = "HOLD", 0, {}
                    if action != "HOLD" and not action.startswith("TIGHTEN"):
                        reason = ctx.get("reason_detail") or action
                        t = runner._close(live, signals, reason, priority, day)
                        trades.append(t.as_row())
                    else:
                        still.append((name, et, live, params))
                open_trials = still

            # forced flat for anything still alive at last snapshot
            runner.clock.set(day.cycle_dt(day.cycles[-1]))
            runner.client.point(day, day.cycles[-1])
            try:
                with contextlib.redirect_stdout(sink):
                    signals = runner.me.run_cycle()
            except Exception:
                signals = {}
            for name, et, live, params in open_trials:
                t = runner._close(live, signals, "LAB_END_FLAT", 7, day)
                trades.append(t.as_row())
    finally:
        runner._teardown()
    return trades


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--lots", type=int, default=3)
    ap.add_argument("--entries", default="")
    ap.add_argument("--hard-exit", default=None)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--only", default=None, help="substring filter on structure")
    ap.add_argument("--sequential", action="store_true",
                    help="one position at a time, re-enter at later slots")
    args = ap.parse_args()

    cfg = load_config()
    store = bt.HistoricalStore(args.db)

    # sensible default entry grids; expiry day only has the 0DTE chain from noon
    if args.entries:
        entries = args.entries.split(",")
    elif args.date == "2026-09-08":
        entries = ["12:05", "12:25", "12:50"]
    else:
        entries = ["09:50", "11:15", "12:45"]

    trades = run_lab(store, cfg, args.date, entries, args.lots,
                     args.hard_exit, only=args.only, sequential=args.sequential)
    if not trades:
        print("no trades produced")
        return 1

    if args.sequential:
        tot = sum(t["pnl_rs"] for t in trades)
        print(f"\n=== SEQUENTIAL POLICY {args.date} | {args.only} | "
              f"lots={args.lots} | entries={entries} ===")
        print(f"{'entry':6s} {'exit':6s} {'structure':24s} {'gross':>7s} "
              f"{'costs':>6s} {'P&L Rs':>9s} exit_reason")
        for t in trades:
            print(f"{t['entry_time'][0:5]:6s} {t['exit_time'][0:5]:6s} "
                  f"{t['strategy'].replace('LAB_',''):24s} "
                  f"{t['gross_pts']:>7.1f} {t['costs_rs']:>6.0f} "
                  f"{t['pnl_rs']:>9,.0f} {t['exit_reason'][:38]}")
        print(f"DAILY TOTAL P&L: Rs {tot:,.0f} over {len(trades)} trade(s)")
        return 0

    # aggregate by (entry_time, strategy)
    agg: Dict[tuple, dict] = {}
    for t in trades:
        key = (t["entry_time"][:5], t["strategy"].replace("LAB_", ""))
        a = agg.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0,
                                 "exit": "", "gross": 0.0, "costs": 0.0,
                                 "held": 0})
        a["n"] += 1
        a["pnl"] += t["pnl_rs"]
        a["gross"] += t["gross_pts"] * 65 * t["lots"]
        a["costs"] += t["costs_rs"]
        a["held"] += t["held_min"]
        if t["pnl_rs"] > 0:
            a["wins"] += 1
        a["exit"] = t["exit_reason"][:30]
        a["lots"] = t["lots"]

    rows = sorted(agg.items(), key=lambda kv: -kv[1]["pnl"])
    print(f"\n=== LAB {args.date}  lots={args.lots}  entries={entries} ===")
    print(f"{'entry':6s} {'structure':22s} {'trials':>6s} {'win%':>5s} "
          f"{'P&L Rs':>10s} {'gross Rs':>10s} {'costs':>7s} {'avgHold':>7s} exit")
    for (et, name), a in rows[:args.top]:
        print(f"{et:6s} {name:22s} {a['n']:>6d} {100*a['wins']/a['n']:>4.0f}% "
              f"{a['pnl']:>10,.0f} {a['gross']:>10,.0f} {a['costs']:>7,.0f} "
              f"{a['held']/a['n']:>6.0f}m {a['exit']}")

    # per entry-time totals (one of EACH structure simultaneously is unrealistic;
    #  report best structure per entry time too)
    print("\n-- best structure per entry time --")
    best: Dict[str, tuple] = {}
    for (et, name), a in agg.items():
        if et not in best or a["pnl"] > best[et][1]["pnl"]:
            best[et] = (name, a)
    for et in sorted(best):
        name, a = best[et]
        print(f"{et}: {name:22s} P&L={a['pnl']:>9,.0f}  "
              f"gross={a['gross']:>8,.0f} costs={a['costs']:>6,.0f} exit={a['exit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
