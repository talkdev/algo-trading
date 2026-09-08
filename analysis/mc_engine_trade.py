"""
Monte Carlo of the trade THIS ENGINE is configured to place.
NIFTY 0DTE (Tuesday) Iron Condor, 2026 rules: spot ~23,900, VIX ~11, lot 65,
STT 0.15% sell-side (Budget 2026), exchange txn 0.03503%, GST 18%.

Replicates, exactly as coded in the repo:
  strategy_engine._select_strikes   (straddle x sqrt(time-remaining), 50pt round, floor)
  strategy_engine._compute_slippage (0.5x half-spread entry, 1.5x half-spread exit)
  strategy_engine._compute_costs
  execution_engine.evaluate_exit    (priority 1..7)
Then measures realised expectancy. Nothing here is taken on faith from the
docstrings - the exit ladder is re-implemented from the source.
"""
import math, random

random.seed(7)

# ── 2026 market facts ────────────────────────────────────────────────────
SPOT0      = 23900.0
LOT        = 65
STEP       = 50
VIX        = 11.0
SESSION_M  = 375            # 09:15 -> 15:30
ENTRY_M    = 35             # 09:50
HARD_EXIT_M= 345            # 15:00
EXPIRY_M   = 375            # 15:30

# ── costs (core.py / env template, verified against 2026 rates) ──────────
STT_SELL   = 0.0015
EXCH       = 0.0003552
SEBI       = 0.000001
STAMP      = 0.00003
BROK       = 20.0

# ── engine config ────────────────────────────────────────────────────────
WING            = 150
PROXIMITY_PTS   = 40
DELTA_CLOSE     = 0.40
STOP_MULT       = 2.5
PRICE_STOP_MULT = 0.30
TARGET_PCT_DTE0 = 0.50      # vix < 12


