# -*- coding: utf-8 -*-
"""
================================================================================
 regime_engine.py — merged regime_detector.py + strategies/__init__.py +
 strategies/sellers.py + strategies/buyers.py + strategy_selector.py
================================================================================
   * regime_detector.py   : Steps 0-8 (Vol/Edge/Trend/Flow scores, persistence
                            filter, macro override, composite, regime mapping,
                            15-minute zone confirmation).
   * strategies/__init__  : Leg / TradePlan / MarketEnv / BaseStrategy /
                            credit_for shared primitives.
   * strategies/sellers.py: A 45-d ATM straddle, B wide iron condor,
                            C credit spreads, D 1x2 ratio spread.
   * strategies/buyers.py : E long put butterfly, F reduce shorts + ATM put,
                            G long 1-mo ATM straddle, H 25-delta backspread.
   * strategy_selector.py : regime -> strategy mapping with micro-condition
                            gates (ADX/ATR, skew, contango, inventory, gamma)
                            and the theta & liquidity master overrides.
================================================================================
"""
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import main as C
from main import IST
from indicators import (adx14, atr14, bs_delta, ema_series, expected_move,
                        realised_vol_pct, round_down, round_to_nearest,
                        round_up, years_to_expiry)
from storage import fx


# =====================================================================
# PART 1 — REGIME DETECTOR (merged from regime_detector.py)
# =====================================================================

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from indicators import (bs_delta, ema_series, adx14, realised_vol_pct,
                        years_to_expiry)

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

# =====================================================================
# PART 2 — STRATEGY PRIMITIVES (merged from strategies/__init__.py)
# =====================================================================

from dataclasses import dataclass, field

from indicators import bs_delta

# ---------------------------------------------------------------------------
# Execution primitives
# ---------------------------------------------------------------------------
@dataclass
class Leg:
    instrument_key: str
    side: str                # BUY / SELL (broker view)
    qty: int                 # in lots (option lots)
    kind: str                # 'long' (debit/hedge) | 'short' (credit/core)
    role: str = "core"       # 'core' | 'hedge' | 'wing'
    slippage_bps: float = 100.0  # default: hedge-grade slip (core shorts -> 20)
    order_id: str = None
    fill_price: float = None
    status: str = "PENDING"  # PENDING / COMPLETE / PARTIAL / REJECTED / CANCELLED

    def __post_init__(self):
        # core shorts get the cheap 20 bps slip; everything long gets 100 bps
        if self.kind == "short" and self.role == "core":
            self.slippage_bps = 20.0
        elif self.kind == "short":
            self.slippage_bps = 100.0
        else:
            self.slippage_bps = 100.0

    @property
    def strike(self):
        try:
            return float(self.instrument_key.split("|")[3])
        except (IndexError, ValueError):
            return None

    @property
    def option_type(self):
        try:
            return self.instrument_key.split("|")[4].upper()
        except IndexError:
            return None

    @property
    def expiry(self):
        try:
            return self.instrument_key.split("|")[2]
        except IndexError:
            return None

@dataclass
class TradePlan:
    strategy_name: str
    legs: list = field(default_factory=list)
    expiry_date: str = None
    days_to_expiry: float = None
    total_credit: float = 0.0     # premium received (positive) at plan prices
    total_debit: float = 0.0      # premium paid (positive) at plan prices
    max_risk_points: float = 0.0  # worst-case loss per lot (points)
    lot_size: int = C.NIFTY_LOT_SIZE
    meta: dict = field(default_factory=dict)
    exit_rules: dict = field(default_factory=dict)
    # exit_rules keys: profit_target (pts), static_stop_up/down (spot),
    # time_exit_days, hold_days, trail_start, trail_retain, adjustments

    @property
    def net_premium(self):
        return self.total_credit - self.total_debit

# ---------------------------------------------------------------------------
# Market environment
# ---------------------------------------------------------------------------
@dataclass
class MarketEnv:
    spot: float
    vix: float
    near_expiry: str
    monthly_expiry: str
    chain_near: list
    chain_monthly: list
    days_to_expiry_near: float
    days_to_expiry_monthly: float
    scores: dict                  # confirmed module scores
    composite: float
    regime: str
    adx: float = None
    atr: float = None
    atr_5d: float = None
    skew25: float = None          # IV(25d put) - IV(25d call)
    skew_flat: bool = None
    term_spread: float = None     # V_fwd - V_spot
    v_fwd: float = None
    vix_series: list = field(default_factory=list)
    open_shorts: list = field(default_factory=list)
    open_longs: list = field(default_factory=list)
    portfolio_delta: float = 0.0
    gamma_ratio: float = 0.0      # gamma exposure / risk limit (0..1+)
    lot_size: int = C.NIFTY_LOT_SIZE
    trend_score: int = 0

    def chain_side(self, chain, optype):
        """-> [(strike, leg), ...] for a given side of a chain."""
        if not chain:
            return []
        out = []
        for s in chain:
            leg = s.get("c" if optype == "CE" else "p")
            if leg and leg.get("oi") is not None and leg.get("bid") and leg.get("ask"):
                out.append((s["strike"], leg))
        return out

    def side(self, optype):
        """Near-expiry chain side."""
        return self.chain_side(self.chain_near, optype)

    def side_monthly(self, optype):
        return self.chain_side(self.chain_monthly, optype)

    def strike_by_delta(self, optype, target_delta, monthly=False):
        side = self.side_monthly(optype) if monthly else self.side(optype)
        tgt = abs(target_delta) * (1 if optype == "CE" else -1)
        best, best_d = None, None
        for strike, leg in side:
            d = leg.get("delta")
            if d is None or not (0.01 < abs(d) < 0.99):
                continue
            if optype == "CE" and d <= 0:
                continue
            if optype == "PE" and d >= 0:
                continue
            diff = abs(d - tgt)
            if best_d is None or diff < best_d:
                best, best_d = strike, diff
        return best

    def leg_at(self, chain, strike, optype):
        if not chain:
            return None
        for s in chain:
            if s["strike"] == strike:
                return s.get("c" if optype == "CE" else "p")
        return None

    def near_leg(self, strike, optype):
        return self.leg_at(self.chain_near, strike, optype)

    def monthly_leg(self, strike, optype):
        return self.leg_at(self.chain_monthly, strike, optype)

    def spread(self, strike, optype, monthly=False):
        leg = self.monthly_leg(strike, optype) if monthly else self.near_leg(strike, optype)
        if not leg:
            return None
        return (leg.get("ask"), leg.get("bid"))

