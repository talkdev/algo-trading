#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 NIFTY REGIME-BASED OPTIONS TRADING ENGINE  —  main.py (orchestrator)
================================================================================
 Per the system spec:

  * PAPER_TRADING_MODE = True by default (config.py). No real orders unless
    you explicitly flip it to False AND supply a valid UPSTOX_ACCESS_TOKEN.
  * No runtime input: everything is hardcoded in config.py.
  * State is checkpointed to SQLite (state.db) on every fill, every regime
    change and every 10 s heartbeat -> crash recovery guaranteed.
  * All closed trades are logged to trade_analysis.csv (one row per strategy
    execution).
  * Regime detection runs every 5 minutes (Steps 0-8); strategy entry is
    gated to the 09:19:30-09:20:30 IST window; positions are managed every
    cycle; regime transitions A-E are applied on confirmed regime changes.

 USAGE
 -----
   python main.py                 # demo/simulated feed, paper trading
   python main.py --demo          # same as default
   python main.py --once          # single cycle then exit
   python main.py --data upstox   # real Upstox data + paper fills (needs env.txt)
   python main.py --no-color      # plain console output
================================================================================
"""
import argparse
import json
import signal
import sys
import time
import types
from datetime import datetime, timedelta

import config as C
from common_utils import Clock, VirtualClock, now_ist
from data_manager import (Feed, UpstoxClient, PaperBroker, LiveBroker,
                          DemoProvider, MarketDataStreamerV3, pick_expiries,
                          load_env_file)
from common_utilsimport (Loggers, enable_color, g, r, y, cy, mg, bo, dim, fx)
from state_store import StateStore
from regime_detector import RegimeDetector, load_events
from strategy_selector import StrategySelector
from order_executor import OrderExecutor
from common_utils import RiskManager

W = 88


def rule(ch="─"):
    return dim(ch * W)


def header_line(txt):
    return cy(" " + txt) + " " + dim("─" * max(0, W - 2 - len(txt)))


def parse_args():
    p = argparse.ArgumentParser(description="NIFTY regime-based options trading engine")
    p.add_argument("--demo", action="store_true", help="offline simulated feed (default when --data is omitted)")
    p.add_argument("--data", choices=["simulated", "upstox"], default=C.DATA_SOURCE,
                   help="market data source (default from config.py)")
    p.add_argument("--env", default=C.ENV_FILE, help="env file with UPSTOX_* keys")
    p.add_argument("--interval", type=int, default=None,
                   help="override detection interval in seconds (default 300)")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--virtual-step", type=int, default=C.DEMO_VIRTUAL_STEP_MIN,
                   help="demo: virtual minutes per cycle")
    p.add_argument("--demo-start", default=None,
                   help="demo: virtual start 'YYYY-MM-DD HH:MM' (IST), default next trading day 09:14")
    return p.parse_args()


# ---------------------------------------------------------------------------
# console report
# ---------------------------------------------------------------------------
def market_phase(now):
    if now.weekday() >= 5:
        return "CLOSED (weekend)", y
    if not C.is_trading_day(now.date()):
        return "CLOSED (NSE holiday)", y
    t = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= t <= 15 * 60 + 30:
        return "OPEN", g
    if t < 9 * 60 + 15:
        return "PRE-OPEN", y
    return "CLOSED (after hours)", y


def build_report(cyc, res, selector, executor, clock, phase, entry_state, meta):
    L = []
    now = res.ts
    L.append(bo(cy("╔" + "═" * (W - 2) + "╗")))
    title = "NIFTY REGIME OPTIONS ENGINE  ·  " + now.strftime("%a %d-%b-%Y %H:%M:%S IST")
    L.append(bo(cy("║" + title.center(W - 2) + "║")))
    L.append(bo(cy("╚" + "═" * (W - 2) + "╝")))
    phase_txt, phase_c = phase
    L.append(f"  Cycle #{cyc}  ·  {phase_c(phase_txt)}  ·  data: {meta['data_source']}  ·  "
             f"paper: {bo(str(meta['paper']))}  ·  Ctrl+C to stop")
    L.append(rule())

    L.append(header_line("MARKET SNAPSHOT"))
    L.append(f"   NIFTY 50   {bo(fx(res.spot))}   VIX {fx(res.vix)}   "
             f"RV{C.RV_WINDOW} {fx(res.rv)}%  IV_atm {fx(res.iv_atm)}%")
    L.append(f"   near expiry {res.near_expiry} · monthly {res.monthly_expiry} · "
             f"PCR {fx(res.pcr, 2)} · fut basis {fx(res.fut_basis, 2)}")
    L.append(rule())

    L.append(header_line("MODULE SCORES  (raw -> confirmed)"))
    labels = {"vol": "[1] VOL SURFACE ", "edge": "[2] VOL EDGE    ",
              "trend": "[3] TREND       ", "flow": "[4] ORDER FLOW  "}
    for m in ("vol", "edge", "trend", "flow"):
        mr = getattr(res, m)
        conf_s = _sc(mr.confirmed)
        raw_s = _sc(mr.raw)
        note = getattr(mr, "notes", [])
        L.append(f"   {labels[m]} {mr.detail}")
        L.append(f"   {' ' * len(labels[m])} score: {raw_s} -> {conf_s}   {dim('')}")
    L.append(rule())

    L.append(header_line("AGGREGATION (Steps 5-8)"))
    terms = " + ".join(f"{w}·({res.confirmed_scores[k]:+g})" for k, w in C.WEIGHTS.items())
    L.append(f"   Composite = {terms} = {bo(f'{res.composite:+.3f}')}")
    if res.override:
        L.append("   Macro: " + r(bo(res.macro_text)))
    else:
        L.append("   Macro override: OFF  (" + res.macro_text + ")")
    L.append(rule())

    col = g if res.composite > 0.15 else (r if res.composite < -0.15 else y)
    reg = bo(mg("EVENT_HEDGE")) if res.regime == C.REGIME_EVENT_HEDGE else bo(col(res.regime))
    L.append("  " + bo("★ CONFIRMED REGIME: ") + reg)
    L.append("    Action : " + C.REGIME_ACTION.get(res.regime, ""))
    if entry_state.get("msg"):
        L.append("    " + entry_state["msg"])
    L.append(rule())

    L.append(header_line("POSITIONS"))
    if not executor.open_trades:
        L.append("   no open trades")
    for tid, t in executor.open_trades.items():
        status = t.get("status", "?")
        nlegs = len(t["legs"])
        L.append(f"   {t['strategy_name']:<22} {status:<8} legs {nlegs}  "
                 f"entry {t['entry_ts'][:16]}  spot {fx(t.get('entry_spot'))}")
    L.append(rule())

    if res.notes:
        L.append(dim("   notes: " + " | ".join(res.notes)))
    if selector.last and selector.last.rationale:
        L.append(dim("   selection: " + selector.last.rationale))
    if meta.get("last_cycle_seconds") is not None:
        L.append(dim(f"   cycle time {meta['last_cycle_seconds']*1000:.0f} ms · "
                     f"day pnl {meta.get('day_pnl', 0.0):+.0f} pts"))
    return "\n".join(L)


def _sc(v):
    if v is None:
        return dim("n/a")
    s = f"{v:+g}"
    return g(s) if v > 0 else (r(s) if v < 0 else y(s))


# ---------------------------------------------------------------------------
# bootstrap helpers
# ---------------------------------------------------------------------------
def resolve_data_source(args):
    """-> (client, master, streamer_or_None, note)"""
    if args.data == "upstox":
        env = load_env_file(args.env)
        token = env.get("UPSTOX_ACCESS_TOKEN", "")
        if not token:
            print(r("ERROR: --data upstox requires UPSTOX_ACCESS_TOKEN in " + args.env))
            sys.exit(2)
        client = UpstoxClient(token)
        master = client.instrument_master(cache_dir=C.LOG_DIR)
        streamer = None
        if C.PAPER_TRADING_MODE:
            note = "upstox data + PAPER fills"
        else:
            note = "LIVE upstox data + LIVE orders"
        return client, master, streamer, note
    # simulated
    demo = DemoProvider(clock=_global_clock)
    master = demo.instruments()
    return demo, master, None, "simulated feed (offline)"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()
    enable_color(not args.no_color)
    if args.demo:
        args.data = "simulated"

    print(bo(cy("═" * W)))
    print(bo(cy(" NIFTY REGIME-BASED OPTIONS TRADING ENGINE — starting up")))
    print(bo(cy("═" * W)))
    print(f"  paper trading : {bo('TRUE') if C.PAPER_TRADING_MODE else bo(r('FALSE — LIVE'))}")
    print(f"  data source   : {args.data}")
    if C.PAPER_TRADING_MODE and args.data == "upstox":
        print(y("  NOTE: paper fills with real Upstox quotes — no real orders."))
    if not C.PAPER_TRADING_MODE and args.data == "simulated":
        print(r("  FATAL: LIVE mode requires --data upstox. Aborting."))
        sys.exit(2)

    # --------------------------------------------------------------- clock
    use_virtual = args.data == "simulated"
    if use_virtual:
        start = None
        if args.demo_start:
            try:
                from common_utils import IST
                start = datetime.strptime(args.demo_start, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            except ValueError:
                print(r(f"bad --demo-start {args.demo_start} (use YYYY-MM-DD HH:MM)"))
                sys.exit(2)
        if start is None:
            d = now_ist().date()
            while not C.is_trading_day(d):
                d += timedelta(days=1)
            start = datetime(d.year, d.month, d.day, 9, 14, 0).replace(tzinfo=now_ist().tzinfo)
        clock = VirtualClock(start, timedelta(minutes=args.virtual_step))
        print(y(f"  virtual clock starts {start:%a %d-%b-%Y %H:%M} IST "
                f"(+{args.virtual_step} min/cycle)"))
    else:
        clock = Clock()
    global _global_clock
    _global_clock = clock

    # ------------------------------------------------------- trading-day gate
    today = clock.now().date()
    if not C.is_trading_day(today):
        if C.ALLOW_NON_TRADING_DAY_RUN or use_virtual:
            print(y(f"  WARNING: {today} is not an NSE trading day — continuing "
                    f"({('test override' if C.ALLOW_NON_TRADING_DAY_RUN else 'virtual clock')})."))
        else:
            print(r(f"  {today} is not an NSE trading day (holiday/weekend). Refusing to run."))
            sys.exit(0)

    # ------------------------------------------------------------- plumbing
    loggers = Loggers()
    state = StateStore(C.STATE_DB)
    events, ev_err = load_events(C.EVENTS_FILE)

    client, master, streamer, ds_note = resolve_data_source(args)
    feed = Feed(client, streamer)
    if use_virtual:
        broker = PaperBroker(feed, capital=C.DEMO_ACCOUNT_CAPITAL)
    else:
        broker = PaperBroker(feed) if C.PAPER_TRADING_MODE else LiveBroker(client)

    ctx = types.SimpleNamespace(
        feed=feed, broker=broker, clock=clock, state=state, loggers=loggers,
        risk=None, lot_size=master.get("lot_size", C.NIFTY_LOT_SIZE),
        last_spot=None, last_vix=None, last_regime=None,
        demo=use_virtual,
    )
    risk = RiskManager(broker, state, loggers, capital=broker.cash)
    ctx.risk = risk

    detector = RegimeDetector(feed, state, clock, events=events, loggers=loggers)
    detector.futs = master.get("futs", [])
    selector = StrategySelector(ctx, detector)
    executor = OrderExecutor(ctx)

    # ---- demo warm-up: pre-seed skew history + relax flow warm-up ------------
    if use_virtual:
        import random
        if not state.skew_series(limit=1):
            rng = random.Random(9)
            base = clock.now().date()
            for i in range(25, 0, -1):
                state.record_skew(round(rng.gauss(1.6, 0.8), 3),
                                  (base - timedelta(days=i)).isoformat())
            print(y(f"  demo warm-up: seeded 25 days of skew history"))
        C.FLOW_MIN_AGE = max(2 * args.virtual_step * 60, 4.0)
        C.FLOW_TARGET_AGE = max(3 * args.virtual_step * 60, 6.0)
        C.FLOW_MAX_AGE = max(12 * args.virtual_step * 60, 30.0)
        C.SPREAD_SPAN_MIN = max(2 * args.virtual_step, 0.05)
        C.SPREAD_AVG_MIN = max(12 * args.virtual_step, 1.0)

    if args.interval is not None:
        interval = args.interval
    elif use_virtual:
        interval = C.DEMO_CYCLE_SECONDS
    else:
        interval = 300

    near_exp, monthly_exp = pick_expiries(master, clock.now().date())
    if use_virtual:
        near_exp = master["expiries"][0]["date"]
        monthlies = [e["date"] for e in master["expiries"] if not e["weekly"]]
        monthly_exp = monthlies[0] if monthlies else None
    if not near_exp:
        print(r("ERROR: could not determine any NIFTY option expiry."))
        sys.exit(2)

    print(f"  near expiry : {near_exp}   monthly (V_fwd): {monthly_exp or 'n/a'}"
          f"   [{master.get('source', '')}]  lot {ctx.lot_size}")
    print(f"  interval    : {interval}s   state: {C.STATE_DB}   log dir: {C.LOG_DIR}")
    n_high = sum(1 for e in events if e["high"])
    print(f"  calendar    : {len(events)} events ({n_high} high-impact)"
          + (y(f"  [{ev_err}]") if ev_err else ""))
    print(dim("  algo: Vol(0.30)+Edge(0.30)+Trend(0.25)+Flow(0.15) · persistence 3x · "
              "macro override 6h/-2h · entry window 09:19:30-09:20:30"))

    # ------------------------------------------------------- graceful shutdown
    stopping = {"flag": False}

    def _shutdown(sig, frame):
        if stopping["flag"]:
            sys.exit(1)
        stopping["flag"] = True
        print("\n" + r(bo("  SHUTDOWN REQUESTED — squaring off all positions (live-safe).")))
        try:
            executor.square_off_all("MANUAL")
        except Exception as e:
            print(r(f"  square-off error: {e}"))
        state.close()
        loggers.audit("SHUTDOWN", signal=sig)
        print(bo(cy("═" * W)))
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ------------------------------------------------------------ main loop
    cyc = 0
    entered_today = False
    entry_msg = ""
    last_heartbeat = time.monotonic()
    last_cycle_t = time.monotonic()
    try:
        while True:
            now = clock.now()
            cyc += 1
            cycle_start = time.monotonic()
            risk._roll_day(clock)

            # ---- run detection ------------------------------------------
            try:
                res = detector.run_cycle(near_exp, monthly_exp)
            except Exception as e:
                print(r(f"  cycle {cyc} failed: {e} — retrying next interval"))
                res = detector.last_result
                if res is None:
                    if args.once:
                        break
                    clock.sleep_seconds(interval)
                    continue

            ctx.last_spot, ctx.last_vix = res.spot, res.vix
            ctx.last_regime = res.regime

            # ---- regime change -> transitions A-E -------------------------
            confirmed = detector.prev_confirmed_regime
            if confirmed and confirmed != getattr(ctx, "_applied_regime", None):
                old = getattr(ctx, "_applied_regime", None)
                if old:
                    executor.apply_transitions(old, confirmed, selector.build_env(res))
                    loggers.audit("REGIME_CHANGE", from_regime=old, to_regime=confirmed,
                                  composite=res.composite)
                ctx._applied_regime = confirmed

            # ---- entry window gate ----------------------------------------
            tnow = now.time()
            entry_state = {"msg": ""}
            if use_virtual and C.DEMO_ENTRY_ANYTIME:
                # demo: enter on the first eligible cycle of the day after the
                # strike-selection time (the 30-s window cannot be sampled by
                # 5-minute virtual steps)
                allow_entry = tnow >= C.EXEC_START_TIME and tnow <= C.TIME_EXIT_NORMAL
            else:
                allow_entry = C.EXEC_START_TIME <= tnow <= C.EXEC_END_TIME
            if not entered_today and allow_entry:
                sel = selector.evaluate(res)
                if sel.plan is not None:
                    scores = {"composite": res.composite, **res.confirmed_scores}
                    tid = executor.execute_entry(sel.plan, sel.env, res.regime, scores=scores)
                    if tid:
                        entry_msg = (f"ENTRY: {sel.strategy_name} (tid {tid}) — "
                                     f"entered today, no re-entry until next session")
                        entered_today = True
                    else:
                        entry_msg = f"ENTRY ABORTED — {sel.rationale}"
                else:
                    entry_msg = f"NO ENTRY: {sel.rationale}"
            elif entered_today:
                entry_msg = "entry already executed today — no new entries until next session"
            elif not allow_entry and tnow >= C.EXEC_END_TIME:
                entry_msg = "ENTRY WINDOW CLOSED (after 09:20:30 IST)"
            elif tnow < C.EXEC_START_TIME:
                entry_msg = "awaiting entry window (09:19:30-09:20:30 IST)"

            # ---- manage open positions ------------------------------------
            env = selector.build_env(res)
            executor.manage_positions(res, env, now)

            # ---- heartbeat checkpoint (every 10 s) ------------------------
            if time.monotonic() - last_heartbeat >= C.CHECKPOINT_HEARTBEAT_S:
                state.checkpoint({"spot": res.spot, "vix": res.vix,
                                  "scores": res.confirmed_scores,
                                  "composite": res.composite,
                                  "regime": res.regime}, reason="heartbeat")
                last_heartbeat = time.monotonic()

            # ---- console + csv log -----------------------------------------
            phase = market_phase(now)
            meta = {"data_source": args.data, "paper": C.PAPER_TRADING_MODE,
                    "last_cycle_seconds": time.monotonic() - cycle_start,
                    "day_pnl": risk.day_pnl_points}
            entry_state["msg"] = entry_msg
            print("\n" + build_report(cyc, res, selector, executor, clock, phase,
                                      entry_state, meta))
            loggers.regime_csv.append([now.isoformat(timespec="seconds"),
                                       f"{res.composite:+.3f}", res.regime,
                                       res.vol.raw, res.vol.confirmed,
                                       res.edge.raw, res.edge.confirmed,
                                       res.trend.raw, res.trend.confirmed,
                                       res.flow.raw, res.flow.confirmed,
                                       res.spot, res.vix, int(res.override)])

            if args.once:
                break

            # ---- sleep (real) or advance (virtual) --------------------------
            if use_virtual:
                clock.advance(args.virtual_step)
                n2 = clock.now()
                # roll to fresh expiries when the current one has passed
                last_exp = monthly_exp or near_exp
                if last_exp:
                    from datetime import datetime as _dt
                    try:
                        exp_d = _dt.strptime(last_exp, "%Y-%m-%d").date()
                        if n2.date() > exp_d:
                            master = client.instruments()      # regenerate expiries
                            near_exp = master["expiries"][0]["date"]
                            monthlies = [e["date"] for e in master["expiries"] if not e["weekly"]]
                            monthly_exp = monthlies[0] if monthlies else None
                            detector.futs = master.get("futs", [])
                            print(y(f"  --- rolled to new expiry set: near {near_exp} "
                                    f"monthly {monthly_exp} ---"))
                    except (ValueError, TypeError):
                        pass
                # after the session, jump straight to the next trading day 09:14
                if n2.time() >= C.TIME_EXIT_NORMAL or n2.time() < C.ENTRY_VIX_TIME:
                    d = n2.date() + timedelta(days=1)
                    while not C.is_trading_day(d):
                        d += timedelta(days=1)
                    n2 = datetime(d.year, d.month, d.day, 9, 14, 0).replace(tzinfo=n2.tzinfo)
                    clock.set(n2)
                    print(y(f"  --- next trading day {d} ({n2:%a}) ---"))
                if clock.now().date() != now.date():
                    entered_today = False
                    entry_msg = ""
                clock.sleep_seconds(0.15)
            else:
                clock.sleep_seconds(interval)

    except KeyboardInterrupt:
        pass

    # --------------------------------------------------------------- teardown
    print("\n" + bo(cy("═" * W)))
    print(bo(cy(" Engine stopped.")))
    print(f"  cycles run : {cyc}")
    print(f"  open trades: {len(executor.open_trades)}")
    if executor.open_trades:
        print(y("  NOTE: open trades are persisted in state.db — re-run to resume management."))
    if args.once or not executor.open_trades:
        try:
            executor.square_off_all("MANUAL")
        except Exception:
            pass
    loggers.audit("STOPPED", cycles=cyc)
    state.close()
    print(bo(cy("═" * W)))
    return 0


_global_clock = Clock()


if __name__ == "__main__":
    sys.exit(main())