def nd(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(spot, k, t_years, iv, kind):
    """European option price + delta. t in years."""
    if t_years <= 1e-9 or iv <= 0:
        intr = max(spot - k, 0.0) if kind == "c" else max(k - spot, 0.0)
        if kind == "c":
            return intr, (1.0 if spot > k else 0.0)
        return intr, (-1.0 if spot < k else 0.0)
    sq = iv * math.sqrt(t_years)
    d1 = (math.log(spot / k) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    if kind == "c":
        return spot * nd(d1) - k * nd(d2), nd(d1)
    return k * nd(-d2) - spot * nd(-d1), nd(d1) - 1.0


def half_spread(px):
    """NIFTY 0DTE liquid-strike quote width; ticks are 0.05."""
    return max(0.05, px * 0.005)


def leg_costs(sell_val, buy_val, n_orders):
    turnover = sell_val + buy_val
    if turnover <= 0:
        return 0.0
    stt   = sell_val * STT_SELL
    exch  = turnover * EXCH
    sebi  = turnover * SEBI
    stamp = buy_val * STAMP
    brok  = BROK * n_orders
    gst   = (brok + exch + sebi) * 0.18
    return stt + exch + sebi + stamp + brok + gst


def price_condor(spot, sc, sp, lc, lp, t, iv):
    """Return (net premium of the 4-leg short condor, short-call delta, short-put delta)."""
    c_s, d_cs = bs(spot, sc, t, iv, "c")
    p_s, d_ps = bs(spot, sp, t, iv, "p")
    c_l, _    = bs(spot, lc, t, iv, "c")
    p_l, _    = bs(spot, lp, t, iv, "p")
    return (c_s + p_s) - (c_l + p_l), d_cs, d_ps


def run_one(sigma_ann, iv_ann, use_engine_geometry=True,
            target_pct=TARGET_PCT_DTE0, stop_mult=STOP_MULT,
            proximity=PROXIMITY_PTS, dist_scale=1.0):
    """Simulate one 0DTE session. Returns net rupees P&L per lot, and exit tag."""
    dt_min = 1.0
    sigma_min = sigma_ann / math.sqrt(252.0 * SESSION_M)

    # --- walk from 09:15 to entry, recording the 09:30 ATM straddle -------
    spot = SPOT0
    straddle_0930 = None
    for m in range(1, ENTRY_M + 1):
        spot *= math.exp(-0.5 * sigma_min ** 2 + sigma_min * random.gauss(0, 1))
        if m == 15:                                  # 09:30
            t = (EXPIRY_M - m) / (252.0 * SESSION_M)
            atm = round(spot / STEP) * STEP
            c, _ = bs(spot, atm, t, iv_ann, "c")
            p, _ = bs(spot, atm, t, iv_ann, "p")
            straddle_0930 = c + p

    # --- engine strike selection (strategy_engine._select_strikes) --------
    remaining_frac = max((SESSION_M - ENTRY_M) / SESSION_M, 0.05)
    time_mult = math.sqrt(remaining_frac)
    dist_mult = 1.0 * time_mult * dist_scale
    floor_pts = max(int(120 * time_mult), 55)
    short_dist = max(int(straddle_0930 * dist_mult), floor_pts)
    short_dist = int(round(short_dist / STEP) * STEP)

    sc = round((spot + short_dist) / STEP) * STEP
    sp = round((spot - short_dist) / STEP) * STEP
    lc, lp = sc + WING, sp - WING

    t_entry = (EXPIRY_M - ENTRY_M) / (252.0 * SESSION_M)
    gross, _, _ = price_condor(spot, sc, sp, lc, lp, t_entry, iv_ann)

    # entry slippage: 0.5 x half-spread per leg (engine model)
    legs_px = []
    for k, kind in ((sc, "c"), (sp, "p"), (lc, "c"), (lp, "p")):
        px, _ = bs(spot, k, t_entry, iv_ann, kind)
        legs_px.append(px)
    slip_in = sum(0.5 * half_spread(px) for px in legs_px)

    sell_val = (legs_px[0] + legs_px[1]) * LOT
    buy_val  = (legs_px[2] + legs_px[3]) * LOT
    cost_in  = leg_costs(sell_val, buy_val, 4)
    cost_in_pts = cost_in / LOT

    net_credit = gross - slip_in - cost_in_pts
    if net_credit <= 0:
        return None

    price_stop_pts = max(int(straddle_0930 * PRICE_STOP_MULT), 30)
    ps_call = sc - price_stop_pts
    ps_put  = sp + price_stop_pts
    stop_prem   = net_credit * stop_mult
    target_prem = net_credit * (1.0 - target_pct)

    profit_locked = False
    lock_level = None

    # --- monitor 09:50 -> 15:00 ------------------------------------------
    for m in range(ENTRY_M + 1, HARD_EXIT_M + 1):
        spot *= math.exp(-0.5 * sigma_min ** 2 + sigma_min * random.gauss(0, 1))
        t = max((EXPIRY_M - m) / (252.0 * SESSION_M), 1e-9)
        prem, d_cs, d_ps = price_condor(spot, sc, sp, lc, lp, t, iv_ann)
        tag = None

        # P1 delta breach
        if abs(d_cs) > DELTA_CLOSE or abs(d_ps) > DELTA_CLOSE:
            tag = "DELTA"
        # P2 spot proximity
        elif abs(spot - sc) <= proximity or abs(spot - sp) <= proximity:
            tag = "PROXIMITY"
        # P3 price stop / premium stop
        elif spot >= ps_call or spot <= ps_put:
            tag = "PRICE_STOP"
        elif prem >= stop_prem:
            tag = "PREM_STOP"
        else:
            # P4 profit lock -> breakeven stop
            if gross > 0:
                pp = (gross - prem) / gross
                if pp >= 0.40 and not profit_locked:
                    profit_locked = True
                    lock_level = net_credit
                elif profit_locked and prem >= lock_level:
                    tag = "LOCK_STOP"
            # P6 time targets
            if tag is None:
                eff = target_prem
                if m >= 255:                      # 13:30
                    eff = min(eff, net_credit * 0.60)
                if m >= 315:                      # 14:30
                    eff = min(eff, net_credit * 0.70)
                if prem <= eff:
                    tag = "TARGET"

        if tag:
            return settle(spot, sc, sp, lc, lp, t, iv_ann, prem, net_credit,
                          is_stop=tag in ("DELTA", "PROXIMITY", "PRICE_STOP",
                                          "PREM_STOP")), tag

    # P7 hard exit 15:00
    t = (EXPIRY_M - HARD_EXIT_M) / (252.0 * SESSION_M)
    prem, _, _ = price_condor(spot, sc, sp, lc, lp, t, iv_ann)
    return settle(spot, sc, sp, lc, lp, t, iv_ann, prem, net_credit,
                  is_stop=False), "HARD_EXIT"


def settle(spot, sc, sp, lc, lp, t, iv, prem, net_credit, is_stop):
    """Close the 4 legs, pay exit slippage + costs, return rupees per lot."""
    legs = []
    for k, kind in ((sc, "c"), (sp, "p"), (lc, "c"), (lp, "p")):
        px, _ = bs(spot, k, t, iv, kind)
        legs.append(px)
    mult = 1.5 if is_stop else 1.5      # engine uses 1.5x on every exit
    slip_out = sum(mult * half_spread(px) for px in legs)
    buy_val  = (legs[0] + legs[1]) * LOT      # buying back the shorts
    sell_val = (legs[2] + legs[3]) * LOT      # selling the wings
    cost_out = leg_costs(sell_val, buy_val, 4)
    pnl_pts = net_credit - prem - slip_out
    return pnl_pts * LOT - cost_out


def summarise(name, results):
    pnls = [r[0] for r in results]
    tags = [r[1] for r in results]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    print(f"\n=== {name} ===")
    print(f"  trades            {n}")
    print(f"  win rate          {len(wins)/n*100:5.1f}%")
    print(f"  avg win           Rs {sum(wins)/len(wins):9,.0f}" if wins else "  avg win  n/a")
    print(f"  avg loss          Rs {sum(loss)/len(loss):9,.0f}" if loss else "  avg loss n/a")
    print(f"  EXPECTANCY/lot    Rs {sum(pnls)/n:9,.0f}")
    print(f"  worst             Rs {min(pnls):9,.0f}")
    order = ["TARGET", "HARD_EXIT", "LOCK_STOP", "PROXIMITY", "DELTA",
             "PRICE_STOP", "PREM_STOP"]
    dist = {t: tags.count(t) for t in order if tags.count(t)}
    print("  exits             " + ", ".join(f"{k}={v/n*100:.0f}%" for k, v in dist.items()))
    return sum(pnls) / n


if __name__ == "__main__":
    N = 4000
    # VIX 11 -> realised ann. vol ~10.5%; 0DTE ATM IV runs richer than VIX
    SIGMA, IV = 0.105, 0.125

    print("NIFTY 0DTE Iron Condor | spot 23,900 | VIX 11 | lot 65 | 2026 costs")
    print(f"realised vol {SIGMA:.1%}  |  ATM IV {IV:.1%}  |  VRP = {(IV-SIGMA)*100:.1f}pp (a GENUINELY rich day)")

    base = [r for r in (run_one(SIGMA, IV) for _ in range(N)) if r]
    summarise("AS CONFIGURED (engine geometry: 40pt proximity, 0.40 delta, 50% target)", base)

    wide = [r for r in (run_one(SIGMA, IV, dist_scale=1.6) for _ in range(N)) if r]
    summarise("Strikes 1.6x further out (~0.10 delta), same exit ladder", wide)

    nopx = [r for r in (run_one(SIGMA, IV, proximity=0, dist_scale=1.6) for _ in range(N)) if r]
    summarise("1.6x strikes + proximity stop REMOVED (structural stop only)", nopx)

    t25 = [r for r in (run_one(SIGMA, IV, proximity=0, dist_scale=1.6,
                               target_pct=0.25) for _ in range(N)) if r]
    summarise("1.6x strikes, no proximity stop, target cut to 25% of credit", t25)
