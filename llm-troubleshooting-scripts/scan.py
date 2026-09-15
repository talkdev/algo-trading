#!/usr/bin/env python3
"""What-if structure scanner over recorded option_chain_snapshot data."""
import sqlite3, sys, bisect
from collections import defaultdict

LOT = 65
STT_SELL = 0.0015
BROK = 20.0
EXCH = 0.0003553
SEBI = 0.000001
STAMP = 0.00003
EDGE = 0.25
STRESS = 0.5


class Day:
    def __init__(self, db):
        c = sqlite3.connect(db)
        cur = c.cursor()
        cur.execute("select capture_time,strike,option_type,bid,ask,ltp,spot_at_capture,vix_at_capture "
                    "from option_chain_snapshot order by capture_time")
        self.q = {}
        self.spot = {}
        self.vix = {}
        times = []
        last = None
        for ct, k, ot, bid, ask, ltp, sp, vx in cur.fetchall():
            t = ct[11:19]
            if t != last:
                times.append(t); last = t
            self.q[(t, float(k), ot)] = (bid or 0.0, ask or 0.0, ltp or 0.0)
            if sp: self.spot[t] = sp
            if vx: self.vix[t] = vx
        self.times = times
        c.close()

    def fill(self, t, k, ot, action, urgent=False):
        bid, ask, ltp = self.q.get((t, float(k), ot), (0, 0, 0))
        if bid <= 0 or ask <= 0 or ask < bid:
            return ltp if ltp > 0 else None
        mid = (bid + ask) / 2.0
        e = EDGE * (STRESS if urgent else 1.0)
        if action == 'SELL':
            return round(bid + (mid - bid) * (e / 0.5), 2)
        return round(ask - (ask - mid) * (e / 0.5), 2)

    def nearest(self, t):
        i = bisect.bisect_left(self.times, t)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(self.times):
                if best is None or abs(self.times[j] < t) < 0:
                    pass
        # pick closest by string distance
        cands = [self.times[j] for j in (i - 1, i) if 0 <= j < len(self.times)]
        if not cands:
            cands = [self.times[0]]
        return min(cands, key=lambda x: abs(int(x[:2]) * 3600 + int(x[3:5]) * 60 + int(x[6:8])
                                            - (int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8]))))

    def costs(self, legs, lots):
        sell = buy = 0.0
        for leg in legs:
            pv = leg['px'] * lots * LOT
            if leg['action'] == 'SELL':
                sell += pv
            else:
                buy += pv
        to = sell + buy
        if to <= 0:
            return 0.0
        stt = sell * STT_SELL
        exch = to * EXCH
        sebi = to * SEBI
        stamp = buy * STAMP
        brok = BROK * len(legs)
        gst = (brok + exch + sebi) * 0.18
        return round(stt + exch + sebi + stamp + brok + gst, 2)


def evaluate(day, legs_spec, t_entry, t_exit, lots, urgent_exit=False):
    """legs_spec: list of (strike, 'call'/'put', 'SELL'/'BUY')"""
    te = day.nearest(t_entry); tx = day.nearest(t_exit)
    ent = []
    for k, ot, act in legs_spec:
        px = day.fill(te, k, ot, act)
        if px is None or px <= 0:
            return None
        ent.append({'k': k, 'ot': ot, 'action': act, 'px': px})
    ex = []
    for leg in ent:
        ca = 'BUY' if leg['action'] == 'SELL' else 'SELL'
        px = day.fill(tx, leg['k'], leg['ot'], ca, urgent=urgent_exit)
        if px is None or px <= 0:
            px = leg['px']
        ex.append({'k': leg['k'], 'ot': leg['ot'], 'action': ca, 'px': px})
    credit = sum(l['px'] if l['action'] == 'SELL' else -l['px'] for l in ent)
    debit = sum(l['px'] if l['action'] == 'BUY' else -l['px'] for l in ex)
    gross = credit - debit
    costs = day.costs(ent, lots) + day.costs(ex, lots)
    pnl = gross * LOT * lots - costs
    return {'entry_t': te, 'exit_t': tx, 'credit': round(credit, 2), 'debit': round(debit, 2),
            'gross_pts': round(gross, 2), 'costs': round(costs, 2), 'pnl': round(pnl, 2),
            'lots': lots, 'spot_e': day.spot.get(te), 'spot_x': day.spot.get(tx)}


if __name__ == '__main__':
    db = sys.argv[1]
    day = Day(db)
    print("cycles:", len(day.times), day.times[0], day.times[-1])