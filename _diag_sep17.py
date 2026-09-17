#!/usr/bin/env python3
"""Diagnose 2026-09-17 tape, IC marks, and fade candidates. Not engine code."""
import sqlite3
from pathlib import Path

DB = Path(r"C:\Users\Administrator\Desktop\algo-trading\data\nifty_algo_v3.db")
c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
LOT = 65


def q(sql, args=()):
    return c.execute(sql, args)


print("=== session_state ===")
try:
    for r in q(
        "SELECT trading_date, actual_dte, day_label, day_mode, "
        "hard_exit_time, entry_start, entry_end, wing_width "
        "FROM session_state WHERE trading_date='2026-09-17' "
        "ORDER BY updated_at DESC LIMIT 2"
    ):
        print(dict(r))
except Exception as e:
    print("session_state", e)

print("\n=== live positions ===")
try:
    cols = [x[1] for x in q("PRAGMA table_info(positions)")]
    want = [k for k in (
        "strategy_name", "entry_time", "exit_time", "exit_reason", "status",
        "final_lots", "entry_credit", "net_pnl_rupees", "entry_spot",
        "actual_dte", "vol_regime_at_entry", "price_regime_at_entry",
        "final_regime_at_entry", "selection_reason",
    ) if k in cols]
    for r in q(
        f"SELECT {', '.join(want)} FROM positions WHERE trading_date='2026-09-17'"
    ):
        print(dict(r))
except Exception as e:
    print("positions", e)

print("\n=== spot path ===")
spots = q(
    """
    SELECT substr(capture_time,12,5) t,
           MIN(spot_at_capture) lo, MAX(spot_at_capture) hi,
           AVG(spot_at_capture) avg
    FROM option_chain_snapshot
    WHERE trading_date='2026-09-17'
      AND substr(capture_time,12,5) BETWEEN '09:15' AND '15:30'
    GROUP BY 1 ORDER BY 1
    """
).fetchall()
keys = {
    "09:15", "09:20", "09:30", "09:35", "09:45", "09:50", "10:00", "10:15",
    "10:30", "10:35", "11:00", "11:30", "12:00", "12:15", "12:30", "12:45",
    "13:00", "13:15", "13:30", "14:00", "14:30", "15:00", "15:15",
}
print(f"minutes={len(spots)}")
running_hi = -1e9
running_lo = 1e9
for r in spots:
    running_hi = max(running_hi, r["hi"])
    running_lo = min(running_lo, r["lo"])
    if r["t"] in keys or r["t"].endswith(":00"):
        print(
            f"  {r['t']} avg={r['avg']:.1f} bar={r['lo']:.1f}-{r['hi']:.1f} "
            f"day={running_lo:.1f}-{running_hi:.1f} rng={running_hi-running_lo:.0f}"
        )

print("\n=== day min/max ===")
print(tuple(q(
    "SELECT MIN(spot_at_capture), MAX(spot_at_capture), "
    "MIN(vix_at_capture), MAX(vix_at_capture), COUNT(DISTINCT capture_time) "
    "FROM option_chain_snapshot WHERE trading_date='2026-09-17' "
    "AND substr(capture_time,12,5) BETWEEN '09:15' AND '15:30'"
).fetchone()))

print("\n=== option_type sample ===")
print([r[0] for r in q(
    "SELECT DISTINCT option_type FROM option_chain_snapshot "
    "WHERE trading_date='2026-09-17' LIMIT 10"
)])


def mid(strike, ot, tp):
    r = q(
        """
        SELECT capture_time, spot_at_capture, bid, ask, ltp,
               ((COALESCE(NULLIF(bid,0), ltp) + COALESCE(NULLIF(ask,0), ltp))/2.0) m
        FROM option_chain_snapshot
        WHERE trading_date='2026-09-17' AND strike=? AND option_type=?
          AND capture_time LIKE ?
        ORDER BY capture_time LIMIT 1
        """,
        (float(strike), ot, f"2026-09-17T{tp}%"),
    ).fetchone()
    return r


def credit(legs, tp):
    total = 0.0
    spot = None
    for k, ot, sgn in legs:
        r = mid(k, ot, tp)
        if not r or r["m"] is None:
            return None, None
        total += sgn * float(r["m"])
        spot = float(r["spot_at_capture"])
    return total, spot


IC = [(23500, "call", 1), (23650, "call", -1), (22900, "put", -1), (23050, "put", 1)]
print("\n=== IC 23500/23650 C + 23050/22900 P vs fill 48.03 @3 lots ===")
entry = 48.03
best = None
for h in range(10, 16):
    for m in (0, 10, 15, 20, 30, 35, 45):
        if h == 10 and m < 35:
            continue
        if h == 15 and m > 15:
            continue
        tp = f"{h:02d}:{m:02d}"
        val, sp = credit(IC, tp)
        if val is None:
            continue
        pts = entry - val
        rs = pts * LOT * 3 - 250
        rec = (rs, tp, sp, val, pts)
        if best is None or rs > best[0]:
            best = rec
        if m in (0, 30) or tp in ("10:35", "11:00", "12:20", "12:30", "13:00", "13:30"):
            print(f"  {tp} spot={sp:.0f} struct={val:.2f} pts={pts:+.2f} Rs={rs:.0f}")
print("BEST IC", best)

print("\n=== fade BCS / BPS around 12:20-13:30 hold to 14:00 / 15:15 ===")
structs = [
    ("BCS_23450_23650", [(23450, "call", 1), (23650, "call", -1)]),
    ("BCS_23500_23700", [(23500, "call", 1), (23700, "call", -1)]),
    ("BCS_23500_23650", [(23500, "call", 1), (23650, "call", -1)]),
    ("BCS_23550_23750", [(23550, "call", 1), (23750, "call", -1)]),
    ("BCS_23600_23800", [(23600, "call", 1), (23800, "call", -1)]),
    ("BPS_23100_22800", [(23100, "put", 1), (22800, "put", -1)]),
    ("BPS_23050_22750", [(23050, "put", 1), (22750, "put", -1)]),
    ("LONG_P_23300", [(23300, "put", -1)]),  # debit: sign -1 buy
    ("LONG_C_23250", [(23250, "call", -1)]),
]
for name, legs in structs:
    for et in ("11:00", "12:20", "12:30", "12:45", "13:00", "13:15"):
        e, es = credit(legs, et)
        if e is None:
            continue
        for xt in ("13:30", "14:00", "14:30", "15:15"):
            x, xs = credit(legs, xt)
            if x is None:
                continue
            # for credit structures (sell +1) pts = entry_credit - exit_credit
            # for long (buy -1) pts = -entry - (-exit) = exit - entry = -e + x wait
            # credit() with sell +1 buy -1: entry positive for credit, negative for debit
            pts = e - x
            lots = 4 if "BCS" in name or "BPS" in name else 3
            rs = pts * LOT * lots - 180
            if xt in ("14:00", "15:15") or (et == "12:30" and xt == "13:30"):
                print(
                    f"  {name} {et}@{es:.0f} c={e:.1f} -> {xt}@{xs:.0f} "
                    f"x={x:.1f} pts={pts:+.1f} Rs{lots}={rs:.0f}"
                )

print("\n=== chain columns for regime (if any) ===")
row = q(
    "SELECT * FROM option_chain_snapshot WHERE trading_date='2026-09-17' LIMIT 1"
).fetchone()
print("keys", list(row.keys())[:40])
