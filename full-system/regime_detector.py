# -*- coding: utf-8 -*-
"""
regime_detector.py — the 8-step vol-regime algorithm (spec §4), executed every
5 minutes on the 5-minute candle close.

  Step 0  Data windowing & quality filter (OI >= MIN_OI, bid > 0)
  Step 1  Vol_Score  = 0.5*Term_Spread + 0.5*Skew_z
  Step 2  Edge_Score = IV_ATM - RV(20d)  -> +1 rich / -1 cheap / 0 fair
  Step 3  Trend_Score= ADX(14) + 50-EMA slope on 5-min bars
  Step 4  Flow_Score = net delta-weighted OI change (15 min) vs spread ratio
  Step 5  Persistence filter: 3 consecutive identical readings to confirm
  Step 6  Macro override: high-impact event -> EVENT_HEDGE (6h before / 2h after)
  Step 7  Composite = 0.30*Vol + 0.30*Edge + 0.25*Trend + 0.15*Flow
  Step 8  Regime mapping with the 5 labelled bands.

A *confirmed* regime change additionally requires 3 consecutive 5-min readings
in the same zone (spec: 'Requires 3 consecutive readings (15 minutes)').
"""
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config as C
from common_utils import IST
from common_utils import (bs_delta, ema_series, adx14, realised_vol_pct,
                        years_to_expiry)
from common_utilsimport fx


@dataclass
class ModuleResult:
    raw: float | None = None
    confirmed: float | None = None
    detail: str = ""
    notes: list = field(default_factory=list)


@dataclass
class RegimeResult:
    ts: datetime
    spot: float | None
    vix: float | None
    vol: ModuleResult
    edge: ModuleResult
    trend: ModuleResult
    flow: ModuleResult
    composite: float
    raw_composite: float
    regime: str
    prev_regime: str | None
    override: bool
    macro_text: str
    near_expiry: str | None
    monthly_expiry: str | None
    rv: float | None
    iv_atm: float | None
    pcr: float | None
    fut_basis: float | None
    notes: list = field(default_factory=list)
    # raw data carried for the strategy selector / monitors
    chain_near: list = field(default_factory=list)
    chain_monthly: list = field(default_factory=list)
    bars5: list = field(default_factory=list)
    daily: list = field(default_factory=list)
    vix_series: list = field(default_factory=list)

    @property
    def confirmed_scores(self):
        return {"vol": self.vol.confirmed, "edge": self.edge.confirmed,
                "trend": self.trend.confirmed, "flow": self.flow.confirmed}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def quality(leg, min_oi):
    return (leg["oi"] is not None and leg["oi"] >= min_oi
            and leg["bid"] is not None and leg["bid"] > 0
            and leg["ask"] is not None and leg["ask"] > 0)


def leg_delta(leg, spot, strike, T, rate, is_call):
    d = leg.get("delta")
    if d is not None and 0.01 < abs(d) < 0.99:
        return d
    if leg.get("iv"):
        return bs_delta(spot, strike, T, leg["iv"], rate, is_call)
    return None


def atm_strike(chain, spot):
    if not chain:
        return None
    return min(chain, key=lambda s: abs(s["strike"] - spot))