# ---------------------------------------------------------------------------
# Strategy base
# ---------------------------------------------------------------------------
class BaseStrategy:
    name = "BASE"
    regime = None

    def __init__(self, ctx):
        self.ctx = ctx            # ExecutionContext (feed, broker, clock, state, loggers, risk)

    def validate(self, env: MarketEnv):
        """-> (ok: bool, reason: str)."""
        return True, ""

    def build_plan(self, env: MarketEnv, lots: int = 1) -> TradePlan:
        raise NotImplementedError

    def make_key(self, expiry, strike, optype):
        return f"NSE_FO|NIFTY|{expiry}|{strike:.0f}|{optype}"

# ---------------------------------------------------------------------------
# shared cost helpers
# ---------------------------------------------------------------------------
def credit_for(env: MarketEnv, expiry, legs_spec):
    """legs_spec: [(strike, optype, 'buy'|'sell'), ...] on the given expiry.
    Returns (credit, debit). Buys priced at ask, sells at bid."""
    credit = debit = 0.0
    for strike, optype, action in legs_spec:
        leg = env.leg_at(env.chain_monthly if expiry == env.monthly_expiry else env.chain_near,
                         strike, optype)
        if not leg:
            return None, None
        if action == "sell":
            credit += (leg.get("bid") or 0.0)
        else:
            debit += (leg.get("ask") or 0.0)
    return credit, debit

# =====================================================================
# PART 3 — PREMIUM SELLERS (merged from strategies/sellers.py)
# =====================================================================

import math

from indicators import round_to_nearest, round_down, round_up, expected_move

