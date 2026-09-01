# ============ FILE: regime_engine.py ============
"""
Regime engine implementing the 8-step vol-regime algorithm
from the reference nifty_regime_monitor.py.

Steps:
  1  Vol surface   : Term spread (V_fwd - V_spot) + 25d skew z-score
  2  Edge          : RV(20d) vs ATM IV
  3  Trend         : ADX(14) on 30-min bars + EMA-50 slope
  4  Flow          : Net delta-weighted OI change + spread ratio
  5  Persistence   : 3 consecutive identical readings to confirm
  6  Macro override: high-impact event -> EVENT_HEDGE
  7  Aggregation   : 0.30*Vol + 0.30*Edge + 0.25*Trend + 0.15*Flow
  8  Regime mapping: STRONG_SELL_VOL ... STRONG_BUY_VOL
"""

import logging
import sqlite3
import json
import asyncio
import math
import statistics
import numpy as np
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import pytz
import config
from data_manager import DataManager

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Reference algorithm constants (from nifty_regime_monitor.py)
# ─────────────────────────────────────────────────────────────────────
TERM_THRESHOLD   = 0.5    # V_fwd - V_spot contango/backwardation
SKEW_Z_STEEP = config.SKEW_ZSCORE_FEAR        # AUDIT #2.2: reads from config
SKEW_Z_FLAT = config.SKEW_ZSCORE_COMPLACENT  # AUDIT #2.2: reads from config
EDGE_RICH = config.EDGE_RICH        # AUDIT #2.2: reads from config
EDGE_CHEAP = config.EDGE_CHEAP       # AUDIT #2.2: reads from config
# AUDIT #2.2/#2.3: ADX_TREND now reads from config.
# config.ADX_TREND_THRESHOLD = 20 (calibrated for 30-min bars).
ADX_TREND = config.ADX_TREND_THRESHOLD
EMA_SLOPE_PCT    = 0.05   # |slope| > 0.05% of spot
RV_WINDOW        = 20     # trading days
RV_ANNUALISE     = 252
SKEW_HISTORY_DAYS = 30
SKEW_MIN_DAYS    = 3      # minimum history before z is trusted
SPREAD_AVG_MIN   = 60     # minutes for spread-ratio average
EVENT_PRE_HOURS  = 6
EVENT_POST_HOURS = 2
MODULES          = ["vol", "edge", "trend", "flow"]
# AUDIT #2.2: weights now read from config so tuning
# config.WEIGHT_* actually takes effect at runtime.
def _build_weights():
    return {
        "vol":   config.WEIGHT_VOL,
        "edge":  config.WEIGHT_EDGE,
        "trend": config.WEIGHT_TREND,
        "flow":  config.WEIGHT_FLOW,
    }
WEIGHTS = _build_weights()


def map_regime(x: float) -> str:
    """Reference algorithm regime mapping.
    AUDIT #2.2: thresholds now read from config so
    config.STRONG_SELL_THRESHOLD etc. actually take effect.
    """
    if x > config.STRONG_SELL_THRESHOLD:
        return "STRONG_SELL_VOL"
    if x >= config.MILD_SELL_THRESHOLD:
        return "MILD_SELL_VOL"
    if x > config.MILD_BUY_THRESHOLD:
        return "NEUTRAL"
    if x >= config.STRONG_BUY_THRESHOLD:
        return "BUY_VOL"
    return "STRONG_BUY_VOL"


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, T, iv_pct, r, is_call) -> Optional[float]:
    """Black-Scholes delta fallback."""
    if T <= 0 or iv_pct <= 0 or spot <= 0 or strike <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sq = math.sqrt(T)
    d1 = (
        math.log(spot / strike)
        + (r + 0.5 * (iv_pct / 100.0) ** 2) * T
    ) / (iv_pct / 100.0 * sq)
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def _wilder(vals: List[float], n: int) -> List[float]:
    if len(vals) < n:
        return []
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append((out[-1] * (n - 1) + v) / n)
    return out


def adx14(bars: List[Dict], n: int = 14) -> Optional[Tuple]:
    """Wilder ADX. bars: ascending [{"h","l","c"}]. -> (adx, +di, -di)."""
    if len(bars) < 2 * n + 2:
        return None
    trs, pdms, ndms = [], [], []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        pdms.append(up if (up > dn and up > 0) else 0.0)
        ndms.append(dn if (dn > up and dn > 0) else 0.0)
    atr   = _wilder(trs,  n)
    pdm_s = _wilder(pdms, n)
    ndm_s = _wilder(ndms, n)
    if not atr or len(atr) != len(pdm_s):
        return None
    pdi = [100.0 * p / a if a > 0 else 0.0 for p, a in zip(pdm_s, atr)]
    ndi = [100.0 * d / a if a > 0 else 0.0 for d, a in zip(ndm_s, atr)]
    dx  = [
        abs(a - b) / (a + b) * 100 if (a + b) > 0 else 0.0
        for a, b in zip(pdi, ndi)
    ]
    adx_line = _wilder(dx, n)
    if not adx_line:
        return None
    return adx_line[-1], pdi[-1], ndi[-1]