def expiry_dt(expiry_iso):
    try:
        return datetime.strptime(expiry_iso, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Step 1 — Volatility surface
# ---------------------------------------------------------------------------
def module_vol(chain_near, chain_monthly, vix_spot, state, now, near_expiry):
    notes = []
    if not chain_near:
        return None, False, "no option chain", ["chain unavailable"]
    spots = [s["spot"] for s in chain_near if s["spot"]]
    spot = spots[0] if spots else None
    if not spot:
        return None, False, "no spot in chain", ["chain missing underlying_spot_price"]

    # term spread: forward IV (next monthly ATM straddle) - spot VIX
    v_fwd = None
    if chain_monthly:
        atmm = atm_strike(chain_monthly, spot)
        ivs = [v for v in (atmm["c"]["iv"], atmm["p"]["iv"]) if v and v > 0] if atmm else []
        if ivs:
            v_fwd = statistics.mean(ivs)
    if v_fwd is None:
        notes.append("forward VIX unavailable -> Term_Score neutral")
        t_spread, term_score = None, 0
    else:
        t_spread = v_fwd - vix_spot
        term_score = 1 if t_spread > C.TERM_THRESHOLD else (-1 if t_spread < -C.TERM_THRESHOLD else 0)
    term_txt = (f"T_spread {t_spread:+.2f} " + {1: "CONTANGO", -1: "BACKWARDATION", 0: "FLAT"}[term_score]) \
        if t_spread is not None else "T_spread n/a"

    # 25-delta skew (Put IV - Call IV) with 30-day z-score
    T = years_to_expiry(expiry_dt(near_expiry).date() if expiry_dt(near_expiry) else None, now)
    best_c = best_p = None
    for s in chain_near:
        if quality(s["c"], C.MIN_OI) and s["c"]["iv"]:
            d = leg_delta(s["c"], spot, s["strike"], T, C.RISK_FREE, True)
            if d is not None and (best_c is None or abs(d - 0.25) < abs(best_c[0] - 0.25)):
                best_c = (d, s)
        if quality(s["p"], C.MIN_OI) and s["p"]["iv"]:
            d = leg_delta(s["p"], spot, s["strike"], T, C.RISK_FREE, False)
            if d is not None and (best_p is None or abs(d + 0.25) < abs(best_p[0] + 0.25)):
                best_p = (d, s)
    skew = z = skew_score = None
    if best_c and best_p and abs(best_c[0] - 0.25) < 0.15 and abs(best_p[0] + 0.25) < 0.15:
        skew = best_p[1]["p"]["iv"] - best_c[1]["c"]["iv"]
        day = now.date().isoformat()
        state.record_skew(skew, day)
        hist = state.skew_series(exclude_day=day, limit=C.SKEW_HISTORY_DAYS)
        if len(hist) >= C.SKEW_MIN_DAYS:
            sd = statistics.stdev(hist)
            if sd >= 1e-9:
                z = (skew - statistics.mean(hist)) / sd
                skew_score = -1 if z > C.SKEW_Z_STEEP else (1 if z < C.SKEW_Z_FLAT else 0)
                skew_txt = f"skew25 {skew:+.2f} (z {z:+.2f}, {len(hist)}d)"
            else:
                skew_txt = f"skew25 {skew:+.2f} (flat history)"
        else:
            skew_txt = f"skew25 {skew:+.2f} (z warming {len(hist)}/{C.SKEW_MIN_DAYS}d)"
    else:
        skew_txt = "skew25 n/a"
        notes.append("25-delta legs not found (illiquid chain?)")

    vol_score = 0.5 * (term_score or 0) + 0.5 * (skew_score or 0)
    detail = f"{term_txt} | {skew_txt}"
    return vol_score, True, detail, notes


# ---------------------------------------------------------------------------
# Step 2 — Realised vs implied edge
# ---------------------------------------------------------------------------
def module_edge(chain_near, rv, spot):
    notes = []
    if rv is None:
        return None, False, "RV unavailable", ["insufficient daily history"]
    if not chain_near:
        return None, False, "no chain", ["chain unavailable"]
    atm = atm_strike(chain_near, spot)
    ivs = [v for v in (atm["c"]["iv"], atm["p"]["iv"]) if v and v > 0] if atm else []
    if not ivs:
        return None, False, "ATM IV unavailable", ["ATM IV missing/degenerate"]
    iv_atm = statistics.mean(ivs)
    edge = iv_atm - rv
    raw = 1 if edge > C.EDGE_RICH else (-1 if edge < C.EDGE_CHEAP else 0)
    tag = "RICH (seller's edge)" if raw == 1 else ("CHEAP (buyer's edge)" if raw == -1 else "FAIR")
    detail = f"IV_atm {iv_atm:.2f}% - RV{C.RV_WINDOW} {rv:.2f}% = {edge:+.2f} -> {tag}"
    return raw, True, detail, notes


# ---------------------------------------------------------------------------
# Step 3 — Trend & momentum
# ---------------------------------------------------------------------------
def module_trend(bars5, spot):
    notes = []
    if len(bars5) < C.TREND_BARS_REQUIRED:
        return None, False, f"only {len(bars5)} 5-min bars", [f"need >= {C.TREND_BARS_REQUIRED} bars"]
    closes = [b["c"] for b in bars5]
    ax = adx14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in bars5])
    ema = ema_series(closes, 50)
    if ax is None or len(ema) < 21:
        return None, False, "indicator warmup", ["not enough bars"]
    adx_v, pdi, ndi = ax
    slope = ema[-1] - ema[-21]
    slope_pct = slope / spot * 100.0 if spot else 0.0
    above = spot > ema[-1]
    if adx_v > C.TREND_ADX and abs(slope_pct) > C.EMA_SLOPE_MIN_PCT:
        raw = 1 if above else -1
        dirn = "bullish" if above else "bearish"
    else:
        raw = 0
        dirn = "range-bound"
    detail = (f"ADX {adx_v:.1f} (+DI {pdi:.0f}/-DI {ndi:.0f}) | "
              f"EMA50 slope {slope_pct:+.3f}% | spot {'>' if above else '<'} EMA50 -> {dirn}")
    return raw, True, detail, notes