# ---------------------------------------------------------------------------
# A — 45-day ATM straddle (short), 10% static spot stop
# ---------------------------------------------------------------------------
class Short45DayStraddle(BaseStrategy):
    name = "ATM_STRADDLE_45D"
    regime = C.REGIME_STRONG_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        demo = bool(getattr(self, "ctx", None) and getattr(self.ctx, "demo", False))
        lo, hi = (40, 60) if demo else (40, 50)
        if not (lo <= env.days_to_expiry_monthly <= hi):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside {lo}-{hi} (selector tie-break)"
        atm = env.strike_by_delta("CE", 0.50, monthly=True)
        if atm is None:
            return False, "no ATM call strike"
        spread = env.spread(atm, "CE", monthly=True)
        if spread and spread[0] - spread[1] > 3:
            return False, f"ATM call spread {spread[0]-spread[1]:.1f} pts > 3"
        return True, ""

    def build_plan(self, env, lots=1):
        atm = env.strike_by_delta("CE", 0.50, monthly=True) or round_to_nearest(env.spot)
        ce, pe = env.monthly_leg(atm, "CE"), env.monthly_leg(atm, "PE")
        if not ce or not pe:
            raise ValueError("ATM legs missing for straddle")
        credit = (ce.get("bid") or 0.0) + (pe.get("bid") or 0.0)
        expiry = env.monthly_expiry
        legs = [
            Leg(self.make_key(expiry, atm, "CE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, atm, "PE"), "SELL", lots, "short", "core"),
        ]
        max_risk = round(env.spot * C.STATIC_STOP_PCT, 2) * 2  # ~ both legs 10% ITM
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(credit * lots, 2), total_debit=0.0,
            max_risk_points=round(max_risk * lots, 2), lot_size=env.lot_size,
            meta={"atm_strike": atm, "credit_per_lot": round(credit, 2),
                  "entry_spot": env.spot, "entry_vix": env.vix},
            exit_rules={
                "static_stop_pct": C.STATIC_STOP_PCT,           # spot-based stop
                "profit_target_pct": C.PROFIT_TARGET_PCT,       # 50% of credit
                "time_exit_days": C.TIME_EXIT_DAYS_STRADDLE,    # 21 DTE
            })
        return plan

# ---------------------------------------------------------------------------
# B — Wide iron condor (300-pt wings)
# ---------------------------------------------------------------------------
class WideIronCondor(BaseStrategy):
    name = "WIDE_IRON_CONDOR"
    regime = C.REGIME_STRONG_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        if not (30 <= env.days_to_expiry_monthly <= 45):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside 30-45"
        if not (C.LOW_VIX <= env.vix <= 20.0):
            return False, f"VIX {env.vix:.1f} outside 12-20 band"
        if env.adx is not None and env.adx >= 25:
            return False, f"ADX {env.adx:.1f} >= 25 (not range-bound)"
        return True, ""

    def _strikes(self, env):
        em = expected_move(env.spot, env.vix, env.days_to_expiry_monthly)
        short_put = round_down(env.spot - 1.5 * em)
        short_call = round_up(env.spot + 1.5 * em)
        long_put = short_put - C.IRON_CONDOR_WING_WIDTH
        long_call = short_call + C.IRON_CONDOR_WING_WIDTH
        return short_put, short_call, long_put, long_call

    def build_plan(self, env, lots=1):
        sp, sc, lp, lc = self._strikes(env)
        expiry = env.monthly_expiry
        legs = [
            Leg(self.make_key(expiry, lp, "PE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, lc, "CE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, sp, "PE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, sc, "CE"), "SELL", lots, "short", "core"),
        ]
        credit = credit_for(env, expiry, [(sp, "PE", "sell"), (sc, "CE", "sell"),
                                          (lp, "PE", "buy"), (lc, "CE", "buy")])
        if credit is None or credit[0] is None:
            raise ValueError("condor legs missing (strikes outside chain)")
        credit, _ = credit
        max_risk = max(C.IRON_CONDOR_WING_WIDTH - credit, 0.0)
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(credit * lots, 2), total_debit=0.0,
            max_risk_points=round(max_risk * lots, 2), lot_size=env.lot_size,
            meta={"short_put": sp, "short_call": sc, "long_put": lp, "long_call": lc,
                  "credit_per_lot": round(credit, 2), "wing_width": C.IRON_CONDOR_WING_WIDTH},
            exit_rules={
                "profit_target_pct": C.PROFIT_TARGET_PCT,
                "time_exit_days": C.TIME_EXIT_DAYS_CONDOR,
                "adjust_test_side": True,
            })
        return plan

# ---------------------------------------------------------------------------
# C — Bull Put + Bear Call credit spreads (0.30 / 0.15 delta)
# ---------------------------------------------------------------------------
class CreditSpreads(BaseStrategy):
    name = "BULL_PUT_BEAR_CALL"
    regime = C.REGIME_MILD_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        if not (30 <= env.days_to_expiry_monthly <= 45):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside 30-45"
        if env.adx is not None and env.adx >= 25:
            return False, f"ADX {env.adx:.1f} >= 25"
        sp30 = env.strike_by_delta("PE", 0.30, monthly=True)
        sc30 = env.strike_by_delta("CE", 0.30, monthly=True)
        if not sp30 or not sc30:
            return False, "0.30-delta strikes not found"
        return True, ""

    def build_plan(self, env, lots=1):
        expiry = env.monthly_expiry
        sp30 = env.strike_by_delta("PE", 0.30, monthly=True)
        sp15 = env.strike_by_delta("PE", 0.15, monthly=True)
        sc30 = env.strike_by_delta("CE", 0.30, monthly=True)
        sc15 = env.strike_by_delta("CE", 0.15, monthly=True)
        # sanity: short closer to ATM than long (standard structure)
        if sp15 is None:
            sp15 = sp30 - 100
        if sc15 is None:
            sc15 = sc30 + 100
        legs = [
            Leg(self.make_key(expiry, sp15, "PE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, sc15, "CE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, sp30, "PE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, sc30, "CE"), "SELL", lots, "short", "core"),
        ]
        credit, debit = credit_for(env, expiry, [(sp30, "PE", "sell"), (sc30, "CE", "sell"),
                                                 (sp15, "PE", "buy"), (sc15, "CE", "buy")])
        if credit is None:
            raise ValueError("credit spread legs missing")
        width_p = abs(sp30 - sp15)
        width_c = abs(sc30 - sc15)
        # per-side credits and max risk per spec: max loss = width - credit
        put_legs = [(sp30, "PE", "sell"), (sp15, "PE", "buy")]
        call_legs = [(sc30, "CE", "sell"), (sc15, "CE", "buy")]
        pc, pd = credit_for(env, expiry, put_legs)
        cc, cd = credit_for(env, expiry, call_legs)
        put_net = (pc or 0.0) - (pd or 0.0)
        call_net = (cc or 0.0) - (cd or 0.0)
        net_credit = put_net + call_net
        max_risk = max(width_p - put_net, width_c - call_net, 0.0)
        if net_credit <= 0:
            raise ValueError(f"net credit {net_credit:.1f} <= 0 — abort")
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(net_credit * lots, 2), total_debit=0.0,
            max_risk_points=round(max_risk * lots, 2), lot_size=env.lot_size,
            meta={"short_put": sp30, "long_put": sp15, "short_call": sc30, "long_call": sc15,
                  "credit_per_lot": round(net_credit, 2),
                  "put_width": width_p, "call_width": width_c},
            exit_rules={"profit_target_pct": C.PROFIT_TARGET_PCT,
                        "time_exit_days": C.TIME_EXIT_DAYS_CONDOR,
                        "roll_delta": 0.35})
        return plan

# ---------------------------------------------------------------------------
# D — Call/Put Ratio Spread (1x2), 50-pt offset
# ---------------------------------------------------------------------------
class RatioSpread1x2(BaseStrategy):
    name = "RATIO_SPREAD_1x2"
    regime = C.REGIME_MILD_SELL

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        if not (30 <= env.days_to_expiry_monthly <= 45):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside 30-45"
        if env.skew_flat is not True:
            return False, "skew not flat (needs < 0.5% put-call IV diff)"
        if env.term_spread is None or env.term_spread <= 1.5:
            return False, f"contango {env.term_spread:.2f} not > 1.5 pts"
        if env.adx is not None and env.adx > 25:
            return False, f"ADX {env.adx:.1f} > 25"
        return True, ""

    def build_plan(self, env, lots=1):
        expiry = env.monthly_expiry
        atm = round_to_nearest(env.spot)
        legs = [
            # longs first per spec sequence (buyer of the wing)
            Leg(self.make_key(expiry, atm + 50, "CE"), "BUY", 2 * lots, "long", "wing"),
            Leg(self.make_key(expiry, atm - 50, "PE"), "BUY", 2 * lots, "long", "wing"),
            Leg(self.make_key(expiry, atm, "CE"), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, atm, "PE"), "SELL", lots, "short", "core"),
        ]
        credit, debit = credit_for(env, expiry, [(atm, "CE", "sell"), (atm, "PE", "sell"),
                                                 (atm + 50, "CE", "buy"), (atm - 50, "PE", "buy")])
        if credit is None:
            raise ValueError("ratio spread legs missing")
        net = credit - 2 * debit
        if net <= 0:
            raise ValueError("net debit ratio spread — abort")
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=round(credit * lots, 2), total_debit=round(2 * debit * lots, 2),
            max_risk_points=round(0.05 * env.spot * lots, 2), lot_size=env.lot_size,
            meta={"atm": atm, "net_per_lot": round(net, 2)},
            exit_rules={"profit_target_pct": 0.40,
                        "time_exit_days": C.RATIO_TIME_EXIT_DAYS,
                        "close_side_on_delta": 0.35})
        return plan

# =====================================================================
# PART 4 — PREMIUM BUYERS / DEFENSIVE (merged from strategies/buyers.py)
# =====================================================================

import statistics

from indicators import round_to_nearest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def vix_above_sma(vix_series, vix, n):
    if len(vix_series) < n:
        return None
    sma = statistics.mean(vix_series[-n:])
    return vix, sma, vix > sma * 1.0

# ---------------------------------------------------------------------------
# E — Long Put Butterfly (0.30 / 0.20 / 0.10)
# ---------------------------------------------------------------------------
class LongPutButterfly(BaseStrategy):
    name = "LONG_PUT_BUTTERFLY"
    regime = C.REGIME_BUY

    def validate(self, env):
        if env.days_to_expiry_near is None or env.days_to_expiry_near > 7:
            return False, "weekly expiry needed (<= 7 DTE)"
        a = env.strike_by_delta("PE", 0.30)
        b = env.strike_by_delta("PE", 0.20)
        c = env.strike_by_delta("PE", 0.10)
        if not (a and b and c):
            return False, "0.30/0.20/0.10 put strikes not found"
        if not (abs(b - a) == abs(c - b)):
            return False, f"strikes not equidistant ({a},{b},{c})"
        return True, ""

    def build_plan(self, env, lots=1):
        a = env.strike_by_delta("PE", 0.30)
        b = env.strike_by_delta("PE", 0.20)
        c = env.strike_by_delta("PE", 0.10)
        expiry = env.near_expiry
        legs = [
            Leg(self.make_key(expiry, a, "PE"), "BUY", lots, "long", "wing"),
            Leg(self.make_key(expiry, b, "PE"), "SELL", 2 * lots, "short", "core"),
            Leg(self.make_key(expiry, c, "PE"), "BUY", lots, "long", "wing"),
        ]
        credit, debit = credit_for(env, expiry, [(b, "PE", "sell")])
        debit += (env.near_leg(a, "PE").get("ask") or 0.0)
        debit += (env.near_leg(c, "PE").get("ask") or 0.0)
        net_debit = debit - (credit or 0.0) * 2
        max_profit = (b - a) - net_debit
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_near,
            total_credit=0.0, total_debit=round(net_debit * lots, 2),
            max_risk_points=round(net_debit * lots, 2), lot_size=env.lot_size,
            meta={"wing_a": a, "body_b": b, "wing_c": c,
                  "net_debit_per_lot": round(net_debit, 2),
                  "max_profit_per_lot": round(max_profit, 2)},
            exit_rules={"time_exit_days": 2})
        return plan