def ema_series(vals: List[float], span: int) -> List[float]:
    if len(vals) < span:
        return []
    k = 2.0 / (span + 1.0)
    e = [sum(vals[:span]) / span]
    for v in vals[span:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e


def realised_vol_pct(
    closes: List[float], window: int = RV_WINDOW
) -> Tuple[Optional[float], int]:
    closes = closes[-(window + 1):]
    if len(closes) < window + 1:
        return None, len(closes) - 1
    rets = [
        math.log(closes[i + 1] / closes[i])
        for i in range(len(closes) - 1)
        if closes[i] > 0 and closes[i + 1] > 0
    ]
    if len(rets) < window:
        return None, len(rets)
    return (
        statistics.stdev(rets) * math.sqrt(RV_ANNUALISE) * 100.0,
        len(rets),
    )


def quality(leg: Dict, min_oi: float = 50.0) -> bool:
    return (
        leg.get("oi") is not None and leg["oi"] >= min_oi
        and leg.get("bid") is not None and leg["bid"] > 0
        and leg.get("ask") is not None and leg["ask"] > 0
    )


def leg_delta(
    leg: Dict, spot: float, strike: float,
    T: float, rate: float, is_call: bool
) -> Optional[float]:
    """Prefer API greeks; fall back to Black-Scholes."""
    d = leg.get("delta")
    if d is not None and 0.01 < abs(d) < 0.99:
        return d
    iv = leg.get("iv")
    if iv and iv > 0:
        # PATCH (R1): leg["iv"] is stored as a DECIMAL (e.g. 0.12
        # for 12%), but bs_delta() divides its iv_pct parameter by
        # 100 again internally (expects a percentage like 12.0).
        # Previously this made sigma ~100x too small, pushing
        # fallback deltas to ~0/±1 whenever the API delta was
        # missing/out-of-range. Convert decimal -> percentage here.
        iv_pct = iv * 100.0 if iv < 1.5 else iv
        return bs_delta(spot, strike, T, iv_pct, rate, is_call)
    return None


def atm_strike_from_chain(
    chain: Dict[float, Dict], spot: float
) -> Optional[float]:
    if not chain:
        return None
    return min(chain.keys(), key=lambda k: abs(k - spot))


def years_to_expiry(expiry_iso: str, now: datetime) -> float:
    try:
        IST = pytz.timezone("Asia/Kolkata")
        exp_dt = datetime.strptime(
            expiry_iso, "%Y-%m-%d"
        ).replace(hour=15, minute=30, tzinfo=IST)
        return max(
            (exp_dt - now).total_seconds(), 1.0
        ) / (365.0 * 24 * 3600)
    except Exception:
        return 1.0 / 365.0


# ─────────────────────────────────────────────────────────────────────
# RegimeEngine
# ─────────────────────────────────────────────────────────────────────

class RegimeEngine:
    """
    Implements the 8-step vol-regime algorithm from the reference
    nifty_regime_monitor.py, adapted for async operation.
    """

    def __init__(
        self,
        data_manager: DataManager,
        db_path: str,
    ) -> None:
        self.dm      = data_manager
        self.db_path = db_path
        self._IST    = pytz.timezone(config.TZ)

        # Persistence buffers (3 consecutive readings to confirm)
        self._buf: Dict[str, List[int]] = {
            m: [] for m in MODULES
        }
        self._conf: Dict[str, int] = {
            m: 0 for m in MODULES
        }

        # Skew history (one value per calendar day)
        self._skew_history: List[Dict] = []

        # Flow snapshots (last 75 min)
        self._flow_snapshots: List[Dict] = []

        # Score history for debugging
        self.score_history: deque = deque(maxlen=288 * 10)

        # Public state
        self.raw_composite:     float = 0.0
        self.confirmed_regime:  str   = config.REGIME_NEUTRAL
        self.previous_regime:   str   = config.REGIME_NEUTRAL
        self.regime_changed:    bool  = False
        self.persistence_count: int   = 0
        self.last_refresh_time: Optional[datetime] = None

        # Expose confirmed scores for display
        self.confirmed_vol:   float = 0.0
        self.confirmed_edge:  float = 0.0
        self.confirmed_trend: float = 0.0
        self.confirmed_flow:  float = 0.0

        # Raw scores for display
        self._raw: Dict[str, Optional[float]] = {
            m: None for m in MODULES
        }
        self._detail: Dict[str, str] = {
            m: "not computed" for m in MODULES
        }

        self._refresh_lock  = asyncio.Lock()
        self._refresh_count = 0
        self._warmup_required = 1

        self._load_state()

    # ─────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────

    # ── Backward-compatible buffer properties ─────────────────────────
    # strategy_engine and display code reference these names

    @property
    def vol_buffer(self):
        return self._buf.get("vol", [])

    @property
    def edge_buffer(self):
        return self._buf.get("edge", [])

    @property
    def trend_buffer(self):
        return self._buf.get("trend", [])

    @property
    def flow_buffer(self):
        return self._buf.get("flow", [])


    async def refresh(self) -> str:
        async with self._refresh_lock:
            return await self._refresh_locked()

    async def _refresh_locked(self) -> str:
        logger.info("Regime refresh started")

        if self.dm.spot is None or self.dm.vix is None:
            logger.warning("Regime refresh skipped: spot/vix None")
            return self.confirmed_regime
        if not self.dm.option_chain:
            logger.warning("Regime refresh skipped: chain empty")
            return self.confirmed_regime

        self._refresh_count += 1
        now = datetime.now(self._IST)

        # Step 1: Vol surface
        raw_vol, vol_detail = self._module_vol(now)

        # Step 2: Edge (RV vs IV)
        raw_edge, edge_detail = self._module_edge()

        # Step 3: Trend (ADX + EMA slope)
        raw_trend, trend_detail = self._module_trend()

        # Step 4: Order flow
        raw_flow, flow_detail = self._module_flow(now)

        self._raw["vol"]   = raw_vol
        self._raw["edge"]  = raw_edge
        self._raw["trend"] = raw_trend
        self._raw["flow"]  = raw_flow
        self._detail["vol"]   = vol_detail
        self._detail["edge"]  = edge_detail
        self._detail["trend"] = trend_detail
        self._detail["flow"]  = flow_detail

        # Step 5: Persistence filter
        conf_vol   = self._persist("vol",   raw_vol)
        conf_edge  = self._persist("edge",  raw_edge)
        conf_trend = self._persist("trend", raw_trend)
        conf_flow  = self._persist("flow",  raw_flow)

        self.confirmed_vol   = float(conf_vol)
        self.confirmed_edge  = float(conf_edge)
        self.confirmed_trend = float(conf_trend)
        self.confirmed_flow  = float(conf_flow)

        # Step 6: Macro override
        macro_active, macro_name = self._check_macro_override(now)

        # Warmup gate
        if self._refresh_count <= self._warmup_required:
            new_regime = config.REGIME_NEUTRAL
            logger.info(
                f"Warmup ({self._refresh_count}/"
                f"{self._warmup_required}) — NEUTRAL"
            )
        elif macro_active:
            new_regime = config.REGIME_EVENT
            logger.info(f"Macro override: {macro_name}")
        else:
            # Step 7: Weighted aggregation
            # AUDIT #2.2: rebuild weights from config
            # each cycle so config.WEIGHT_* tuning is live.
            _live_weights = _build_weights()
            composite = sum(
                _live_weights[m] * self._conf[m]
                for m in MODULES
            )
            self.raw_composite = float(
                max(-1.0, min(1.0, composite))
            )
            # Step 8: Regime mapping
            new_regime = self._map_regime(self.raw_composite)

        # Detect change
        self.regime_changed = (
            new_regime != self.confirmed_regime
        )
        if self.regime_changed:
            self.previous_regime   = self.confirmed_regime
            self.confirmed_regime  = new_regime
            self.persistence_count = 1
            logger.info(
                f"REGIME CHANGE: "
                f"{self.previous_regime} -> "
                f"{self.confirmed_regime} | "
                f"composite={self.raw_composite:.4f}"
            )
        else:
            self.persistence_count += 1

        # Save state
        await self._save_regime_to_sqlite({
            "timestamp":         now.isoformat(),
            "vol_score":         conf_vol,
            "edge_score":        conf_edge,
            "trend_score":       conf_trend,
            "flow_score":        conf_flow,
            "composite_score":   self.raw_composite,
            "raw_regime":        new_regime,
            "confirmed_regime":  self.confirmed_regime,
            "persistence_count": self.persistence_count,
            "macro_override":    1 if macro_active else 0,
        })

        self._log_console_output(now)
        self.last_refresh_time = now

        self.score_history.append({
            "timestamp":  now.isoformat(),
            "raw_vol":    raw_vol,
            "raw_edge":   raw_edge,
            "raw_trend":  raw_trend,
            "raw_flow":   raw_flow,
            "conf_vol":   conf_vol,
            "conf_edge":  conf_edge,
            "conf_trend": conf_trend,
            "conf_flow":  conf_flow,
            "composite":  self.raw_composite,
            "regime":     self.confirmed_regime,
        })

        self._save_state()

        logger.info(
            f"Regime: {self.confirmed_regime} "
            f"composite={self.raw_composite:.4f} "
            f"persist={self.persistence_count}"
        )
        return self.confirmed_regime

    # ─────────────────────────────────────────────────────────────────
    # Step 1: Vol surface
    # ─────────────────────────────────────────────────────────────────

    def _module_vol(
        self, now: datetime
    ) -> Tuple[Optional[int], str]:
        """
        Reference algorithm Step 1:
        Term spread (V_fwd - V_spot) + 25d-Put/Call skew z-score.
        """
        notes = []
        active = self.dm.get_active_chain()
        if not active:
            return None, "no option chain"

        spot = self.dm.spot
        if not spot:
            return None, "no spot"

        vix = self.dm.vix or 0.0

        # ── Term spread ───────────────────────────────────────────────
        # V_fwd = ATM IV from far expiry (30-45 DTE)
        # V_spot = VIX (30-day implied vol proxy)
        v_fwd = self.dm.forward_iv
        if v_fwd is not None:
            # forward_iv is stored as decimal (e.g. 0.138)
            # vix is in percentage (e.g. 11.35)
            # Convert to same units: both as percentage
            v_fwd_pct  = v_fwd * 100.0
            v_spot_pct = vix
            t_spread   = v_fwd_pct - v_spot_pct
            if t_spread > TERM_THRESHOLD:
                term_score = 1    # contango = sell vol
            elif t_spread < -TERM_THRESHOLD:
                term_score = -1   # backwardation = buy vol
            else:
                term_score = 0
            term_txt = (
                f"T_spread {t_spread:+.2f}% "
                f"({'CONTANGO' if term_score==1 else 'BACKWARDATION' if term_score==-1 else 'FLAT'})"
            )
        else:
            term_score = 0
            term_txt   = "T_spread n/a (no far expiry)"
            notes.append("forward IV unavailable")

        # ── 25-delta skew z-score ─────────────────────────────────────
        near_expiry = self.dm._active_expiry
        if near_expiry:
            T = years_to_expiry(near_expiry, now)
        else:
            T = 7.0 / 365.0

        best_c = best_p = None
        for strike, data in active.items():
            c_leg = data.get("call", {})
            p_leg = data.get("put",  {})
            if (
                quality(c_leg, config.MIN_OI_LOTS)
                and c_leg.get("iv")
            ):
                d = leg_delta(
                    c_leg, spot, strike, T,
                    0.065, True,
                )
                if d is not None and (
                    best_c is None
                    or abs(d - 0.25) < abs(best_c[0] - 0.25)
                ):
                    best_c = (d, strike, c_leg)
            if (
                quality(p_leg, config.MIN_OI_LOTS)
                and p_leg.get("iv")
            ):
                d = leg_delta(
                    p_leg, spot, strike, T,
                    0.065, False,
                )
                if d is not None and (
                    best_p is None
                    or abs(d + 0.25) < abs(best_p[0] + 0.25)
                ):
                    best_p = (d, strike, p_leg)

        if (
            best_c and best_p
            and abs(best_c[0] - 0.25) < 0.15
            and abs(best_p[0] + 0.25) < 0.15
        ):
            # iv stored as decimal → convert to % for skew
            iv_c = best_c[2]["iv"] * 100.0
            iv_p = best_p[2]["iv"] * 100.0
            skew = iv_p - iv_c

            # Record daily skew
            today_iso = now.date().isoformat()
            self._record_skew(skew, today_iso)
            z, ndays = self._skew_zscore(today_iso)

            if z is None:
                notes.append(
                    f"skew z warming up ({ndays}/{SKEW_MIN_DAYS} days)"
                )
                skew_score = 0
                skew_txt   = (
                    f"skew25 {skew:+.2f}% "
                    f"(z warming {ndays}/{SKEW_MIN_DAYS}d)"
                )
            else:
                if z > SKEW_Z_STEEP:
                    skew_score = -1   # fear = buy vol
                elif z < SKEW_Z_FLAT:
                    skew_score = +1   # complacency = sell vol
                else:
                    skew_score = 0
                skew_txt = (
                    f"skew25 {skew:+.2f}% "
                    f"(z={z:+.2f}, {ndays}d)"
                )
        else:
            skew_score = 0
            skew_txt   = "skew25 n/a (illiquid chain)"
            notes.append("25-delta legs not found")

        vol_score = 0.5 * term_score + 0.5 * skew_score
        detail = f"{term_txt} | {skew_txt}"
        if notes:
            detail += " | " + "; ".join(notes)

        logger.info(
            f"Vol: score={vol_score:.2f} "
            f"term={term_score} skew={skew_score} | "
            f"{detail}"
        )
        return vol_score, detail

    # ─────────────────────────────────────────────────────────────────
    # Step 2: Edge (RV vs IV)
    # ─────────────────────────────────────────────────────────────────

    def _module_edge(self) -> Tuple[Optional[int], str]:
        """
        Reference algorithm Step 2:
        IV_atm - RV(20d). Rich = sell vol, Cheap = buy vol.
        """
        # Get RV — use estimated if actual not available
        rv = self.dm.get_estimated_rv()
        if rv is None:
            return None, "RV unavailable (no daily candles or VIX)"

        # rv is in decimal (e.g. 0.08 = 8%)
        # Convert to percentage
        rv_pct = rv * 100.0

        active = self.dm.get_active_chain()
        if not active:
            return None, "no chain"

        spot = self.dm.spot
        if not spot:
            return None, "no spot"

        atm = atm_strike_from_chain(active, spot)
        if atm is None:
            return None, "ATM not found"

        atm_data = active[atm]
        # iv stored as decimal → convert to %
        ivs = [
            v * 100.0
            for v in (
                atm_data.get("call", {}).get("iv"),
                atm_data.get("put",  {}).get("iv"),
            )
            if v and v > 0
        ]
        if not ivs:
            return None, "ATM IV unavailable"

        iv_atm = statistics.mean(ivs)
        edge   = iv_atm - rv_pct

        if edge > EDGE_RICH:
            raw = 1
            tag = "RICH (seller edge)"
        elif edge < EDGE_CHEAP:
            raw = -1
            tag = "CHEAP (buyer edge)"
        else:
            raw = 0
            tag = "FAIR"

        rv_src = "actual" if self.dm.rv_20d else "est(VIX×0.70)"
        detail = (
            f"IV_atm {iv_atm:.2f}% - "
            f"RV{RV_WINDOW} {rv_pct:.2f}%({rv_src}) = "
            f"{edge:+.2f} -> {tag}"
        )
        logger.info(f"Edge: score={raw} | {detail}")
        return raw, detail

    # ─────────────────────────────────────────────────────────────────
    # Step 3: Trend (ADX + EMA slope)
    # ─────────────────────────────────────────────────────────────────

    def _module_trend(self) -> Tuple[Optional[int], str]:
        """
        Reference algorithm Step 3:
        ADX(14) on 30-min bars + EMA-50 slope.
        """
        bars = list(self.dm.candles_30m)
        if len(bars) < 75:
            return None, f"only {len(bars)} bars (need 75)"

        spot = self.dm.spot
        if not spot:
            return None, "no spot"

        closes = [b.get("close", b.get("c", 0)) for b in bars]
        ax = adx14([
            {"h": b["high"], "l": b["low"], "c": b["close"]}
            for b in bars
        ])
        ema = ema_series(closes, 50)

        if ax is None or len(ema) < 21:
            return None, "indicator warmup"

        adx_v, pdi, ndi = ax
        slope     = ema[-1] - ema[-21]
        slope_pct = slope / spot * 100.0 if spot else 0.0
        above     = spot > ema[-1]

        if adx_v > ADX_TREND and abs(slope_pct) > EMA_SLOPE_PCT:
            raw  = 1 if above else -1
            dirn = "bullish" if above else "bearish"
        else:
            raw  = 0
            dirn = "range-bound"

        detail = (
            f"ADX {adx_v:.1f} "
            f"(+DI {pdi:.0f}/-DI {ndi:.0f}) | "
            f"EMA50 slope {slope_pct:+.3f}% | "
            f"spot {'>' if above else '<'} EMA50 -> {dirn}"
        )
        logger.info(f"Trend: score={raw} | {detail}")
        return raw, detail

    # ─────────────────────────────────────────────────────────────────
    # Step 4: Order flow
    # ─────────────────────────────────────────────────────────────────

    def _module_flow(
        self, now: datetime
    ) -> Tuple[Optional[int], str]:
        """
        Reference algorithm Step 4:
        Net delta-weighted OI change (15 min) +
        3rd-OTM-put spread ratio vs 1h average.
        """
        active = self.dm.get_active_chain()
        if not active:
            return None, "no chain"

        spot = self.dm.spot
        if not spot:
            return None, "no spot"

        near_expiry = self.dm._active_expiry or ""
        T = years_to_expiry(near_expiry, now)

        # Record this cycle's snapshot
        strikes_snap = {}
        for strike, data in active.items():
            c_leg = data.get("call", {})
            p_leg = data.get("put",  {})
            coi   = c_leg.get("oi")
            poi   = p_leg.get("oi")
            if coi is not None and coi < config.MIN_OI_LOTS:
                coi = None
            if poi is not None and poi < config.MIN_OI_LOTS:
                poi = None
            if coi is None and poi is None:
                continue
            cd = (
                leg_delta(c_leg, spot, strike, T, 0.065, True)
                if coi is not None else None
            )
            pd = (
                leg_delta(p_leg, spot, strike, T, 0.065, False)
                if poi is not None else None
            )
            strikes_snap[f"{strike:.0f}"] = [coi, poi, cd, pd]

        # 3rd OTM put spread ratio
        otm_puts = [
            (strike, data["put"])
            for strike, data in active.items()
            if strike < spot and quality(data.get("put", {}), config.MIN_OI_LOTS)
        ]
        spr = None
        spr_strike = None
        if len(otm_puts) >= 3:
            third = sorted(otm_puts, key=lambda x: x[0], reverse=True)[2]
            bid, ask = third[1].get("bid"), third[1].get("ask")
            if bid and ask:
                mid = (bid + ask) / 2.0
                if mid > 0:
                    spr        = (ask - bid) / mid
                    spr_strike = third[0]

        self._add_flow_snapshot({
            "ts":      now.isoformat(),
            "strikes": strikes_snap,
            "spr":     spr,
            "spot":    spot,
        })

        # Net delta-weighted OI change vs ~15 min ago
        ref = self._snapshot_near(
            now,
            min_age_s=600,
            target_age_s=900,
            max_age_s=1800,
        )
        net_flow = None
        if ref:
            dcall = dput = 0.0
            for k, (coi, poi, cd, pd) in strikes_snap.items():
                old = ref["strikes"].get(k)
                if not old:
                    continue
                if coi is not None and old[0] is not None and cd is not None:
                    dcall += (coi - old[0]) * cd
                if poi is not None and old[1] is not None and pd is not None:
                    dput  += (poi - old[1]) * pd
            net_flow = dcall + dput

        # Spread ratio vs 1h average
        spr_avg = None
        hist    = []
        for s in self._flow_snapshots:
            if s.get("spr") is None:
                continue
            try:
                age = (
                    now - datetime.fromisoformat(s["ts"])
                ).total_seconds()
            except ValueError:
                continue
            if age <= 3600:
                hist.append((s["ts"], s["spr"]))
        if len(hist) >= 3:
            span_min = (
                now - datetime.fromisoformat(hist[0][0])
            ).total_seconds() / 60.0
            if span_min >= 20:
                spr_avg = statistics.mean(v for _, v in hist)

        if net_flow is None or spr_avg is None:
            why = []
            if net_flow is None:
                why.append("net-flow warming (needs 10-30 min old snapshot)")
            if spr_avg is None:
                why.append("spread baseline warming (needs ~20 min history)")
            detail = (
                f"Net_dOI(15m): {'n/a' if net_flow is None else f'{net_flow:+,.0f}'} | "
                f"SPR({spr_strike or '-'}): "
                f"{f'{spr:.4f}' if spr is not None else 'n/a'} vs "
                f"1h avg {f'{spr_avg:.4f}' if spr_avg is not None else 'n/a'} | "
                + "; ".join(why)
            )
            return None, detail

        if spr < spr_avg * 0.985:
            spr_state = "CONTRACTING"
        elif spr > spr_avg * 1.015:
            spr_state = "WIDENING"
        else:
            spr_state = "FLAT"

        if net_flow > 0 and spr_state == "CONTRACTING":
            raw = 1
            tag = "aggressive bullish flow"
        elif net_flow < 0 and spr_state == "WIDENING":
            raw = -1
            tag = "defensive/panic flow"
        else:
            raw = 0
            tag = "mixed"

        detail = (
            f"Net_dOI(15m) {net_flow:+,.0f} | "
            f"SPR {spr:.4f} vs avg {spr_avg:.4f} "
            f"-> {spr_state} | {tag}"
        )
        logger.info(f"Flow: score={raw} | {detail}")
        return raw, detail

    # ─────────────────────────────────────────────────────────────────
    # Step 5: Persistence filter
    # ─────────────────────────────────────────────────────────────────

    def _persist(
        self, name: str, raw: Optional[float]
    ) -> int:
        """
        Reference algorithm Step 5:
        3 consecutive identical readings to confirm.
        raw=None -> hold previous confirmed value.
        """
        if raw is None:
            return self._conf[name]

        raw_int = int(round(raw))
        buf     = self._buf[name]
        buf.append(raw_int)
        if len(buf) > 3:
            buf.pop(0)

        if len(buf) == 3 and buf[0] == buf[1] == buf[2]:
            self._conf[name] = raw_int
            logger.info(
                f"Persistence confirmed: {name}={raw_int}"
            )
        else:
            logger.info(
                f"Persistence unconfirmed: {name} "
                f"buf={buf} "
                f"holding={self._conf[name]}"
            )
        return self._conf[name]

    # ─────────────────────────────────────────────────────────────────
    # Step 6: Macro override
    # ─────────────────────────────────────────────────────────────────

    def _check_macro_override(
        self, now: datetime
    ) -> Tuple[bool, str]:
        for event_date_str, event_name in (
            config.HIGH_IMPACT_EVENTS.items()
        ):
            try:
                event_dt = self._IST.localize(
                    datetime.strptime(
                        event_date_str, "%Y-%m-%d"
                    ).replace(
                        hour=9, minute=15,
                        second=0, microsecond=0,
                    )
                )
                diff_h = (
                    (now - event_dt).total_seconds() / 3600.0
                )
                # Skip events > 7 days past
                if diff_h > 7 * 24:
                    continue
                if diff_h < 0:
                    if abs(diff_h) <= EVENT_PRE_HOURS:
                        return True, event_name
                else:
                    if diff_h <= EVENT_POST_HOURS:
                        return True, event_name
            except Exception:
                continue
        return False, ""

    # ─────────────────────────────────────────────────────────────────
    # Step 8: Regime mapping
    # ─────────────────────────────────────────────────────────────────

    def _map_regime(self, composite: float) -> str:
        """Reference algorithm regime mapping.
        AUDIT #2.2: reads thresholds from config.
        """
        if composite > config.STRONG_SELL_THRESHOLD:
            return config.REGIME_STRONG_SELL
        if composite >= config.MILD_SELL_THRESHOLD:
            return config.REGIME_MILD_SELL
        if composite > config.MILD_BUY_THRESHOLD:
            return config.REGIME_NEUTRAL
        if composite >= config.STRONG_BUY_THRESHOLD:
            return config.REGIME_BUY_VOL
        return config.REGIME_STRONG_BUY

    # ─────────────────────────────────────────────────────────────────
    # Skew history helpers
    # ─────────────────────────────────────────────────────────────────

    def _record_skew(self, skew: float, today_iso: str) -> None:
        h = [
            e for e in self._skew_history
            if e.get("date") != today_iso
        ]
        h.append({"date": today_iso, "skew": round(skew, 4)})
        h.sort(key=lambda e: e["date"])
        self._skew_history = h[-60:]

    def _skew_zscore(
        self, today_iso: str
    ) -> Tuple[Optional[float], int]:
        hist = [
            e["skew"] for e in self._skew_history
            if e.get("date") != today_iso
        ][-SKEW_HISTORY_DAYS:]
        if len(hist) < SKEW_MIN_DAYS:
            return None, len(hist)
        sd = statistics.stdev(hist) if len(hist) > 1 else 0.0
        if sd < 1e-9:
            return None, len(hist)
        cur = next(
            (
                e["skew"] for e in reversed(self._skew_history)
                if e["date"] == today_iso
            ),
            None,
        )
        if cur is None:
            return None, len(hist)
        return (cur - statistics.mean(hist)) / sd, len(hist)

    # ─────────────────────────────────────────────────────────────────
    # Flow snapshot helpers
    # ─────────────────────────────────────────────────────────────────

    def _add_flow_snapshot(self, snap: Dict) -> None:
        self._flow_snapshots.append(snap)
        cutoff = (
            datetime.now(self._IST) - timedelta(minutes=75)
        ).isoformat()
        self._flow_snapshots = [
            s for s in self._flow_snapshots
            if s.get("ts", "") >= cutoff
        ]

    def _snapshot_near(
        self,
        now: datetime,
        min_age_s: float,
        target_age_s: float,
        max_age_s: float,
    ) -> Optional[Dict]:
        best, best_d = None, None
        for s in reversed(self._flow_snapshots):
            try:
                age = (
                    now - datetime.fromisoformat(s["ts"])
                ).total_seconds()
            except (KeyError, ValueError):
                continue
            if min_age_s <= age <= max_age_s:
                d = abs(age - target_age_s)
                if best_d is None or d < best_d:
                    best, best_d = s, d
        return best

    # ─────────────────────────────────────────────────────────────────
    # State persistence
    # ─────────────────────────────────────────────────────────────────

    def _save_state(self) -> None:
        """Save skew history and flow snapshots to SQLite."""
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS regime_algo_state (
                    id           INTEGER PRIMARY KEY,
                    key          TEXT UNIQUE,
                    value_json   TEXT,
                    updated_at   TEXT
                )
            """)
            now_str = datetime.now(self._IST).isoformat()
            for key, value in [
                ("skew_history",    self._skew_history),
                ("flow_snapshots",  self._flow_snapshots[-20:]),
                ("buffers",         self._buf),
                ("confirmed",       self._conf),
            ]:
                cursor.execute("""
                    INSERT OR REPLACE INTO regime_algo_state
                    (id, key, value_json, updated_at)
                    VALUES (
                        (SELECT id FROM regime_algo_state WHERE key=?),
                        ?, ?, ?
                    )
                """, (key, key, json.dumps(value), now_str))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"_save_state error: {e}")

    def _load_state(self) -> None:
        """Restore skew history and buffers from SQLite."""
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT key, value_json FROM regime_algo_state
            """)
            rows = cursor.fetchall()
            conn.close()
            for key, val in rows:
                try:
                    data = json.loads(val)
                    if key == "skew_history":
                        self._skew_history = data
                    elif key == "flow_snapshots":
                        self._flow_snapshots = data
                    elif key == "buffers":
                        self._buf = {
                            m: data.get(m, [])
                            for m in MODULES
                        }
                    elif key == "confirmed":
                        self._conf = {
                            m: int(data.get(m, 0))
                            for m in MODULES
                        }
                except Exception:
                    pass
            logger.info("Regime algo state loaded from SQLite")
        except sqlite3.OperationalError:
            logger.info("No regime_algo_state table — fresh start")
        except Exception as e:
            logger.warning(f"_load_state error: {e}")

    async def _save_regime_to_sqlite(self, data: Dict) -> None:
        def _write():
            try:
                conn   = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO regime_history (
                        timestamp, vol_score, edge_score,
                        trend_score, flow_score,
                        composite_score, raw_regime,
                        confirmed_regime, persistence_count,
                        macro_override
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    data.get("timestamp"),
                    data.get("vol_score"),
                    data.get("edge_score"),
                    data.get("trend_score"),
                    data.get("flow_score"),
                    data.get("composite_score"),
                    data.get("raw_regime"),
                    data.get("confirmed_regime"),
                    data.get("persistence_count"),
                    data.get("macro_override", 0),
                ))
                cursor.execute("""
                    DELETE FROM regime_history
                    WHERE id NOT IN (
                        SELECT id FROM regime_history
                        ORDER BY id DESC LIMIT 5000
                    )
                """)
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                logger.warning(f"_save_regime error: {e}")
        try:
            await asyncio.to_thread(_write)
        except Exception as e:
            logger.warning(f"_save_regime thread error: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Backward-compatible methods (used by strategy_engine + main)
    # ─────────────────────────────────────────────────────────────────

    def load_buffers_from_sqlite(self) -> None:
        """Called at startup — loads saved state."""
        self._load_state()

    def save_buffers_to_sqlite(self) -> None:
        """Called periodically — saves state."""
        self._save_state()

    def _log_console_output(self, now: datetime) -> None:
        COLORS = {
            config.REGIME_STRONG_SELL: "\033[92m",
            config.REGIME_MILD_SELL:   "\033[32m",
            config.REGIME_NEUTRAL:     "\033[93m",
            config.REGIME_BUY_VOL:     "\033[91m",
            config.REGIME_STRONG_BUY:  "\033[31m",
            config.REGIME_EVENT:       "\033[95m",
        }
        RESET = "\033[0m"
        color = COLORS.get(self.confirmed_regime, "")
        spot_str = f"{self.dm.spot:.2f}" if self.dm.spot else "N/A"
        vix_str  = f"{self.dm.vix:.2f}"  if self.dm.vix  else "N/A"

        # Show raw vs confirmed for each module
        def fmt(raw, conf):
            r = f"{raw:+.2f}" if raw is not None else " n/a"
            c = f"{conf:+.2f}"
            return f"raw={r} conf={c}"

        print(
            f"\n[{now.strftime('%H:%M:%S')}] "
            f"Spot={spot_str} VIX={vix_str} "
            f"Composite={self.raw_composite:+.4f} "
            f"Regime={color}{self.confirmed_regime}{RESET} "
            f"(persist={self.persistence_count})"
        )
        print(
            f"  Vol:   {fmt(self._raw['vol'],   self._conf['vol'])}   "
            f"{self._detail['vol'][:60]}"
        )
        print(
            f"  Edge:  {fmt(self._raw['edge'],  self._conf['edge'])}   "
            f"{self._detail['edge'][:60]}"
        )
        print(
            f"  Trend: {fmt(self._raw['trend'], self._conf['trend'])}   "
            f"{self._detail['trend'][:60]}"
        )
        print(
            f"  Flow:  {fmt(self._raw['flow'],  self._conf['flow'])}   "
            f"{self._detail['flow'][:60]}"
        )

    def get_regime_action_description(self) -> str:
        actions = {
            config.REGIME_STRONG_SELL: (
                "SELL PREMIUM: Straddle/Condor"
            ),
            config.REGIME_MILD_SELL: (
                "SELL DEFINED: Credit Spreads"
            ),
            config.REGIME_NEUTRAL: (
                "HOLD: Manage existing only"
            ),
            config.REGIME_BUY_VOL: (
                "BUY VOL: Butterfly/Defensive Hedge"
            ),
            config.REGIME_STRONG_BUY: (
                "BUY VOL: Long Straddle/Backspread"
            ),
            config.REGIME_EVENT: (
                "EVENT HEDGE: Flatten shorts, long gamma"
            ),
        }
        return actions.get(self.confirmed_regime, "UNKNOWN")