# ---------------------------------------------------------------------------
# Step 4 — Order flow & microstructure
# ---------------------------------------------------------------------------
def module_flow(chain_near, state, now, near_expiry):
    notes = []
    if not chain_near:
        return None, False, "no chain", ["chain unavailable"]
    spot = next((s["spot"] for s in chain_near if s["spot"]), None)
    exp_dt = expiry_dt(near_expiry)
    T = years_to_expiry(exp_dt.date() if exp_dt else None, now)

    strikes = {}
    for s in chain_near:
        coi = s["c"]["oi"] if (s["c"]["oi"] is not None and s["c"]["oi"] >= C.MIN_OI) else None
        poi = s["p"]["oi"] if (s["p"]["oi"] is not None and s["p"]["oi"] >= C.MIN_OI) else None
        if coi is None and poi is None:
            continue
        cd = leg_delta(s["c"], spot, s["strike"], T, C.RISK_FREE, True) if coi is not None else None
        pd = leg_delta(s["p"], spot, s["strike"], T, C.RISK_FREE, False) if poi is not None else None
        strikes[f"{s['strike']:.0f}"] = [coi, poi, cd, pd]

    # 3rd OTM put spread ratio
    otm_puts = [s for s in chain_near if spot is not None and s["strike"] < spot
                and quality(s["p"], C.MIN_OI)]
    spr = spr_strike = None
    if len(otm_puts) >= 3:
        third = sorted(otm_puts, key=lambda s: s["strike"], reverse=True)[2]
        bid, ask = third["p"]["bid"], third["p"]["ask"]
        mid = (bid + ask) / 2.0
        if mid and mid > 0:
            spr = (ask - bid) / mid
            spr_strike = third["strike"]

    state.add_snapshot(now.isoformat(),
                       {"strikes": strikes, "spr": spr, "spot": spot, "vix": None})

    # net delta-weighted OI change vs ~15 min ago
    cutoff = (now - timedelta(minutes=75)).isoformat()
    snaps = state.snapshots_since(cutoff)
    ref = None
    for ts, payload in snaps:
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            continue
        if C.FLOW_MIN_AGE <= age <= C.FLOW_MAX_AGE:
            if ref is None or abs(age - C.FLOW_TARGET_AGE) < abs((now - datetime.fromisoformat(ref[0])).total_seconds() - C.FLOW_TARGET_AGE):
                ref = (ts, payload)
    net_flow = None
    if ref:
        dcall = dput = 0.0
        for k, (coi, poi, cd, pd) in strikes.items():
            old = ref[1]["strikes"].get(k)
            if not old:
                continue
            if coi is not None and old[0] is not None and cd is not None:
                dcall += (coi - old[0]) * cd
            if poi is not None and old[1] is not None and pd is not None:
                dput += (poi - old[1]) * pd
        net_flow = dcall + dput

    # spread ratio vs ~1h average
    spr_avg = None
    hist = []
    for ts, payload in snaps:
        if payload.get("spr") is None:
            continue
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            continue
        if age <= C.SPREAD_AVG_MIN * 60:
            hist.append((ts, payload["spr"]))
    if len(hist) >= 3:
        span_min = (now - datetime.fromisoformat(hist[0][0])).total_seconds() / 60.0
        if span_min >= C.SPREAD_SPAN_MIN:
            spr_avg = statistics.mean(v for _, v in hist)

    if net_flow is None or spr_avg is None:
        why = []
        if net_flow is None:
            why.append("net-flow warming up (needs OI snapshot 10-30 min old)")
        if spr_avg is None:
            why.append("spread baseline warming up (~20 min history)")
        detail = (f"Net_dOI(15m): {'n/a' if net_flow is None else fx(net_flow, 0)} | "
                  f"3rd-OTM-put({spr_strike if spr_strike else '-'}) SPR "
                  f"{f'{spr:.4f}' if spr is not None else 'n/a'} vs 1h avg "
                  f"{f'{spr_avg:.4f}' if spr_avg is not None else 'n/a'}")
        return None, False, detail, why

    if spr < spr_avg * 0.985:
        spr_state = "CONTRACTING"
    elif spr > spr_avg * 1.015:
        spr_state = "WIDENING"
    else:
        spr_state = "FLAT"
    if net_flow > 0 and spr_state == "CONTRACTING":
        raw, tag = 1, "aggressive bullish flow"
    elif net_flow < 0 and spr_state == "WIDENING":
        raw, tag = -1, "defensive / panic flow"
    else:
        raw, tag = 0, "mixed"
    detail = (f"Net_dOI(15m) {net_flow:+,.0f} | SPR {spr:.4f} vs avg {spr_avg:.4f} "
              f"-> {spr_state} | {tag}")
    return raw, True, detail, notes