# ---------------------------------------------------------------------------
# F — Reduce Shorts 60% (delta-weighted) + Long ATM Put (defensive overlay)
# ---------------------------------------------------------------------------
class ReduceShortsHedge(BaseStrategy):
    name = "REDUCE_SHORTS_PUT_HEDGE"
    regime = C.REGIME_BUY

    def validate(self, env):
        if not env.open_shorts:
            return False, "no existing short positions (use Long Put Butterfly instead)"
        if env.gamma_ratio <= C.GAMMA_LIMIT_PCT:
            return False, f"gamma exposure {env.gamma_ratio:.0%} <= 50% of limit"
        return True, ""

    def build_plan(self, env, lots=1):
        """Defensive overlay: BUY-to-close 60% (delta-weighted) of each short leg,
        then BUY ATM puts sized on the remaining delta."""
        expiry = env.near_expiry
        legs = []
        total_delta = sum(abs(s.get("delta", 0.0)) * s.get("qty", 0) for s in env.open_shorts)
        for s in env.open_shorts:
            d = abs(s.get("delta", 0.0) or 0.0)
            # spec: close_qty = min(qty, round(total_portfolio_delta * 0.60 / |delta_per_leg|))
            close_qty = max(1, min(s["qty"], int(round(total_delta * C.HEDGE_REDUCE_PCT
                                                       / max(d, 0.01)))))
            legs.append(Leg(s["instrument_key"], "BUY", close_qty, "long", "hedge",
                            slippage_bps=100.0))
        # ATM put hedge for remaining delta
        remaining = sum(s["qty"] * abs(s.get("delta", 0.0) or 0.0) for s in env.open_shorts) \
            * (1 - C.HEDGE_REDUCE_PCT)
        atm = round_to_nearest(env.spot)
        put_leg = env.near_leg(atm, "PE")
        put_delta = abs((put_leg or {}).get("delta", -0.5) or -0.5)
        hedge_qty = max(1, int(math_ceil(remaining / max(put_delta, 0.1))))
        legs.append(Leg(self.make_key(expiry, atm, "PE"), "BUY", hedge_qty, "long", "hedge",
                        slippage_bps=100.0))
        debit = 0.0
        for L in legs:
            leg_q = (env.monthly_leg(L.strike, L.option_type) if L.expiry == env.monthly_expiry
                     else env.near_leg(L.strike, L.option_type))
            if leg_q and L.strike:
                debit += (leg_q.get("ask") or 0.0) * L.qty
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_near,
            total_credit=0.0, total_debit=round(debit, 2),
            max_risk_points=0.0, lot_size=env.lot_size,
            meta={"hedge": True, "atm_put": atm, "reduction_pct": C.HEDGE_REDUCE_PCT},
            exit_rules={"time_exit_days": 3, "viX_exit_sma": C.VIX_SMA_FAST})
        return plan

