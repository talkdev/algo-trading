import sqlite3
from pathlib import Path

p = Path("data/nifty_algo_v3.db")
# prefer per_day if exists
pd = Path("data/per_day/nifty_algo_2026-11-04.db")
if pd.exists():
    p = pd
print("db", p)
con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

rows = list(
    con.execute(
        """
        SELECT substr(candle_time,1,5) t, open, high, low, close
        FROM intraday_candles
        WHERE trading_date='2026-11-04' AND interval_min=1
          AND candle_time>='09:15' AND substr(candle_time,1,5)<'15:30'
        ORDER BY candle_time
        """
    )
)
bars = {
    str(r["t"]): (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    for r in rows
}
print("1m bars", len(bars), "first", min(bars), "last", max(bars))
orc = [v for t, v in sorted(bars.items()) if t <= "09:44"]
orh = max(v[1] for v in orc)
orl = min(v[2] for v in orc)
mid = (orh + orl) / 2
width = orh - orl
or_loc = (orc[-1][3] - orl) / width if width else 0.5
print(f"ORH={orh:.2f} ORL={orl:.2f} mid={mid:.2f} width={width:.2f} or_loc={or_loc:.3f} lastOR={orc[-1][3]:.2f}")
print("exhaust?", or_loc >= 0.85 and width <= 120)
print("spent_put eligible width>=150?", width >= 150)

five = {}
for t, (o, h, l, c) in sorted(bars.items()):
    m = int(t[:2]) * 60 + int(t[3:5]) - 555
    k = (m // 5) * 5
    b = five.get(k)
    five[k] = [o, h, l, c] if b is None else [b[0], max(b[1], h), min(b[2], l), c]

e9 = e21 = None
call_blocked = False
first = None
events = []
for k in sorted(five):
    c = five[k][3]
    e9 = c if e9 is None else e9 + 0.2 * (c - e9)
    e21 = c if e21 is None else e21 + (2 / 22) * (c - e21)
    m_close = k + 5 + 555
    close_t = f"{m_close // 60:02d}:{m_close % 60:02d}"
    hi = five[k][1]
    lo = five[k][2]
    if not ("09:45" <= close_t <= "13:30"):
        continue
    # track proximity
    if first is None:
        if c > orh and e9 > e21 and c > e21:
            ext = c - orh
            weak = (
                close_t <= "11:00"
                and 0.40 <= or_loc <= 0.60
                and width > 0
                and (ext / width) < 0.05
            )
            if call_blocked or (or_loc >= 0.85 and width <= 120) or weak:
                call_blocked = True
                events.append(("SKIP_CALL", close_t, round(c, 2), round(ext, 2), "weak" if weak else "exhaust/block"))
            else:
                first = ("CALL", close_t, c)
                events.append(("TAKE_CALL", close_t, round(c, 2), round(e9 - e21, 2)))
        elif c < orl and e9 < e21 and c < e21:
            spent = width >= 150 and "10:15" <= close_t <= "11:00"
            if spent:
                events.append(("SKIP_PUT_spent", close_t, round(c, 2), round(orl - c, 2)))
            else:
                first = ("PUT", close_t, c)
                events.append(("TAKE_PUT", close_t, round(c, 2), round(e9 - e21, 2)))
        else:
            # notable bars near extremes or during user windows
            if (
                close_t in ("10:15", "10:20", "10:25", "10:30", "11:00", "11:30", "12:00", "12:15", "12:30", "13:00", "13:15", "13:30")
                or abs(c - orh) < 30
                or abs(c - orl) < 30
                or lo < orl
                or hi > orh
            ):
                events.append(
                    (
                        "near",
                        close_t,
                        round(c, 2),
                        round(hi, 2),
                        round(lo, 2),
                        f"vsORH={c-orh:.1f}",
                        f"vsORL={orl-c:.1f}",
                        f"EMAspread={e9-e21:.1f}",
                        "bull" if e9 > e21 else "bear",
                    )
                )

print("first signal", first)
print("call_blocked", call_blocked)
print("events (selected):")
for e in events:
    print(" ", e)

# day extremes vs OR
mx = max(bars.items(), key=lambda x: x[1][1])
mn = min(bars.items(), key=lambda x: x[1][2])
print("day max high", mx[0], mx[1][1], "vs ORH", orh, "delta", mx[1][1] - orh)
print("day min low", mn[0], mn[1][2], "vs ORL", orl, "delta", orl - mn[1][2])

# user windows
print("\nUser bear window 10:19-12:10 lows/closes:")
for t, (o, h, l, c) in sorted(bars.items()):
    if "10:19" <= t <= "12:10":
        if l <= orl + 5 or t in ("10:19", "10:30", "11:00", "11:30", "12:00", "12:10"):
            print(f"  {t} H={h:.1f} L={l:.1f} C={c:.1f}")

print("\nUser bull window 12:15-13:40 highs/closes:")
for t, (o, h, l, c) in sorted(bars.items()):
    if "12:15" <= t <= "13:40":
        if h >= orh - 5 or t in ("12:15", "12:30", "13:00", "13:15", "13:30", "13:40"):
            print(f"  {t} H={h:.1f} L={l:.1f} C={c:.1f}")

# decisions summary
try:
    for r in con.execute(
        """
        SELECT reason, COUNT(*) c FROM strategy_decisions
        WHERE trading_date='2026-11-04' GROUP BY 1 ORDER BY 2 DESC
        """
    ):
        print("decision", dict(r))
except Exception as e:
    print("decisions", e)
try:
    ds = con.execute(
        "SELECT trades_executed, net_pnl_rupees, no_trade_reasons_json, or_width, nifty_high, nifty_low, nifty_open, nifty_close FROM daily_summary WHERE trading_date='2026-11-04'"
    ).fetchone()
    if ds:
        print("daily_summary", dict(ds))
except Exception as e:
    print("daily", e)

con.close()
