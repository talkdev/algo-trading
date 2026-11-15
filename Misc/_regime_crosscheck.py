"""Cross-check OR / price / vol regime labels vs tape math on flat & green days."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

DB = r"C:\Users\Administrator\Desktop\algo-trading\data\nifty_algo_v3.db"

OR_PCT = [0.0020, 0.0036, 0.0055, 0.0078]
OR_STR = [0.24, 0.42, 0.62, 0.86]
BANDS = ["VERY_NARROW", "NARROW", "MODERATE", "WIDE", "VERY_WIDE"]

FLAT = [
    "2026-09-18", "2026-09-23", "2026-09-28", "2026-09-29", "2026-10-06",
    "2026-10-22", "2026-10-26", "2026-10-29", "2026-11-02", "2026-11-03",
    "2026-11-04", "2026-11-06", "2026-11-09", "2026-11-12",
]
GREEN = [
    "2026-09-09", "2026-09-10", "2026-09-16", "2026-10-05", "2026-10-08",
    "2026-10-15", "2026-10-21", "2026-11-11",
]


def band_idx(frac: Optional[float], srat: Optional[float]) -> tuple[int, int, str]:
    idx_pct = 4
    if frac is not None:
        if frac < OR_PCT[0]:
            idx_pct = 0
        elif frac < OR_PCT[1]:
            idx_pct = 1
        elif frac < OR_PCT[2]:
            idx_pct = 2
        elif frac < OR_PCT[3]:
            idx_pct = 3
    idx_str = idx_pct
    if srat is not None:
        if srat < OR_STR[0]:
            idx_str = 0
        elif srat < OR_STR[1]:
            idx_str = 1
        elif srat < OR_STR[2]:
            idx_str = 2
        elif srat < OR_STR[3]:
            idx_str = 3
        else:
            idx_str = 4
    return idx_pct, idx_str, BANDS[max(idx_pct, idx_str)]


def fetch(con: sqlite3.Connection, d: str, after: str = "11:45") -> Optional[tuple]:
    return con.execute(
        """
        SELECT substr(cycle_time,12,8), or_width, or_condition, opening_straddle_pts,
               spot, price_regime, vol_regime, final_regime,
               adx_15, ema_structure, iv_behavior, day_move_used_pct,
               confidence_level, action_taken, no_trade_reason, vrp_smoothed,
               vwap_dist_pct, raw_json
        FROM cycle_log
        WHERE trading_date=? AND substr(cycle_time,12,5)>=?
          AND or_condition IS NOT NULL
        ORDER BY cycle_time LIMIT 1
        """,
        (d, after),
    ).fetchone()


def day_range(con: sqlite3.Connection, d: str):
    return con.execute(
        "SELECT MIN(spot), MAX(spot), MAX(spot)-MIN(spot) "
        "FROM cycle_log WHERE trading_date=? AND spot>0",
        (d,),
    ).fetchone()


def price_should_be(
    spot: float,
    oh: float,
    ol: float,
    adx: float,
    ema: str,
    adx_trend: float = 20.0,
    adx_strong: float = 28.0,
) -> str:
    """Recompute mature classify_price core (ignores choppy/stale/vwap bridges)."""
    if oh <= 0 or ol <= 0 or spot <= 0:
        return "?"
    if (
        adx > adx_strong
        and ema == "BEARISH"
        and spot < ol - 100
    ):
        return "STRONG_DOWNTREND"
    if (
        adx > adx_strong
        and ema == "BULLISH"
        and spot > oh + 100
    ):
        return "STRONG_UPTREND"
    if (
        adx >= adx_trend
        and ema in ("BEARISH", "TRANSITIONAL")
        and spot < ol - 20
    ):
        return "DOWNTREND"
    if (
        adx >= adx_trend
        and ema in ("BULLISH", "TRANSITIONAL")
        and spot > oh + 20
    ):
        return "UPTREND"
    return "RANGE"


def main() -> None:
    con = sqlite3.connect(DB)
    print("=== REGIME CROSS-CHECK (midday ~11:45+) ===\n")
    n_dead = 0
    n_mismatch_or = 0
    n_px_disagree = 0
    for label, days in [("FLAT", FLAT), ("GREEN", GREEN)]:
        print(f"----- {label} -----")
        for d in days:
            r = fetch(con, d)
            if not r:
                print(d, "NO ROW")
                continue
            (
                t, ow, oc, st, sp, pr, vr, fr, adx, ema, iv, dm,
                conf, act, ntr, vrp, vd, raw,
            ) = r
            js: dict[str, Any] = {}
            if raw:
                try:
                    js = json.loads(raw)
                except Exception:
                    js = {}
            ow = float(ow or js.get("or_width") or 0)
            sp = float(sp or js.get("spot") or 0)
            st = float(st or js.get("opening_straddle_pts") or 0)
            oh = float(js.get("or_high") or 0)
            ol = float(js.get("or_low") or 0)
            adx_f = float(adx or 0)
            ema_s = str(ema or js.get("ema_structure") or "")
            frac = (ow / sp) if sp > 0 else None
            srat = (ow / st) if st > 20 else None
            ip, iss, recon = band_idx(frac, srat)
            inside = bool(ol and oh and sp and ol <= sp <= oh)
            dist_or = 0.0
            if oh and ol and sp:
                if sp > oh:
                    dist_or = sp - oh
                elif sp < ol:
                    dist_or = ol - sp
            _, _, rng = day_range(con, d)
            mismatch = oc != recon
            if mismatch:
                n_mismatch_or += 1
            deadzone = (
                oc in ("WIDE", "VERY_WIDE")
                and str(pr) == "RANGE"
                and inside
            )
            if deadzone and label == "FLAT":
                n_dead += 1
            px_core = price_should_be(sp, oh, ol, adx_f, ema_s)
            px_disagree = str(pr) != px_core and not (
                # STRONG_* vs plain is softer disagreement
                str(pr).replace("STRONG_", "") == px_core.replace("STRONG_", "")
            )
            if px_disagree:
                n_px_disagree += 1
            notes = str(js.get("final_regime_notes") or "")[:90]
            orb = js.get("orb_price_regime")
            print(
                f"{d} {t} lab={oc} recon={recon} "
                f"(pct={BANDS[ip]} str={BANDS[iss] if srat is not None else 'n/a'}) "
                f"ow={ow:.0f} stradd={st:.0f} "
                f"frac_bps={(frac or 0)*1e4:.1f} "
                f"srat={0.0 if srat is None else srat:.2f}"
            )
            print(
                f"  px={pr} px_core={px_core} orb={orb} vol={vr} final={fr} "
                f"adx={round(adx_f,1)} ema={ema_s} iv={iv} "
                f"dm={None if dm is None else round(float(dm),0)} "
                f"conf={conf} vrp={vrp} insideOR={inside} "
                f"distOR={dist_or:.0f} dayRange={None if not rng else round(float(rng),0)}"
            )
            print(
                f"  or_mismatch={mismatch} wide_inside_RANGE_deadzone={deadzone} "
                f"px_disagree={px_disagree} act={act}"
            )
            print(f"  notes={notes}")
            print(f"  reason={(ntr or '')[:110]}")
        print()
    print(
        f"SUMMARY flat_wide_inside_RANGE={n_dead} "
        f"or_label_mismatches={n_mismatch_or} "
        f"price_core_disagreements={n_px_disagree}"
    )


if __name__ == "__main__":
    main()