def math_ceil(x):
    import math
    return math.ceil(x)

# ---------------------------------------------------------------------------
# G — Long 1-Month ATM Straddle (3-day hold)
# ---------------------------------------------------------------------------
class LongATMStraddle(BaseStrategy):
    name = "LONG_ATM_STRADDLE_3D"
    regime = C.REGIME_STRONG_BUY

    def validate(self, env):
        if env.days_to_expiry_monthly is None:
            return False, "no monthly expiry"
        demo = bool(getattr(self, "ctx", None) and getattr(self.ctx, "demo", False))
        lo, hi = (25, 60) if demo else (25, 40)
        if not (lo <= env.days_to_expiry_monthly <= hi):
            return False, f"monthly DTE {env.days_to_expiry_monthly:.0f} outside {lo}-{hi}"
        atm = env.strike_by_delta("CE", 0.50, monthly=True) or round_to_nearest(env.spot)
        ce, pe = env.monthly_leg(atm, "CE"), env.monthly_leg(atm, "PE")
        if not ce or not pe:
            return False, "ATM legs missing"
        debit = (ce.get("ask") or 0.0) + (pe.get("ask") or 0.0)
        if debit > env.spot * C.LONG_STRADDLE_MAX_DEBIT_PCT:
            return False, f"straddle debit {debit:.0f} > 2.5% of spot"
        # VIX risen >20% above 10-SMA in last 24h (approximation with daily series)
        if len(env.vix_series) >= 11:
            sma10 = statistics.mean(env.vix_series[-11:-1])
            if env.vix < sma10 * 1.20:
                return False, f"VIX {env.vix:.1f} not >20% above 10-SMA {sma10:.1f}"
        return True, ""

    def build_plan(self, env, lots=1):
        atm = env.strike_by_delta("CE", 0.50, monthly=True) or round_to_nearest(env.spot)
        expiry = env.monthly_expiry
        ce, pe = env.monthly_leg(atm, "CE"), env.monthly_leg(atm, "PE")
        debit = (ce.get("ask") or 0.0) + (pe.get("ask") or 0.0)
        legs = [
            Leg(self.make_key(expiry, atm, "CE"), "BUY", lots, "long", "core"),
            Leg(self.make_key(expiry, atm, "PE"), "BUY", lots, "long", "core"),
        ]
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_monthly,
            total_credit=0.0, total_debit=round(debit * lots, 2),
            max_risk_points=round(debit * lots, 2), lot_size=env.lot_size,
            meta={"atm": atm, "debit_per_lot": round(debit, 2)},
            exit_rules={"profit_target_mult": 1.50,     # +50% on debit
                        "stop_loss_mult": 0.50,         # -50% on debit
                        "hold_days": C.STRADDLE_DAY_HOLD})
        return plan

