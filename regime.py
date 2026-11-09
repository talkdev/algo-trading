#!/usr/bin/env python3
"""Descriptive market-regime overlay (no entry / exit / sizing / side).

Reads option-chain snapshots, spot and VIX from data/per_day shards.
Futures volume/OI are not stored in these shards; futures-dependent fields
degrade to neutral and mark data_ok.futures=False (conviction penalty).

Usage:
  python regime.py
  python regime.py --date 2026-11-09
  python regime.py --date 2026-11-09 --changes-only
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════
# 2. CONFIG
# ═══════════════════════════════════════════════════════════════════════════

# ---------- TIMING ----------
SNAPSHOT_MIN = 3          # option chain refresh (minutes between published rows)
STALE_MIN = 6
FLOW_WINDOW_MIN = 15      # OI / premium / buildup window
TREND_WINDOW_MIN = 30     # PCR change, skew change, wall migration
PERSIST_SNAPSHOTS = 3

# ---------- OPTION CHAIN ----------
STRIKE_STEP = 50
WINDOW_STRIKES = 10       # ATM +/- 10
PCR_STRONG_BULL = 1.20    # DATA
PCR_BULL = 1.05           # UNVALIDATED
PCR_BEAR = 0.95           # UNVALIDATED
PCR_STRONG_BEAR = 0.80    # DATA
PCR_CHANGE_SIG = 0.10     # UNVALIDATED
WALL_PRESENT_X = 1.7      # DATA
WALL_STRONG_X = 2.5       # DATA
WALL_DOMINANT_X = 1.5     # DATA
WALL_TILT_X = 1.1         # UNVALIDATED
WALL_UNWIND_PCT = 10      # wall OI drop = writers leaving (UNVALIDATED)
NET_WRITING_STRONG = 0.30 # UNVALIDATED
NET_WRITING_MILD = 0.10
SKEW_UNSTABLE_MAX = 1.05  # DATA
SKEW_NORMAL_MAX = 1.20    # DATA
SKEW_CHANGE_SIG = 0.03    # UNVALIDATED
OI_CHG_HIGH_PCT = 40      # DATA (thin)

# ---------- FUTURES ----------
RVOL_DAYS = 10
RVOL_HEAVY = 1.5          # UNVALIDATED
RVOL_THIN = 0.8
FUT_OI_SIG_PCT = 0.5      # UNVALIDATED
FUT_VWAP_SIG_PCT = 0.10   # UNVALIDATED

# ---------- PREMIUM / VOL ----------
VIX_BANDS = [12.5, 16.0, 22.0, 24.0]  # DATA
VIX_SPIKE_PCT, VIX_SPIKE_MIN = 15.0, 14.0  # DATA
IV_EXPAND_PCT = 5         # UNVALIDATED
IV_COMPRESS_PCT = -5
IV_SHOCK_PCT = 15
PREMIUM_DIFF_STRONG = 10  # UNVALIDATED
PREMIUM_DIFF_MILD = 4
PIN_DISTANCE_PCT = 0.10

# ---------- CLASSIFICATION ----------
DIR_STRONG = 60
DIR_MILD = 25
CONV_HIGH, CONV_MED, CONV_LOW = 75, 55, 35

BEHAVIOUR_HYSTERESIS = 2  # publish behaviour after 2 consecutive snaps
# VOLATILITY_SHOCK publishes immediately

BASE_DIR = Path(__file__).resolve().parent
PER_DAY_DIR = BASE_DIR / "data" / "per_day"
DEFAULT_DATE = "2026-11-09"


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def hhmm(ts: str) -> str:
    """Extract HH:MM from timestamp or time string."""
    if "T" in ts:
        return ts.split("T", 1)[1][:5]
    return ts[:5]


def parse_ts(ts: str) -> datetime:
    s = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def minutes_between(a: str, b: str) -> float:
    return abs((parse_ts(a) - parse_ts(b)).total_seconds()) / 60.0


def minutes_to_close(t_hhmm: str, close: str = "15:30") -> float:
    h1, m1 = map(int, t_hhmm.split(":"))
    h2, m2 = map(int, close.split(":"))
    return max(0.0, (h2 * 60 + m2) - (h1 * 60 + m1))


def shard_path(day: str) -> Path:
    return PER_DAY_DIR / f"nifty_algo_{day}.db"


def open_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"shard not found: {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ═══════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════

def load_prev_day_vix(day: str) -> Optional[float]:
    con = open_ro(shard_path(day))
    try:
        row = con.execute(
            "SELECT prev_day_vix_close FROM session_state WHERE trading_date=?",
            (day,),
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    finally:
        con.close()
    return None


def load_spot_vix_series(day: str) -> List[dict]:
    con = open_ro(shard_path(day))
    rows = con.execute(
        "SELECT timestamp, time, spot, vix, atm_iv, skew_ratio, pcr "
        "FROM market_snapshots WHERE date=? ORDER BY timestamp",
        (day,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def list_capture_times(day: str) -> List[str]:
    con = open_ro(shard_path(day))
    rows = con.execute(
        "SELECT DISTINCT capture_time FROM option_chain_snapshot "
        "WHERE trading_date=? ORDER BY capture_time",
        (day,),
    ).fetchall()
    con.close()
    return [str(r[0]) for r in rows]


def subsample_captures(times: List[str], every_min: int = SNAPSHOT_MIN) -> List[str]:
    if not times:
        return []
    out = [times[0]]
    last = parse_ts(times[0])
    for t in times[1:]:
        cur = parse_ts(t)
        if (cur - last).total_seconds() >= every_min * 60 - 5:
            out.append(t)
            last = cur
    # always include last
    if out[-1] != times[-1]:
        out.append(times[-1])
    return out


def load_chain_bundle(day: str, capture_times: List[str]) -> Dict[str, Tuple[List[dict], Optional[float], Optional[float]]]:
    """Bulk-load chain rows for selected capture times. Returns capture -> (rows, spot, vix)."""
    if not capture_times:
        return {}
    con = open_ro(shard_path(day))
    # nearest weekly expiry for the day
    exp_row = con.execute(
        "SELECT expiry FROM option_chain_snapshot WHERE trading_date=? "
        "GROUP BY expiry ORDER BY expiry ASC LIMIT 1",
        (day,),
    ).fetchone()
    if not exp_row:
        con.close()
        return {}
    expiry = str(exp_row[0])
    placeholders = ",".join("?" * len(capture_times))
    rows = con.execute(
        f"SELECT capture_time, strike, option_type, ltp, oi, iv, volume, "
        f"spot_at_capture, vix_at_capture "
        f"FROM option_chain_snapshot "
        f"WHERE trading_date=? AND expiry=? AND capture_time IN ({placeholders}) "
        f"ORDER BY capture_time, strike, option_type",
        (day, expiry, *capture_times),
    ).fetchall()
    con.close()
    by: Dict[str, List[dict]] = {}
    for r in rows:
        by.setdefault(str(r["capture_time"]), []).append(dict(r))
    out: Dict[str, Tuple[List[dict], Optional[float], Optional[float]]] = {}
    for cap, lst in by.items():
        spot = float(lst[0]["spot_at_capture"]) if lst[0]["spot_at_capture"] is not None else None
        vix = float(lst[0]["vix_at_capture"]) if lst[0]["vix_at_capture"] is not None else None
        out[cap] = (lst, spot, vix)
    return out


def chain_to_strikes(rows: List[dict]) -> Dict[float, dict]:
    """strike -> {ce_oi, pe_oi, ce_iv, pe_iv, ce_ltp, pe_ltp, ce_vol, pe_vol}."""
    by: Dict[float, dict] = {}
    for r in rows:
        k = float(r["strike"])
        slot = by.setdefault(k, {
            "ce_oi": 0.0, "pe_oi": 0.0,
            "ce_iv": 0.0, "pe_iv": 0.0,
            "ce_ltp": 0.0, "pe_ltp": 0.0,
            "ce_vol": 0.0, "pe_vol": 0.0,
        })
        ot = str(r["option_type"] or "").lower()
        if ot.startswith("c"):
            slot["ce_oi"] = float(r["oi"] or 0)
            slot["ce_iv"] = float(r["iv"] or 0)
            slot["ce_ltp"] = float(r["ltp"] or 0)
            slot["ce_vol"] = float(r["volume"] or 0)
        else:
            slot["pe_oi"] = float(r["oi"] or 0)
            slot["pe_iv"] = float(r["iv"] or 0)
            slot["pe_ltp"] = float(r["ltp"] or 0)
            slot["pe_vol"] = float(r["volume"] or 0)
    return by


# ═══════════════════════════════════════════════════════════════════════════
# 4. Metrics
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Metrics:
    timestamp: str = ""
    spot: float = 0.0
    vix: float = 0.0
    atm: float = 0.0
    pcr: float = 1.0
    pcr_chg: float = 0.0
    support_strike: float = 0.0
    resist_strike: float = 0.0
    support_x: float = 1.0
    resist_x: float = 1.0
    wall_balance: float = 1.0
    support_shift: float = 0.0
    resist_shift: float = 0.0
    net_writing: float = 0.0
    support_unwind: float = 0.0
    resist_unwind: float = 0.0
    oi_chg: float = 0.0
    max_oi_strike: float = 0.0
    skew: float = 1.0
    skew_chg: float = 0.0
    atm_iv: float = 0.0
    iv_chg: float = 0.0
    straddle: float = 0.0
    straddle_chg: float = 0.0
    premium_diff: float = 0.0
    rvol: float = 1.0
    rvol_15: float = 1.0
    fut_dp: float = 0.0
    fut_doi: float = 0.0
    fut_vwap_dist: float = 0.0
    buildup: str = "NONE"
    em_rem: float = 0.0
    range_width: float = 0.0
    vix_chg: float = 0.0
    sum_pe_oi: float = 0.0
    sum_ce_oi: float = 0.0
    opt_volume: float = 0.0
    futures_available: bool = False
    chain_fresh: bool = True


@dataclass
class HistPoint:
    timestamp: str
    m: Metrics
    dscore: int
    dlab: str
    beh: str
    conv: int


class History:
    def __init__(self) -> None:
        self.points: List[HistPoint] = []
        self.oi_reference: Optional[float] = None
        self.vol_samples: List[Tuple[str, float]] = []  # (ts, opt_volume)

    def store(self, ts: str, m: Metrics, dscore: int, dlab: str, beh: str, conv: int) -> None:
        self.points.append(HistPoint(ts, m, dscore, dlab, beh, conv))
        self.vol_samples.append((ts, m.opt_volume))

    def at_or_before(self, ts: str, minutes_ago: float) -> Optional[HistPoint]:
        target = parse_ts(ts).timestamp() - minutes_ago * 60
        best = None
        for p in self.points:
            if parse_ts(p.timestamp).timestamp() <= target + 30:
                best = p
        return best

    def count_same_direction_label(self, label: str, last: int = 3) -> int:
        recent = self.points[-last:]
        return sum(1 for p in recent if p.dlab == label)

    def option_rvol_15(self, ts: str, current_vol: float) -> float:
        """Session-relative option volume proxy when futures are absent.

        rvol_15 = sum(opt volume over last 15m) / mean(same-length windows so far).
        """
        t0 = parse_ts(ts).timestamp()
        window = [
            v for t, v in self.vol_samples
            if t0 - FLOW_WINDOW_MIN * 60 <= parse_ts(t).timestamp() <= t0
        ]
        window.append(current_vol)
        cur = sum(window)
        # build prior rolling 15m sums at each sample
        priors = []
        for i, (t, _) in enumerate(self.vol_samples):
            ti = parse_ts(t).timestamp()
            s = sum(
                v for tt, v in self.vol_samples[: i + 1]
                if ti - FLOW_WINDOW_MIN * 60 <= parse_ts(tt).timestamp() <= ti
            )
            if s > 0:
                priors.append(s)
        if not priors:
            return 1.0
        base = sum(priors) / len(priors)
        return (cur / base) if base > 0 else 1.0


def classify_buildup(fut_dp: float, fut_doi: float) -> str:
    if abs(fut_doi) < FUT_OI_SIG_PCT:
        return "NONE"
    if fut_dp > 0 and fut_doi > 0:
        return "LONG_BUILDUP"
    if fut_dp > 0 and fut_doi < 0:
        return "SHORT_COVERING"
    if fut_dp < 0 and fut_doi < 0:
        return "LONG_UNWINDING"
    if fut_dp < 0 and fut_doi > 0:
        return "SHORT_BUILDUP"
    return "NONE"


def compute_metrics(
    ts: str,
    strikes: Dict[float, dict],
    spot: float,
    vix: float,
    vix_prev_close: Optional[float],
    hist: History,
) -> Metrics:
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    lo = atm - WINDOW_STRIKES * STRIKE_STEP
    hi = atm + WINDOW_STRIKES * STRIKE_STEP
    win = {k: v for k, v in strikes.items() if lo <= k <= hi}
    if not win:
        win = strikes

    sum_pe = sum(v["pe_oi"] for v in win.values())
    sum_ce = sum(v["ce_oi"] for v in win.values())
    opt_volume = sum(v.get("ce_vol", 0.0) + v.get("pe_vol", 0.0) for v in win.values())
    pcr = (sum_pe / sum_ce) if sum_ce > 0 else 1.0

    pe_below = {k: v["pe_oi"] for k, v in win.items() if k <= spot}
    ce_above = {k: v["ce_oi"] for k, v in win.items() if k >= spot}
    support_strike = max(pe_below, key=pe_below.get) if pe_below else atm
    resist_strike = max(ce_above, key=ce_above.get) if ce_above else atm

    pe_vals = [v["pe_oi"] for v in win.values() if v["pe_oi"] > 0]
    ce_vals = [v["ce_oi"] for v in win.values() if v["ce_oi"] > 0]
    med_pe = statistics.median(pe_vals) if pe_vals else 1.0
    med_ce = statistics.median(ce_vals) if ce_vals else 1.0
    support_x = (win.get(support_strike, {}).get("pe_oi", 0) / med_pe) if med_pe else 1.0
    resist_x = (win.get(resist_strike, {}).get("ce_oi", 0) / med_ce) if med_ce else 1.0
    wall_balance = (support_x / resist_x) if resist_x > 0 else 1.0

    # max OI strike (pin)
    tot = {k: v["ce_oi"] + v["pe_oi"] for k, v in win.items()}
    max_oi_strike = max(tot, key=tot.get) if tot else atm

    # skew / ATM IV / straddle
    put_k = atm - 200
    call_k = atm + 200
    pe_iv = win.get(put_k, strikes.get(put_k, {})).get("pe_iv", 0.0) if True else 0.0
    ce_iv = win.get(call_k, strikes.get(call_k, {})).get("ce_iv", 0.0)
    # fall back nearest available
    if pe_iv <= 0:
        cands = [k for k in strikes if k <= put_k]
        if cands:
            pe_iv = strikes[max(cands)]["pe_iv"]
    if ce_iv <= 0:
        cands = [k for k in strikes if k >= call_k]
        if cands:
            ce_iv = strikes[min(cands)]["ce_iv"]
    skew = (pe_iv / ce_iv) if ce_iv > 0 else 1.0

    atm_row = win.get(atm) or strikes.get(atm) or {"ce_iv": 0, "pe_iv": 0, "ce_ltp": 0, "pe_ltp": 0}
    atm_iv = (float(atm_row.get("ce_iv") or 0) + float(atm_row.get("pe_iv") or 0)) / 2.0
    # IV in DB looks like percent (18.8) sometimes and fraction (0.18) in snapshots —
    # normalise to percent for change maths.
    if 0 < atm_iv < 3:
        atm_iv *= 100.0
    straddle = float(atm_row.get("ce_ltp") or 0) + float(atm_row.get("pe_ltp") or 0)

    # history diffs
    h15 = hist.at_or_before(ts, FLOW_WINDOW_MIN)
    h30 = hist.at_or_before(ts, TREND_WINDOW_MIN)

    pcr_chg = (pcr - h30.m.pcr) if h30 else 0.0
    support_shift = (
        (support_strike - h30.m.support_strike) / STRIKE_STEP if h30 else 0.0
    )
    resist_shift = (
        (resist_strike - h30.m.resist_strike) / STRIKE_STEP if h30 else 0.0
    )

    if h15:
        dPE = sum_pe - h15.m.sum_pe_oi
        dCE = sum_ce - h15.m.sum_ce_oi
        denom = abs(dPE) + abs(dCE)
        net_writing = ((dPE - dCE) / denom) if denom > 0 else 0.0
        # unwind at wall strikes vs 15m ago chain OI proxy stored on metrics
        # We only stored aggregate; approximate unwind via support_x change is weak.
        # Store absolute wall OI on metrics via support_x*med — use sum ratio instead:
        # Prefer: compare current wall OI to hist wall OI if same strike tracked.
        # Metrics keeps support_strike; we stash raw wall OI in sum fields? Add below.
        support_unwind = 0.0
        resist_unwind = 0.0
        # Use PE/CE sum change at wall as proxy when hist available
        if h15.m.support_strike == support_strike and h15.m.sum_pe_oi > 0:
            # can't get exact wall; use net PE change rate
            support_unwind = (sum_pe / h15.m.sum_pe_oi - 1.0) * 100.0
        if h15.m.resist_strike == resist_strike and h15.m.sum_ce_oi > 0:
            resist_unwind = (sum_ce / h15.m.sum_ce_oi - 1.0) * 100.0
        # Better: store wall OI on Metrics
    else:
        net_writing = 0.0
        support_unwind = 0.0
        resist_unwind = 0.0

    # wall OI absolute for better unwind (recompute with hist wall oi if present)
    wall_pe = float(win.get(support_strike, {}).get("pe_oi") or 0)
    wall_ce = float(win.get(resist_strike, {}).get("ce_oi") or 0)

    total_oi = sum_pe + sum_ce
    if hist.oi_reference is None and total_oi > 0:
        hist.oi_reference = total_oi
    oi_chg = (
        (total_oi - hist.oi_reference) / hist.oi_reference * 100.0
        if hist.oi_reference and hist.oi_reference > 0 else 0.0
    )

    skew_chg = (skew - h30.m.skew) if h30 else 0.0
    iv_chg = (
        (atm_iv / h30.m.atm_iv - 1.0) * 100.0
        if h30 and h30.m.atm_iv > 0 else 0.0
    )
    straddle_chg = (
        (straddle / h30.m.straddle - 1.0) * 100.0
        if h30 and h30.m.straddle > 0 else 0.0
    )

    # premium diff: CE% - PE% over 15m
    if h15 and h15.m.straddle > 0:
        # approximate using ATM legs stored indirectly via straddle split not available;
        # recompute from hist atm not stored as legs — store ce/pe ltp on Metrics.
        premium_diff = 0.0
    else:
        premium_diff = 0.0

    vix_chg = (
        (vix / vix_prev_close - 1.0) * 100.0
        if vix_prev_close and vix_prev_close > 0 else 0.0
    )
    rem = minutes_to_close(hhmm(ts))
    em_rem = spot * (vix / 100.0) / math.sqrt(252.0) * math.sqrt(rem / 375.0) if vix > 0 else 0.0
    range_width = resist_strike - support_strike

    # Futures unavailable in shards — participation uses option-window volume
    # as a session-relative proxy (buildup stays NONE without fut OI).
    rvol_15 = hist.option_rvol_15(ts, opt_volume)
    m = Metrics(
        timestamp=ts,
        spot=spot,
        vix=vix,
        atm=atm,
        pcr=pcr,
        pcr_chg=pcr_chg,
        support_strike=support_strike,
        resist_strike=resist_strike,
        support_x=support_x,
        resist_x=resist_x,
        wall_balance=wall_balance,
        support_shift=support_shift,
        resist_shift=resist_shift,
        net_writing=net_writing,
        support_unwind=support_unwind,
        resist_unwind=resist_unwind,
        oi_chg=oi_chg,
        max_oi_strike=max_oi_strike,
        skew=skew,
        skew_chg=skew_chg,
        atm_iv=atm_iv,
        iv_chg=iv_chg,
        straddle=straddle,
        straddle_chg=straddle_chg,
        premium_diff=premium_diff,
        rvol=rvol_15,
        rvol_15=rvol_15,
        fut_dp=0.0,
        fut_doi=0.0,
        fut_vwap_dist=0.0,
        buildup="NONE",
        em_rem=em_rem,
        range_width=range_width,
        vix_chg=vix_chg,
        sum_pe_oi=sum_pe,
        sum_ce_oi=sum_ce,
        opt_volume=opt_volume,
        futures_available=False,
        chain_fresh=True,
    )
    # Attach wall OI for next unwind calc via custom attrs
    m._wall_pe = wall_pe  # type: ignore[attr-defined]
    m._wall_ce = wall_ce  # type: ignore[attr-defined]
    m._ce_ltp = float(atm_row.get("ce_ltp") or 0)  # type: ignore[attr-defined]
    m._pe_ltp = float(atm_row.get("pe_ltp") or 0)  # type: ignore[attr-defined]

    # refine unwind / premium_diff with stored wall & ATM legs
    if h15:
        hp = h15.m
        prev_pe = getattr(hp, "_wall_pe", None)
        prev_ce = getattr(hp, "_wall_ce", None)
        if prev_pe and prev_pe > 0 and hp.support_strike == support_strike:
            m.support_unwind = (wall_pe / prev_pe - 1.0) * 100.0
        if prev_ce and prev_ce > 0 and hp.resist_strike == resist_strike:
            m.resist_unwind = (wall_ce / prev_ce - 1.0) * 100.0
        prev_ce_ltp = getattr(hp, "_ce_ltp", 0.0) or 0.0
        prev_pe_ltp = getattr(hp, "_pe_ltp", 0.0) or 0.0
        ce_now = m._ce_ltp  # type: ignore[attr-defined]
        pe_now = m._pe_ltp  # type: ignore[attr-defined]
        ce_pct = ((ce_now / prev_ce_ltp - 1.0) * 100.0) if prev_ce_ltp > 0 else 0.0
        pe_pct = ((pe_now / prev_pe_ltp - 1.0) * 100.0) if prev_pe_ltp > 0 else 0.0
        m.premium_diff = ce_pct - pe_pct
        # net writing from 15m OI deltas already set
        dPE = sum_pe - hp.sum_pe_oi
        dCE = sum_ce - hp.sum_ce_oi
        denom = abs(dPE) + abs(dCE)
        m.net_writing = ((dPE - dCE) / denom) if denom > 0 else 0.0

    # Without futures, approximate price move from spot vs 15m for info only —
    # do NOT invent buildup (needs OI). Keep buildup=NONE.
    if h15:
        m.fut_dp = spot - h15.m.spot
    return m


# ═══════════════════════════════════════════════════════════════════════════
# 5–8. Classification layers
# ═══════════════════════════════════════════════════════════════════════════

def direction_bias(m: Metrics) -> Tuple[int, str, Dict[str, int], Dict[str, int]]:
    d: Dict[str, int] = {}
    # GROUP A: option writers
    if m.pcr >= PCR_STRONG_BULL:
        d["pcr"] = 2
    elif m.pcr >= PCR_BULL:
        d["pcr"] = 1
    elif m.pcr <= PCR_STRONG_BEAR:
        d["pcr"] = -2
    elif m.pcr <= PCR_BEAR:
        d["pcr"] = -1
    else:
        d["pcr"] = 0

    if m.pcr_chg >= PCR_CHANGE_SIG:
        d["pcr_chg"] = 1
    elif m.pcr_chg <= -PCR_CHANGE_SIG:
        d["pcr_chg"] = -1
    else:
        d["pcr_chg"] = 0

    if m.wall_balance >= WALL_DOMINANT_X:
        d["walls"] = 2
    elif m.wall_balance >= WALL_TILT_X:
        d["walls"] = 1
    elif m.wall_balance <= 1.0 / WALL_DOMINANT_X:
        d["walls"] = -2
    elif m.wall_balance <= 1.0 / WALL_TILT_X:
        d["walls"] = -1
    else:
        d["walls"] = 0

    if m.net_writing >= NET_WRITING_STRONG:
        d["writing"] = 2
    elif m.net_writing >= NET_WRITING_MILD:
        d["writing"] = 1
    elif m.net_writing <= -NET_WRITING_STRONG:
        d["writing"] = -2
    elif m.net_writing <= -NET_WRITING_MILD:
        d["writing"] = -1
    else:
        d["writing"] = 0

    d["migration"] = int(clamp(sign(m.support_shift) + sign(m.resist_shift), -2, 2))

    # GROUP B: futures
    d["buildup"] = {
        "LONG_BUILDUP": 2, "SHORT_COVERING": 1, "NONE": 0,
        "LONG_UNWINDING": -1, "SHORT_BUILDUP": -2,
    }.get(m.buildup, 0)
    if m.fut_vwap_dist >= FUT_VWAP_SIG_PCT:
        d["fut_vwap"] = 1
    elif m.fut_vwap_dist <= -FUT_VWAP_SIG_PCT:
        d["fut_vwap"] = -1
    else:
        d["fut_vwap"] = 0

    # GROUP C: premiums
    if m.premium_diff >= PREMIUM_DIFF_STRONG:
        d["premium"] = 2
    elif m.premium_diff >= PREMIUM_DIFF_MILD:
        d["premium"] = 1
    elif m.premium_diff <= -PREMIUM_DIFF_STRONG:
        d["premium"] = -2
    elif m.premium_diff <= -PREMIUM_DIFF_MILD:
        d["premium"] = -1
    else:
        d["premium"] = 0

    if m.skew_chg >= SKEW_CHANGE_SIG:
        d["skew_chg"] = -1
    elif m.skew_chg <= -SKEW_CHANGE_SIG:
        d["skew_chg"] = 1
    else:
        d["skew_chg"] = 0

    max_abs = 15
    score = int(round(sum(d.values()) / max_abs * 100))
    groups = {
        "A": d["pcr"] + d["pcr_chg"] + d["walls"] + d["writing"] + d["migration"],
        "B": d["buildup"] + d["fut_vwap"],
        "C": d["premium"] + d["skew_chg"],
    }
    if score >= DIR_STRONG:
        label = "STRONG_BULL"
    elif score >= DIR_MILD:
        label = "BULL"
    elif score <= -DIR_STRONG:
        label = "STRONG_BEAR"
    elif score <= -DIR_MILD:
        label = "BEAR"
    else:
        label = "NEUTRAL"
    return score, label, groups, d


@dataclass
class VolState:
    level: str
    trend: str


def volatility(m: Metrics, vix: float, vix_missing: int) -> VolState:
    if vix_missing >= 5 or vix >= VIX_BANDS[3] or (
        m.vix_chg >= VIX_SPIKE_PCT and vix >= VIX_SPIKE_MIN
    ):
        level = "EXTREME"
    elif vix < VIX_BANDS[0]:
        level = "LOW"
    elif vix < VIX_BANDS[1]:
        level = "NORMAL"
    elif vix < VIX_BANDS[2]:
        level = "ELEVATED"
    else:
        level = "HIGH"
    if m.iv_chg >= IV_EXPAND_PCT:
        trend = "EXPANDING"
    elif m.iv_chg <= IV_COMPRESS_PCT:
        trend = "COMPRESSING"
    else:
        trend = "STABLE"
    return VolState(level, trend)


def participation(m: Metrics) -> str:
    if m.rvol_15 >= RVOL_HEAVY:
        return "HEAVY"
    if m.rvol_15 < RVOL_THIN:
        return "THIN"
    return "NORMAL"


def behaviour(m: Metrics, dir_score: int, vol: VolState) -> str:
    near_res = abs(m.resist_strike - m.spot) <= 0.25 * m.em_rem if m.em_rem > 0 else False
    near_sup = abs(m.spot - m.support_strike) <= 0.25 * m.em_rem if m.em_rem > 0 else False
    both_strong = m.support_x >= WALL_STRONG_X and m.resist_x >= WALL_STRONG_X
    inside = m.support_strike < m.spot < m.resist_strike

    if vol.level == "EXTREME" or (m.iv_chg >= IV_SHOCK_PCT and m.rvol_15 >= 2.0):
        return "VOLATILITY_SHOCK"
    if (
        m.buildup == "SHORT_COVERING"
        and m.resist_unwind <= -WALL_UNWIND_PCT
        and m.rvol_15 >= 1.5
    ):
        return "SHORT_COVERING_RALLY"
    if (
        m.buildup == "LONG_UNWINDING"
        and m.support_unwind <= -WALL_UNWIND_PCT
        and m.rvol_15 >= 1.5
    ):
        return "LONG_UNWINDING_DECLINE"
    if (
        dir_score >= DIR_MILD
        and m.buildup in ("LONG_BUILDUP", "SHORT_COVERING")
        and m.rvol_15 >= 1.0
        and m.support_shift >= 0
        and m.iv_chg > IV_COMPRESS_PCT
    ):
        return "TRENDING_UP"
    if (
        dir_score <= -DIR_MILD
        and m.buildup in ("SHORT_BUILDUP", "LONG_UNWINDING")
        and m.rvol_15 >= 1.0
        and m.resist_shift <= 0
        and m.iv_chg > IV_COMPRESS_PCT
    ):
        return "TRENDING_DOWN"
    if near_res and m.resist_unwind <= -WALL_UNWIND_PCT and m.rvol_15 >= 1.2:
        return "BREAKOUT_BUILDING_UP"
    if near_sup and m.support_unwind <= -WALL_UNWIND_PCT and m.rvol_15 >= 1.2:
        return "BREAKOUT_BUILDING_DOWN"
    if (
        inside
        and m.spot > 0
        and abs(m.spot - m.max_oi_strike) / m.spot * 100 <= PIN_DISTANCE_PCT
        and m.straddle_chg < 0
        and m.rvol_15 < 1.0
    ):
        return "PINNED"
    if (
        inside
        and both_strong
        and m.em_rem > 0
        and m.range_width <= 1.5 * m.em_rem
        and abs(dir_score) < DIR_MILD
        and m.iv_chg <= 0
    ):
        return "RANGE_BOUND"
    if abs(dir_score) < DIR_MILD and m.iv_chg >= IV_EXPAND_PCT and m.rvol_15 >= 1.3:
        return "TWO_WAY_VOLATILE"
    if DIR_MILD <= abs(dir_score) < DIR_STRONG and m.rvol_15 < RVOL_THIN:
        return "QUIET_DRIFT"
    return "UNCLEAR"


def conviction(
    dir_score: int,
    groups: Dict[str, int],
    beh: str,
    m: Metrics,
    hist: History,
    data_ok: List[bool],
    dlab: str,
) -> Tuple[int, str]:
    s = sign(dir_score)
    g = [sign(groups["A"]), sign(groups["B"]), sign(groups["C"])]
    agree = g.count(s) if s != 0 else 0
    oppose = g.count(-s) if s != 0 else 0

    if agree == 3:
        pts = 50
    elif agree == 2 and oppose == 0:
        pts = 35
    elif agree == 2:
        pts = 15
    else:
        pts = 0

    a = abs(dir_score)
    if a >= 60:
        pts += 25
    elif a >= 40:
        pts += 18
    elif a >= 25:
        pts += 10

    same = hist.count_same_direction_label(dlab, last=PERSIST_SNAPSHOTS)
    # include current label: hist is prior only, so same+1 if we pretend current
    same_incl = min(PERSIST_SNAPSHOTS, same + 1)
    if same_incl == 3:
        pts += 15
    elif same_incl == 2:
        pts += 8

    pts += 10 - 5 * data_ok.count(False)
    pts = max(pts, 0)

    if beh in ("RANGE_BOUND", "PINNED"):
        both = m.support_x >= WALL_STRONG_X and m.resist_x >= WALL_STRONG_X
        pts = (40 if both else 20)
        pts += 20 if m.rvol_15 < 1 else 0
        pts += 25 if m.iv_chg <= 0 else 0
        pts += 15 if same_incl >= 3 else 0

    if m.skew <= SKEW_UNSTABLE_MAX:
        pts -= 15
    if m.skew > SKEW_NORMAL_MAX:
        pts -= 5
    if m.oi_chg > OI_CHG_HIGH_PCT and beh.startswith("TRENDING"):
        pts += 5
    if beh in ("UNCLEAR", "TWO_WAY_VOLATILE"):
        pts = min(pts, 50)
    if beh == "VOLATILITY_SHOCK":
        pts = min(pts, 40)
    if beh == "QUIET_DRIFT":
        pts = min(pts, 60)

    pts = int(clamp(pts, 0, 100))
    if pts >= CONV_HIGH:
        label = "HIGH"
    elif pts >= CONV_MED:
        label = "MEDIUM"
    elif pts >= CONV_LOW:
        label = "LOW"
    else:
        label = "NONE"
    return pts, label


# ═══════════════════════════════════════════════════════════════════════════
# 9. Final regime loop
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RegimeRow:
    t: str
    spot: float
    vix: float
    direction_score: int
    direction_label: str
    behaviour: str
    vol_level: str
    vol_trend: str
    participation: str
    conviction: int
    conviction_label: str
    regime_string: str
    pcr: float
    skew: float
    net_writing: float
    support: float
    resist: float
    support_x: float
    resist_x: float
    iv_chg: float
    premium_diff: float
    oi_chg: float
    group_A: int
    group_B: int
    group_C: int
    changed: bool = False
    note: str = ""


def run_day(day: str) -> Tuple[List[RegimeRow], dict]:
    vix_prev = load_prev_day_vix(day)
    all_caps = list_capture_times(day)
    captures = subsample_captures(all_caps, SNAPSHOT_MIN)
    bundle = load_chain_bundle(day, captures)
    hist = History()
    rows: List[RegimeRow] = []
    published_beh: Optional[str] = None
    pending_beh: Optional[str] = None
    pending_hold = 0
    vix_missing = 0
    last_vix: Optional[float] = None

    meta = {
        "day": day,
        "n_captures_raw": len(all_caps),
        "n_snapshots": len(captures),
        "vix_prev_close": vix_prev,
        "futures_available": False,
        "note": (
            "futures volume/OI absent in shards; buildup=NONE; "
            "rvol_15 uses ATM-window option volume vs session mean"
        ),
    }

    for cap in captures:
        packed = bundle.get(cap)
        if not packed:
            continue
        chain_rows, spot, vix = packed
        if not chain_rows or spot is None:
            continue
        if vix is None:
            vix_missing += 1
            vix = last_vix if last_vix is not None else 0.0
        else:
            vix_missing = 0
            last_vix = vix

        strikes = chain_to_strikes(chain_rows)
        m = compute_metrics(cap, strikes, float(spot), float(vix), vix_prev, hist)
        age_ok = True  # subsampled from live captures
        # futures missing → data_ok[1]=False (conviction penalty); chain+vix OK
        data_ok = [age_ok, False, vix_missing == 0]

        dscore, dlab, groups, detail = direction_bias(m)
        vol = volatility(m, float(vix), vix_missing)
        raw_beh = behaviour(m, dscore, vol)
        part = participation(m)

        # behaviour hysteresis
        changed = False
        if raw_beh == "VOLATILITY_SHOCK":
            if published_beh != raw_beh:
                changed = True
            published_beh = raw_beh
            pending_beh = None
            pending_hold = 0
        else:
            if published_beh is None:
                published_beh = raw_beh
                pending_beh = raw_beh
                pending_hold = 1
                changed = True
            elif raw_beh == published_beh:
                pending_beh = raw_beh
                pending_hold = 0
            elif raw_beh == pending_beh:
                pending_hold += 1
                if pending_hold >= BEHAVIOUR_HYSTERESIS:
                    published_beh = raw_beh
                    pending_hold = 0
                    changed = True
            else:
                pending_beh = raw_beh
                pending_hold = 1

        beh = published_beh or raw_beh
        conv, clab = conviction(dscore, groups, beh, m, hist, data_ok, dlab)

        out_dlab = dlab
        out_beh = beh
        if beh in ("RANGE_BOUND", "PINNED"):
            out_dlab = "NEUTRAL"
        if clab == "NONE":
            out_beh = "NO_CLEAR_REGIME"

        regime = (
            f"{out_dlab} | {out_beh} | {vol.level}-{vol.trend} | {part} "
            f"| conviction {conv} ({clab})"
        )
        hist.store(cap, m, dscore, dlab, beh, conv)

        # Flag row when published behaviour changed OR the one-line regime text moved
        if rows and regime != rows[-1].regime_string:
            changed = True

        note = ""
        if not m.futures_available:
            note = "no_futures"
        rows.append(RegimeRow(
            t=hhmm(cap),
            spot=m.spot,
            vix=m.vix,
            direction_score=dscore,
            direction_label=out_dlab,
            behaviour=out_beh,
            vol_level=vol.level,
            vol_trend=vol.trend,
            participation=part,
            conviction=conv,
            conviction_label=clab,
            regime_string=regime,
            pcr=round(m.pcr, 3),
            skew=round(m.skew, 3),
            net_writing=round(m.net_writing, 3),
            support=m.support_strike,
            resist=m.resist_strike,
            support_x=round(m.support_x, 2),
            resist_x=round(m.resist_x, 2),
            iv_chg=round(m.iv_chg, 2),
            premium_diff=round(m.premium_diff, 2),
            oi_chg=round(m.oi_chg, 2),
            group_A=groups["A"],
            group_B=groups["B"],
            group_C=groups["C"],
            changed=changed,
            note=note,
        ))
    return rows, meta


def print_table(rows: List[RegimeRow], meta: dict, changes_only: bool = False) -> None:
    print("=" * 140)
    print(
        f"DESCRIPTIVE REGIME  {meta['day']}  "
        f"snapshots={meta['n_snapshots']}/{meta['n_captures_raw']}  "
        f"prev_vix={meta['vix_prev_close']}  futures={meta['futures_available']}"
    )
    print(f"NOTE: {meta['note']}")
    print("=" * 140)
    hdr = (
        f"{'Time':>5} {'Spot':>9} {'VIX':>5} {'Dir':>5} {'DLabel':<12} "
        f"{'Behaviour':<24} {'Vol':<18} {'Part':<6} {'Cv':>3} {'CvL':<6} "
        f"{'PCR':>5} {'Skew':>5} {'NW':>6} {'Sup':>7} {'Res':>7}"
    )
    print(hdr)
    print("-" * 140)
    n = 0
    for r in rows:
        if changes_only and not r.changed:
            continue
        mark = " *" if r.changed else ""
        print(
            f"{r.t:>5} {r.spot:9.2f} {r.vix:5.2f} {r.direction_score:5d} {r.direction_label:<12} "
            f"{r.behaviour:<24} {r.vol_level + '-' + r.vol_trend:<18} {r.participation:<6} "
            f"{r.conviction:3d} {r.conviction_label:<6} "
            f"{r.pcr:5.2f} {r.skew:5.2f} {r.net_writing:6.2f} "
            f"{r.support:7.0f} {r.resist:7.0f}{mark}"
        )
        n += 1
    print("-" * 140)
    print(f"rows_printed={n}")
    print("\nFINAL REGIME STRING (behaviour publish / change)")
    print("-" * 140)
    for r in rows:
        if r.changed:
            print(f"{r.t}  {r.regime_string}")
    if not changes_only:
        print("\nALL REGIME STRINGS")
        print("-" * 140)
        for r in rows:
            print(f"{r.t}  {r.regime_string}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Descriptive option-chain market regime")
    ap.add_argument("--date", default=DEFAULT_DATE)
    ap.add_argument("--changes-only", action="store_true")
    ap.add_argument("--out", default=None, help="write full table to this path")
    args = ap.parse_args()
    rows, meta = run_day(args.date)
    if args.out:
        import sys
        # tee-like: write full table to file
        from io import StringIO
        buf = StringIO()
        # monkey: print_table to stdout and capture by redirecting temporarily
        class _Tee:
            def __init__(self, *streams):
                self.streams = streams
            def write(self, data):
                for s in self.streams:
                    s.write(data)
            def flush(self):
                for s in self.streams:
                    s.flush()
        with open(args.out, "w", encoding="utf-8") as f:
            old = sys.stdout
            sys.stdout = _Tee(old, f)
            try:
                print_table(rows, meta, changes_only=False)
            finally:
                sys.stdout = old
    else:
        print_table(rows, meta, changes_only=args.changes_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