# ---------------------------------------------------------------------------
# Step 5 — Persistence filter (3-reading circular buffer per module)
# ---------------------------------------------------------------------------
MODULES = ["vol", "edge", "trend", "flow"]


class Persistence:
    def __init__(self, state):
        self.state = state
        self.buf = {m: list(state.get_buffers().get(m, []))[-3:] for m in MODULES}
        self.conf = {m: int(state.get_confirmed().get(m, 0)) for m in MODULES}
        self.output = {}

    def update(self, name, raw):
        if raw is None:
            self.output[name] = (self.conf[name], None, "no reading - holding previous")
            return self.output[name]
        b = self.buf[name]
        b.append(raw)
        if len(b) > 3:
            b.pop(0)
        if len(b) == 3 and b[0] == b[1] == b[2]:
            self.conf[name] = raw
            self.output[name] = (raw, raw, "confirmed (3 consecutive)")
        else:
            self.output[name] = (self.conf[name], raw, f"unconfirmed - needs {3 - len(b)} more consecutive")
        return self.output[name]

    def persist(self):
        self.state.set_buffers(self.buf)
        self.state.set_confirmed(self.conf)


# ---------------------------------------------------------------------------
# Step 6 — Macro override
# ---------------------------------------------------------------------------
def load_events(path=C.EVENTS_FILE):
    import json
    import os
    template = {"events": [
        {"name": "EXAMPLE - US CPI Release", "start": "2026-09-11 18:30", "impact": "high"},
        {"name": "EXAMPLE - RBI MPC Decision", "start": "2026-10-01 10:00", "impact": "high"},
        {"name": "EXAMPLE - Union Budget Speech", "start": "2027-02-01 11:00", "impact": "high"},
    ]}
    if not os.path.isfile(path):
        try:
            with open(path, "w") as fh:
                json.dump(template, fh, indent=2)
        except OSError:
            pass
    try:
        with open(path) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return [], "calendar unreadable"
    out = []
    for e in d.get("events", []):
        try:
            dt = datetime.strptime(e["start"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            out.append({"name": e.get("name", "?"), "dt": dt,
                        "high": str(e.get("impact", "high")).lower() == "high"})
        except (KeyError, ValueError):
            continue
    return out, None


def macro_status(events, now):
    for e in events:
        if not e["high"]:
            continue
        if e["dt"] - timedelta(hours=C.EVENT_PRE_HOURS) <= now <= e["dt"] + timedelta(hours=C.EVENT_POST_HOURS):
            return True, e, f"OVERRIDE ACTIVE: '{e['name']}' @ {e['dt']:%d-%b %H:%M} IST"
    upcoming = sorted((e for e in events if e["high"] and e["dt"] > now), key=lambda e: e["dt"])
    if upcoming:
        e = upcoming[0]
        return False, e, f"next high-impact: '{e['name']}' {e['dt']:%d-%b %Y %H:%M} IST"
    return False, None, "no high-impact events in calendar"


# ---------------------------------------------------------------------------
# Steps 7-8 — aggregation & mapping
# ---------------------------------------------------------------------------
def map_regime(x: float):
    if x > 0.45:
        return C.REGIME_STRONG_SELL
    if x >= 0.15:
        return C.REGIME_MILD_SELL
    if x > -0.15:
        return C.REGIME_NEUTRAL
    if x >= -0.45:
        return C.REGIME_BUY
    return C.REGIME_STRONG_BUY


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class RegimeDetector:
    def __init__(self, feed, state, clock, events=None, loggers=None):
        self.feed = feed
        self.state = state
        self.clock = clock
        self.events = events or []
        self.loggers = loggers
        self.persistence = Persistence(state)
        self.prev_regime = None
        self.prev_confirmed_regime = None
        self._zone_readings = []   # last 3 regime zones for the 15-min confirmation
        self.last_result = None

    # ------------------------------------------------------------- detection
    def run_cycle(self, near_expiry, monthly_expiry) -> RegimeResult:
        now = self.clock.now()
        notes = []
        if hasattr(self.feed, "tick"):
            self.feed.tick()

        # ---- quotes ---------------------------------------------------------
        qkeys = [C.KEY_NIFTY, C.KEY_VIX]
        master_fut = getattr(self.feed, "client", None)
        fut_keys = []
        if hasattr(self.feed, "quotes"):
            pass
        q = {}
        try:
            q = self.feed.quotes(qkeys)
        except Exception as e:
            notes.append(f"quotes failed: {e}")

        def qv(key):
            norm = key.replace("|", "").replace(" ", "").lower()
            for qk, v in q.items():
                if norm in qk.replace("|", "").replace(" ", "").lower():
                    return v
            return {}

        spot_q = (qv(C.KEY_NIFTY) or {}).get("last_price")
        vix = (qv(C.KEY_VIX) or {}).get("last_price") or 0.0

        # ---- option chains ----------------------------------------------------
        chain_near = []
        chain_monthly = []
        if near_expiry:
            try:
                chain_near = self.feed.option_chain(C.KEY_NIFTY, near_expiry)
            except Exception as e:
                notes.append(f"near chain error: {e}")
        if monthly_expiry and monthly_expiry != near_expiry:
            try:
                chain_monthly = self.feed.option_chain(C.KEY_NIFTY, monthly_expiry)
            except Exception as e:
                notes.append(f"monthly chain error: {e}")

        spot = None
        for ch in (chain_near, chain_monthly):
            if ch:
                s = next((x["spot"] for x in ch if x["spot"]), None)
                if s:
                    spot = s
                    break
        if spot is None:
            spot = spot_q
        if spot is None:
            raise RuntimeError("cannot determine NIFTY spot (quotes + chain both failed)")

        # ---- history ----------------------------------------------------------
        daily = []
        try:
            daily = self.feed.daily_candles(C.KEY_NIFTY)
        except Exception as e:
            notes.append(f"daily candles failed: {e}")
        rv, nret = realised_vol_pct([b["c"] for b in daily], window=C.RV_WINDOW,
                                    annualise=C.RV_ANNUALISE)
        if rv is None:
            notes.append(f"RV needs {C.RV_WINDOW + 1} daily closes (have {nret + 1})")

        bars5 = []
        try:
            bars5 = self.feed.hist_5m(C.KEY_NIFTY) + self.feed.intraday_5m(C.KEY_NIFTY)
        except Exception as e:
            notes.append(f"5-min candles failed: {e}")
        seen = set()
        uniq = []
        for b in sorted(bars5, key=lambda b: b["dt"]):
            if b["dt"] not in seen:
                uniq.append(b)
                seen.add(b["dt"])
        bars5 = uniq[-300:]

        vix_series = []
        try:
            vix_series = self.feed.vix_series(30)
        except Exception as e:
            notes.append(f"vix history failed: {e}")
        self._vix_series_cache = vix_series

        # ---- module scores ----------------------------------------------------
        vol_raw, _, vol_det, nt = module_vol(chain_near, chain_monthly, vix, self.state, now, near_expiry)
        notes += nt
        edge_raw, _, edge_det, nt = module_edge(chain_near, rv, spot)
        notes += nt
        trend_raw, _, trend_det, nt = module_trend(bars5, spot)
        notes += nt
        flow_raw, _, flow_det, nt = module_flow(chain_near, self.state, now, near_expiry)
        notes += nt

        self.persistence.update("vol", vol_raw)
        self.persistence.update("edge", edge_raw)
        self.persistence.update("trend", trend_raw)
        self.persistence.update("flow", flow_raw)
        self.persistence.persist()
        conf = self.persistence.conf

        raw_composite = (C.WEIGHTS["vol"] * (vol_raw or conf["vol"])
                         + C.WEIGHTS["edge"] * (edge_raw or conf["edge"])
                         + C.WEIGHTS["trend"] * (trend_raw or conf["trend"])
                         + C.WEIGHTS["flow"] * (flow_raw or conf["flow"]))
        composite = (C.WEIGHTS["vol"] * conf["vol"] + C.WEIGHTS["edge"] * conf["edge"]
                     + C.WEIGHTS["trend"] * conf["trend"] + C.WEIGHTS["flow"] * conf["flow"])

        override, event, macro_txt = macro_status(self.events, now)
        regime = C.REGIME_EVENT_HEDGE if override else map_regime(composite)

        # ---- 15-minute zone confirmation --------------------------------------
        zone = map_regime(composite) if not override else C.REGIME_EVENT_HEDGE
        self._zone_readings.append(zone)
        if len(self._zone_readings) > 3:
            self._zone_readings.pop(0)
        if len(self._zone_readings) == 3 and self._zone_readings[0] == self._zone_readings[1] == self._zone_readings[2]:
            self.prev_confirmed_regime = zone
            confirmed_note = "REGIME CONFIRMED (3 consecutive readings / 15 min)"
        else:
            confirmed_note = (f"zone confirmation {len(self._zone_readings)}/3 "
                              f"({len(self._zone_readings) * 5} min)")
        notes.append(confirmed_note)

        # ---- display extras ---------------------------------------------------
        atm = atm_strike(chain_near, spot) if chain_near else None
        iv_atm = None
        if atm:
            ivs = [v for v in (atm["c"]["iv"], atm["p"]["iv"]) if v and v > 0]
            if ivs:
                iv_atm = statistics.mean(ivs)
        pcr = None
        if chain_near:
            tc = sum(s["c"]["oi"] or 0 for s in chain_near)
            tp = sum(s["p"]["oi"] or 0 for s in chain_near)
            pcr = tp / tc if tc else None
        fut_basis = None
        if getattr(self, "futs", None):
            fk = self.futs[0]["key"]
            try:
                fq = self.feed.quotes([fk]).get(fk, {})
                fpx = fq.get("last_price")
                if fpx:
                    fut_basis = fpx - spot
            except Exception:
                pass

        result = RegimeResult(
            ts=now, spot=spot, vix=vix,
            vol=ModuleResult(vol_raw, conf["vol"], vol_det, []),
            edge=ModuleResult(edge_raw, conf["edge"], edge_det, []),
            trend=ModuleResult(trend_raw, conf["trend"], trend_det, []),
            flow=ModuleResult(flow_raw, conf["flow"], flow_det, []),
            composite=composite, raw_composite=raw_composite,
            regime=regime, prev_regime=self.prev_regime,
            override=override, macro_text=macro_txt,
            near_expiry=near_expiry, monthly_expiry=monthly_expiry,
            rv=rv, iv_atm=iv_atm, pcr=pcr, fut_basis=fut_basis, notes=notes,
            chain_near=chain_near, chain_monthly=chain_monthly,
            bars5=bars5, daily=daily,
            vix_series=self._vix_series_cache if hasattr(self, "_vix_series_cache") else [])
        self.prev_regime = regime
        self.last_result = result
        return result