# ---------------------------------------------------------------------------
# H — Long 25-Delta Strangle Backspread (directional)
# ---------------------------------------------------------------------------
class Backspread(BaseStrategy):
    name = "BACKSPREAD_25D"
    regime = C.REGIME_STRONG_BUY

    def validate(self, env):
        if env.days_to_expiry_near is None or not (7 <= env.days_to_expiry_near <= 10):
            return False, f"weekly expiry 7-10 DTE needed (have {env.days_to_expiry_near})"
        if env.vix > 30:
            return False, f"VIX {env.vix:.1f} > 30 — backspread too expensive"
        if env.trend_score == 0:
            return False, "Trend_Score neutral — use Long ATM Straddle instead"
        if env.skew_flat is not True:
            return False, "skew not flat (< 0.5%)"
        over = "CE" if env.trend_score > 0 else "PE"
        d25 = env.strike_by_delta(over, 0.25)
        d10 = env.strike_by_delta(over, 0.10)
        if not d25 or not d10:
            return False, "25d/10d strikes not found on overweight side"
        if abs(d25 - d10) < C.BACKSPREAD_MIN_WIDTH:
            return False, f"25d-10d width {abs(d25-d10):.0f} < 100 pts"
        return True, ""

    def build_plan(self, env, lots=1):
        over = "CE" if env.trend_score > 0 else "PE"
        under = "PE" if over == "CE" else "CE"
        expiry = env.near_expiry
        o25 = env.strike_by_delta(over, 0.25)
        o10 = env.strike_by_delta(over, 0.10)
        u25 = env.strike_by_delta(under, 0.25)
        u10 = env.strike_by_delta(under, 0.10)
        legs = [
            # longs first: 3 lots on overweight side, 1 lot underweight
            Leg(self.make_key(expiry, o25, over), "BUY", 3 * lots, "long", "core"),
            Leg(self.make_key(expiry, u25, under), "BUY", lots, "long", "core"),
            # shorts
            Leg(self.make_key(expiry, o10, over), "SELL", lots, "short", "core"),
            Leg(self.make_key(expiry, u10, under), "SELL", lots, "short", "core"),
        ]
        ask_o = (env.near_leg(o25, over).get("ask") or 0.0)
        ask_u = (env.near_leg(u25, under).get("ask") or 0.0)
        bid_o = (env.near_leg(o10, over).get("bid") or 0.0)
        bid_u = (env.near_leg(u10, under).get("bid") or 0.0)
        net_debit = (3 * ask_o + ask_u) - (bid_o + bid_u)
        if net_debit <= 0 or net_debit > C.BACKSPREAD_MAX_DEBIT:
            raise ValueError(f"backspread net debit {net_debit:.1f} outside (0, 30]")
        plan = TradePlan(
            strategy_name=self.name, legs=legs, expiry_date=expiry,
            days_to_expiry=env.days_to_expiry_near,
            total_credit=0.0, total_debit=round(net_debit * lots, 2),
            max_risk_points=round(net_debit * lots, 2), lot_size=env.lot_size,
            meta={"overweight": over, "net_debit_per_lot": round(net_debit, 2),
                  "strikes": {"o25": o25, "o10": o10, "u25": u25, "u10": u10}},
            exit_rules={"profit_target_mult": 10.0,      # 10x net debit
                        "stop_move_pct": 0.015,          # 1.5% against overweight side
                        "time_exit_days": 2})
        return plan

# =====================================================================
# PART 5 — STRATEGY SELECTOR (merged from strategy_selector.py)
# =====================================================================

import json
import statistics
from dataclasses import dataclass, field

from indicators import adx14, atr14, ema_series, expected_move

STRATEGY_MAP = {
    C.REGIME_STRONG_SELL: [Short45DayStraddle, WideIronCondor],
    C.REGIME_MILD_SELL: [CreditSpreads, RatioSpread1x2],
    C.REGIME_BUY: [LongPutButterfly, ReduceShortsHedge],
    C.REGIME_STRONG_BUY: [LongATMStraddle, Backspread],
}

COMPLEX_STRATEGIES = {WideIronCondor.name, LongPutButterfly.name, Backspread.name}

@dataclass
class SelectionResult:
    regime: str
    plan: object = None
    strategy_name: str = None
    rationale: str = ""
    env: MarketEnv = None

