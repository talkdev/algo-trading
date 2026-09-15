#!/usr/bin/env python3
"""What-if scan: which structures/entry times made money on a given day."""
import sys, sqlite3
sys.path.insert(0, '/tmp')
from scan import Day, evaluate, LOT


def chain_at(day, t):
    t = day.nearest(t)
    rows = {}
    for (tt, k, ot), (bid, ask, ltp) in day.q.items():
        if tt == t:
            rows[(k, ot)] = (bid, ask, ltp)
    return t, rows


def deltas(day, t):
    """Return {(strike,type): delta} from the recorded iv/delta columns."""
    c = sqlite3.connect(DB)
    cur = c.cursor()
    cur.execute("select strike,option_type,delta,iv from option_chain_snapshot where capture_time like ?",
                (day.trading_date + 'T' + day.nearest(t) + '%',))
    out = {}
    for k, ot, d, iv in cur.fetchall():
        out[(float(k), ot)] = (d, iv)
    c.close()
    return out


def pick_by_delta(dmap, typ, target, side=None, spot=None):
    best = None
    for (k, ot), (d, iv) in dmap.items():
        if ot != typ or d is None:
            continue
        if side == 'above' and spot and k < spot:
            continue
        if side == 'below' and spot and k > spot:
            continue
        err = abs(abs(d) - target)
        if best is None or err < best[0]:
            best = (err, k)
    return best[1] if best else None


DB = sys.argv[1]
day = Day(DB)
day.trading_date = DB.split('nifty_algo_')[1][:10]

entries = sys.argv[2].split(',') if len(sys.argv) > 2 else \
    ['09:45', '10:00', '10:30', '11:00', '11:30', '12:00', '12:30', '13:00', '13:30', '14:00', '14:30']
exits = sys.argv[3].split(',') if len(sys.argv) > 3 else ['15:00', '15:20']
LOTS = int(sys.argv[4]) if len(sys.argv) > 4 else 3

print(f"{'entry':>9} {'exit':>6} {'structure':<34} {'credit':>8} {'debit':>8} {'gross':>7} {'costs':>7} {'pnl':>9}")
for te in entries:
    tt, rows = chain_at(day, te)
    spot = day.spot.get(tt)
    dm = deltas(day, te)
    atm = round(spot / 50) * 50
    sc = pick_by_delta(dm, 'call', 0.20, 'above', spot)
    wc = (sc + 150) if sc else None
    sp = pick_by_delta(dm, 'put', 0.20, 'below', spot)
    wp = (sp - 150) if sp else None
    sc_t = pick_by_delta(dm, 'call', 0.30, 'above', spot)
    sp_t = pick_by_delta(dm, 'put', 0.30, 'below', spot)
    structs = {
        'BEAR_CALL d.20/150w': [(sc, 'call', 'SELL'), (wc, 'call', 'BUY')],
        'BULL_PUT  d.20/150w': [(sp, 'put', 'SELL'), (wp, 'put', 'BUY')],
        'IRON_CONDOR d.20/150w': [(sc, 'call', 'SELL'), (wc, 'call', 'BUY'), (sp, 'put', 'SELL'), (wp, 'put', 'BUY')],
        'CONDOR_TREND(put d.30)': [(sc, 'call', 'SELL'), (wc, 'call', 'BUY'), (sp_t, 'put', 'SELL'), (sp_t - 150, 'put', 'BUY')],
        'IRON_BFLY atm/150': [(atm, 'call', 'SELL'), (atm, 'put', 'SELL'), (atm + 150, 'call', 'BUY'), (atm - 150, 'put', 'BUY')],
        'SHORT_STRADDLE atm': [(atm, 'call', 'SELL'), (atm, 'put', 'SELL')],
        'SHORT_STRANGLE d.20': [(sc, 'call', 'SELL'), (sp, 'put', 'SELL')],
        'LONG_PUT atm': [(atm, 'put', 'BUY')],
        'LONG_CALL atm': [(atm, 'call', 'BUY')],
        'BEAR_PUT d.45/.25': [(pick_by_delta(dm, 'put', 0.45, 'below', spot), 'put', 'BUY'),
                              (pick_by_delta(dm, 'put', 0.25, 'below', spot), 'put', 'SELL')],
        'LONG_PUT otm100': [(atm - 100, 'put', 'BUY')],
        'LONG_PUT otm200': [(atm - 200, 'put', 'BUY')],
    }
    for tx in exits:
        for name, legs in structs.items():
            if any(l[0] is None for l in legs):
                continue
            r = evaluate(day, legs, te, tx, LOTS)
            if r is None:
                print(f"{te:>9} {tx:>6} {name:<34} NO FILL")
                continue
            legstr = ' '.join(f"{l[2][0]}{l[0]:.0f}{l[1][0].upper()}" for l in legs)
            print(f"{r['entry_t']:>9} {r['exit_t'][:5]:>6} {name + ' ' + legstr:<52} "
                  f"{r['credit']:>8.2f} {r['debit']:>8.2f} {r['gross_pts']:>7.2f} {r['costs']:>7.0f} {r['pnl']:>9,.0f}")
    print('-' * 110)