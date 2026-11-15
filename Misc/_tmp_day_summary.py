import json
import sqlite3
from pathlib import Path

days = [
    "2026-09-16",
    "2026-09-29",
    "2026-10-13",
    "2026-10-22",
    "2026-10-26",
    "2026-11-04",
    "2026-11-05",
    "2026-11-09",
    "2026-11-12",
    "2026-11-13",
]
per = Path("data/per_day")

keys = [
    "trading_date",
    "net_pnl_rupees",
    "gross_pnl_rupees",
    "trades_executed",
    "trades_won",
    "trades_lost",
    "stops_fired",
    "or_condition",
    "or_width",
    "dominant_vol_regime",
    "dominant_price_regime",
    "dominant_final_regime",
    "strategies_used_json",
    "day_range_points",
    "realized_move",
    "straddle_ratio",
    "opening_straddle",
    "vix_open",
    "vix_close",
    "nifty_open",
    "nifty_close",
    "nifty_low",
    "nifty_high",
    "avg_hold_minutes",
    "max_intraday_drawdown",
]

print("DATE | net_pnl | exec | W/L | stops | OR | vol | price | final | strats | range | straddle_ratio")
for d in days:
    p = per / f"nifty_algo_{d}.db"
    if not p.exists():
        print(d, "NO SHARD")
        continue
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ts = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "daily_summary" not in ts:
        print(d, "no daily_summary")
        con.close()
        continue
    row = con.execute(
        "SELECT * FROM daily_summary WHERE trading_date=?", (d,)
    ).fetchone()
    if not row:
        print(d, "empty daily_summary")
        con.close()
        continue
    r = dict(row)
    # top no_trade reasons
    ntr = {}
    try:
        ntr = json.loads(r.get("no_trade_reasons_json") or "{}")
    except Exception:
        pass
    top = sorted(ntr.items(), key=lambda x: -x[1])[:5]
    top_s = "; ".join(f"{k.split('|')[0][:60]}={v}" for k, v in top)

    # closed positions
    closed = []
    if "positions" in ts:
        for pos in con.execute(
            "SELECT strategy_name, status, exit_reason, net_pnl_rupees, "
            "entry_time, exit_time, final_lots, paper_trade "
            "FROM positions WHERE trading_date=? "
            "AND status IN ('CLOSED','STOPPED','HARD_EXIT','ABORTED') "
            "ORDER BY entry_time",
            (d,),
        ):
            closed.append(dict(pos))
        # also any with net pnl
        if not closed:
            for pos in con.execute(
                "SELECT strategy_name, status, exit_reason, net_pnl_rupees, "
                "entry_time, exit_time, final_lots, paper_trade "
                "FROM positions WHERE trading_date=? ORDER BY entry_time",
                (d,),
            ):
                closed.append(dict(pos))

    print("-" * 100)
    print(
        f"{d} net={r.get('net_pnl_rupees')} exec={r.get('trades_executed')} "
        f"W/L={r.get('trades_won')}/{r.get('trades_lost')} stops={r.get('stops_fired')} "
        f"OR={r.get('or_condition')}({r.get('or_width'):.0f}) "
        f"vol={r.get('dominant_vol_regime')} price={r.get('dominant_price_regime')} "
        f"final={r.get('dominant_final_regime')}"
    )
    print(
        f"  strats={r.get('strategies_used_json')} range={r.get('day_range_points')} "
        f"realized={r.get('realized_move')} straddle_r={r.get('straddle_ratio')} "
        f"vix={r.get('vix_open')}->{r.get('vix_close')} "
        f"nifty={r.get('nifty_open')}->{r.get('nifty_close')} "
        f"HL={r.get('nifty_low')}/{r.get('nifty_high')} hold={r.get('avg_hold_minutes')} "
        f"dd={r.get('max_intraday_drawdown')}"
    )
    print(f"  top_blockers: {top_s}")
    # filled only
    filled = [
        c
        for c in closed
        if c.get("status") not in ("ABORTED",)
        and c.get("net_pnl_rupees") is not None
    ]
    aborted = [c for c in closed if c.get("status") == "ABORTED"]
    print(f"  positions filled={len(filled)} aborted={len(aborted)} total_rows={len(closed)}")
    for c in filled[:12]:
        print(
            f"    {c.get('entry_time','')[11:16]} {c.get('strategy_name')} "
            f"status={c.get('status')} exit={c.get('exit_reason')} "
            f"pnl={c.get('net_pnl_rupees')} lots={c.get('final_lots')} "
            f"paper={c.get('paper_trade')}"
        )
    if aborted:
        from collections import Counter

        ac = Counter(a.get("strategy_name") for a in aborted)
        print(f"    ABORTED counts: {dict(ac)}")
        # sample first abort reason
        a0 = aborted[0]
        print(
            f"    first abort: {a0.get('entry_time','')[11:16]} "
            f"{a0.get('strategy_name')} {a0.get('exit_reason')}"
        )
    con.close()