class StrategySelector:
    def __init__(self, ctx, detector):
        self.ctx = ctx
        self.detector = detector
        self.last = None

    # ------------------------------------------------------------- env build
    def build_env(self, res) -> MarketEnv:
        spot, vix = res.spot, res.vix or 0.0
        daily_closes = [b["c"] for b in res.daily]

        # ADX / ATR from 5-min bars
        adx_v = None
        if len(res.bars5) >= 30:
            ax = adx14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in res.bars5[-90:]])
            if ax:
                adx_v = ax[0]
        atr_now = atr14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in res.bars5[-30:]]) if res.bars5 else None
        atr_5d = None
        if len(res.daily) >= 6:
            atr_5d = atr14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in res.daily[-6:]])

        # 25-delta skew on near chain
        skew25 = None
        best_c = best_p = None
        for s in res.chain_near:
            if s["c"].get("delta") is not None and 0.1 < abs(s["c"]["delta"]) < 0.5:
                if best_c is None or abs(abs(s["c"]["delta"]) - 0.25) < abs(abs(best_c[0]) - 0.25):
                    best_c = (s["c"]["delta"], s["c"].get("iv"))
            if s["p"].get("delta") is not None and 0.1 < abs(s["p"]["delta"]) < 0.5:
                if best_p is None or abs(abs(s["p"]["delta"]) - 0.25) < abs(abs(best_p[0]) - 0.25):
                    best_p = (s["p"]["delta"], s["p"].get("iv"))
        if best_c and best_p and best_c[1] and best_p[1]:
            skew25 = best_p[1] - best_c[1]

        # forward vol / term spread from monthly chain
        v_fwd = term_spread = None
        if res.chain_monthly:
            ivs = []
            for s in res.chain_monthly:
                if abs(s["strike"] - spot) <= 100:
                    for k in ("c", "p"):
                        if s[k].get("iv"):
                            ivs.append(s[k]["iv"])
            if ivs:
                v_fwd = statistics.mean(ivs)
                term_spread = v_fwd - vix

        # open positions -> shorts/longs with deltas
        open_shorts, open_longs = [], []
        for pos in self.ctx.state.get_open_positions():
            try:
                legs = json.loads(pos.get("legs_json") or "[]")
            except (TypeError, ValueError):
                legs = []
            for leg in legs:
                delta = self._delta_from_chains(leg.get("instrument_key", ""), res)
                if delta is None:
                    delta = 0.0
                if leg.get("side") == "SELL":
                    open_shorts.append({"instrument_key": leg["instrument_key"],
                                        "qty": leg.get("qty", 0),
                                        "avg_price": leg.get("fill_price"),
                                        "delta": delta, "trade_id": pos["trade_id"]})
                else:
                    open_longs.append({"instrument_key": leg["instrument_key"],
                                       "qty": leg.get("qty", 0),
                                       "avg_price": leg.get("fill_price"),
                                       "delta": delta, "trade_id": pos["trade_id"]})

        gamma_ratio = self.ctx.risk.gamma_ratio(open_shorts, spot)
        portfolio_delta = sum((s.get("delta") or 0.0) * s.get("qty", 0) for s in open_shorts)

        # 200-day EMA
        ema200 = None
        if len(daily_closes) >= 200:
            e = ema_series(daily_closes, 200)
            if e:
                ema200 = e[-1]

        days_near = _days_to(res.near_expiry, res.ts) if res.near_expiry else None
        days_monthly = _days_to(res.monthly_expiry, res.ts) if res.monthly_expiry else None

        env = MarketEnv(
            spot=spot, vix=vix,
            near_expiry=res.near_expiry, monthly_expiry=res.monthly_expiry,
            chain_near=res.chain_near, chain_monthly=res.chain_monthly,
            days_to_expiry_near=days_near, days_to_expiry_monthly=days_monthly,
            scores=res.confirmed_scores, composite=res.composite, regime=res.regime,
            adx=adx_v, atr=atr_now, atr_5d=atr_5d,
            skew25=skew25, skew_flat=(abs(skew25) < 0.5 if skew25 is not None else None),
            term_spread=term_spread, v_fwd=v_fwd, vix_series=res.vix_series,
            open_shorts=open_shorts, open_longs=open_longs,
            portfolio_delta=portfolio_delta, gamma_ratio=gamma_ratio,
            lot_size=self.ctx.lot_size,
            trend_score=int(res.trend.confirmed or 0),
        )
        env.meta_ema200 = ema200
        return env

    def _delta_from_chains(self, instrument_key, res):
        parts = instrument_key.split("|")
        if len(parts) < 5:
            return None
        expiry, strike, otype = parts[2], float(parts[3]), parts[4].upper()
        chain = res.chain_near if expiry == res.near_expiry else res.chain_monthly
        for s in chain:
            if s["strike"] == strike:
                return s.get("c" if otype == "CE" else "p", {}).get("delta")
        return None

    # ------------------------------------------------------------ evaluation
    def evaluate(self, res):
        env = self.build_env(res)
        regime = res.regime
        rationale = []

        # ---- master override 1: theta filter ------------------------------
        # All SELL strategies trade the monthly expiry (30-45 DTE); the filter
        # rejects short premium when that traded contract is inside the last
        # week (gamma risk too high to be a seller).
        traded_dte = env.days_to_expiry_monthly if env.days_to_expiry_monthly is not None \
            else env.days_to_expiry_near
        if regime in (C.REGIME_STRONG_SELL, C.REGIME_MILD_SELL) and traded_dte is not None \
                and traded_dte < 5:
            rationale.append(f"theta filter: traded DTE {traded_dte:.0f} < 5 -> SELL rejected")
            regime = C.REGIME_NEUTRAL

        # ---- master override 2: liquidity filter ---------------------------
        atm_spread = self._atm_spread(env)
        if atm_spread is not None and atm_spread > 3:
            rationale.append(f"liquidity filter: ATM spread {atm_spread:.1f} pts > 3")

        strat_cls, pick_note = self._pick(env, regime)
        rationale.append(pick_note)

        if strat_cls is None:
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last

        # complex strategy rejected by liquidity -> fall back to the simpler one
        if strat_cls.name in COMPLEX_STRATEGIES and atm_spread is not None and atm_spread > 3:
            pair = STRATEGY_MAP.get(regime, [])
            fallback = next((k for k in pair if k.name not in COMPLEX_STRATEGIES), None)
            if fallback:
                strat_cls = fallback
                rationale.append(f"liquidity fallback -> {fallback.name}")

        strategy = strat_cls(self.ctx)
        ok, reason = strategy.validate(env)
        if not ok:
            pair = STRATEGY_MAP.get(regime, [])
            alt = next((k for k in pair if k.name != strat_cls.name), None)
            if alt:
                alt_strat = alt(self.ctx)
                ok2, reason2 = alt_strat.validate(env)
                if ok2:
                    strategy, strat_cls = alt_strat, alt
                    rationale.append(f"primary invalid ({reason}) -> alt {alt.name}")
                else:
                    rationale.append(f"primary invalid ({reason}); alt invalid ({reason2})")
                    self.last = SelectionResult(regime=regime,
                                                rationale=" | ".join(rationale), env=env)
                    return self.last
            else:
                rationale.append(f"no alternative for invalid {strat_cls.name}: {reason}")
                self.last = SelectionResult(regime=regime,
                                            rationale=" | ".join(rationale), env=env)
                return self.last

        # size + build
        try:
            probe = strategy.build_plan(env, lots=1)
        except ValueError as e:
            rationale.append(f"plan rejected: {e}")
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last
        existing_risk = self._existing_risk_rupees(env)
        lots = self.ctx.risk.suggest_lots(probe, existing_risk)
        try:
            plan = strategy.build_plan(env, lots=lots)
        except ValueError as e:
            rationale.append(f"plan rejected: {e}")
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last

        # post-build viability checks (min credit etc.)
        reason2 = self._post_checks(plan)
        if reason2:
            rationale.append(reason2)
            self.last = SelectionResult(regime=regime, rationale=" | ".join(rationale), env=env)
            return self.last

        self.last = SelectionResult(regime=regime, plan=plan, strategy_name=strategy.name,
                                    rationale=" | ".join(rationale), env=env)
        return self.last

    # ------------------------------------------------------------------ pick
    def _pick(self, env, regime):
        if regime == C.REGIME_STRONG_SELL:
            dte = env.days_to_expiry_monthly or 0
            if dte > 30:
                return WideIronCondor, f"STRONG_SELL: DTE {dte:.0f} > 30 tie-break -> Wide Iron Condor"
            adx = env.adx or 0
            atr_contracting = env.atr is not None and env.atr_5d is not None and env.atr < env.atr_5d
            skew_asym = env.skew25 is not None and abs(env.skew25) >= 2.0
            if adx < C.RANGE_ADX and atr_contracting:
                return Short45DayStraddle, "STRONG_SELL: ADX<22 & ATR contracting -> ATM Straddle"
            if (C.RANGE_ADX <= adx <= 28) or skew_asym:
                return WideIronCondor, f"STRONG_SELL: ADX {adx:.0f} 22-28 or skew asym -> Iron Condor"
            return Short45DayStraddle, "STRONG_SELL: default -> ATM Straddle"

        if regime == C.REGIME_MILD_SELL:
            if env.skew25 is not None and env.skew25 >= 2.0:
                return CreditSpreads, f"MILD_SELL: skew steep ({env.skew25:+.1f}%) -> Credit Spreads"
            if env.skew_flat and env.term_spread is not None and env.term_spread > 1.5:
                return RatioSpread1x2, f"MILD_SELL: skew flat & contango {env.term_spread:.1f} -> Ratio Spread"
            return CreditSpreads, "MILD_SELL: default -> Credit Spreads"

        if regime == C.REGIME_BUY:
            ema200 = getattr(env, "meta_ema200", None)
            if env.open_shorts and env.gamma_ratio > C.GAMMA_LIMIT_PCT:
                return ReduceShortsHedge, f"BUY_VOL: shorts & gamma {env.gamma_ratio:.0%} > 50% -> Reduce+ATM Put"
            if not env.open_shorts and ema200 is not None and env.spot > ema200:
                return LongPutButterfly, "BUY_VOL: flat book & spot>200EMA -> Long Put Butterfly"
            if not env.open_shorts:
                return LongPutButterfly, "BUY_VOL: flat book -> Long Put Butterfly"
            return None, "BUY_VOL: shorts exist but gamma <= 50% -> hold, tighten stops only"

        if regime == C.REGIME_STRONG_BUY:
            if env.trend_score == 0:
                return LongATMStraddle, "STRONG_BUY: Trend neutral -> Long ATM Straddle (3d)"
            if env.skew_flat:
                return Backspread, f"STRONG_BUY: Trend {env.trend_score:+d} & skew flat -> Backspread"
            return LongATMStraddle, "STRONG_BUY: skew not flat -> Long ATM Straddle (3d)"

        return None, f"{regime}: no new entries (hold / defensive)"

    # --------------------------------------------------------------- helpers
    def _atm_spread(self, env):
        if not env.chain_near:
            return None
        best = min(env.chain_near, key=lambda s: abs(s["strike"] - env.spot))
        spreads = []
        for k in ("c", "p"):
            leg = best.get(k) or {}
            if leg.get("bid") and leg.get("ask"):
                spreads.append(leg["ask"] - leg["bid"])
        return max(spreads) if spreads else None

    def _existing_risk_rupees(self, env):
        total = 0.0
        for pos in self.ctx.state.get_open_positions():
            meta = json.loads(pos.get("meta_json") or "{}")
            total += float(meta.get("risk_rupees") or 0.0)
        return total

    def _post_checks(self, plan):
        if plan.strategy_name in ("WIDE_IRON_CONDOR", "BULL_PUT_BEAR_CALL") \
                and plan.total_credit < C.MIN_CREDIT * max(plan.meta.get("lots", 1), 1):
            return f"credit {plan.total_credit:.0f} < MIN_CREDIT {C.MIN_CREDIT}"
        if plan.strategy_name == "LONG_PUT_BUTTERFLY" and plan.meta.get("net_debit_per_lot", 0) > C.BUTTERFLY_MAX_DEBIT:
            return f"butterfly debit {plan.meta['net_debit_per_lot']:.1f} > {C.BUTTERFLY_MAX_DEBIT}"
        return None

def _days_to(iso_date, now):
    from datetime import datetime
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
        return (d - now.date()).days
    except (TypeError, ValueError):
        return None
