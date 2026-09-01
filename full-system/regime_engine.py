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
# CAL-05: raised to 1.5 to match config.TERM_SPREAD_CONTANGO.
# At 0.5pp, far-month ATM IV vs VIX always exceeded the threshold
# at VIX=11 (typical spread 1-2pp), permanently injecting +0.15
# into the composite as a structural sell-vol bias.
TERM_THRESHOLD   = 0.8    # P3-2: TERM_THRESHOLD lowered (was 1.5pp; NIFTY near-vs-far weekly spread is 0.3-1.0pp so 1.5 never fired)
SKEW_Z_STEEP = config.SKEW_ZSCORE_FEAR        # AUDIT #2.2: reads from config
SKEW_Z_FLAT = config.SKEW_ZSCORE_COMPLACENT  # AUDIT #2.2: reads from config
# CFG-01: reads from config (now symmetric ±2.0)
EDGE_RICH = config.EDGE_RICH        # AUDIT #2.2: reads from config
EDGE_CHEAP = config.EDGE_CHEAP       # AUDIT #2.2: reads from config
# AUDIT #2.2/#2.3: ADX_TREND now reads from config.
# config.ADX_TREND_THRESHOLD = 20 (calibrated for 30-min bars).
ADX_TREND = config.ADX_TREND_THRESHOLD
# CAL-04: raised to 0.15 to match config.EMA_SLOPE_THRESHOLD.
# At 0.05% (~12.5 pts over 10h), the condition fired on minor drift
# making trend=-1 (trending) ~80%+ of sessions. The +1 (range-bound,
# favorable for premium selling) almost never fired.
EMA_SLOPE_PCT    = 0.15   # |slope| > 0.15% of spot
RV_WINDOW        = 20     # trading days
RV_ANNUALISE     = 252
SKEW_HISTORY_DAYS = 30
# RE-C1: raised from 3 to 20. Same argument as EDGE_SCORE_MIN_HISTORY:
# 3-sample std has ~40% error. Skew carries 0.15 composite weight
# and should not be live on 3 samples when edge is gated at 20.
SKEW_MIN_DAYS    = 10     # PATCHED: 20→10 (2-week warmup sufficient)
SPREAD_AVG_MIN   = 60     # minutes for spread-ratio average
EVENT_PRE_HOURS  = 6
EVENT_POST_HOURS = 2
MODULES          = ["vol", "edge", "trend", "flow"]
# AUDIT #2.2: weights now read from config so tuning
# config.WEIGHT_* actually takes effect at runtime.
def _build_weights(flow_none_frac=0.0):
    """PATCH R-07: redistribute flow weight when flow is frequently None.
    flow_none_frac: fraction of recent cycles where flow returned None.
    When > FLOW_WEIGHT_NONE_THRESHOLD, flow weight is set to 0 and
    redistributed proportionally to vol, edge, trend.
    """
    _threshold = getattr(config, "FLOW_WEIGHT_NONE_THRESHOLD", 0.50)
    wv = config.WEIGHT_VOL
    we = config.WEIGHT_EDGE
    wt = config.WEIGHT_TREND
    wf = config.WEIGHT_FLOW
    if flow_none_frac > _threshold and wf > 0:
        # Redistribute flow weight proportionally to other three modules
        _other_sum = wv + we + wt
        if _other_sum > 0:
            _scale = (wv + we + wt + wf) / _other_sum
            wv = round(wv * _scale, 6)
            we = round(we * _scale, 6)
            wt = round(wt * _scale, 6)
            wf = 0.0
    return {
        "vol":   wv,
        "edge":  we,
        "trend": wt,
        "flow":  wf,
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


def bs_delta(
    spot, strike, T, iv_pct, r, is_call,
    q: float = 0.012,
) -> Optional[float]:
    """Black-Scholes delta fallback with dividend yield.

    RE-IV-02: NIFTY 50 has ~1.2% annual dividend yield.
    Omitting q overestimates call deltas and underestimates put
    deltas by ~0.5-1.0 delta points for 30-day ATM options,
    creating a persistent upward skew bias in the vol module.
    Uses cost-of-carry model: F = S * exp((r - q) * T).
    q defaults to 0.012 (1.2% NIFTY historical dividend yield).
    """
    if T <= 0 or iv_pct <= 0 or spot <= 0 or strike <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sq = math.sqrt(T)
    sigma = iv_pct / 100.0
    # Cost-of-carry: use forward price F = S * exp((r-q)*T)
    # d1 = [ln(F/K) + 0.5*sigma^2*T] / (sigma*sqrt(T))
    forward = spot * math.exp((r - q) * T)
    d1 = (
        math.log(forward / strike)
        + 0.5 * sigma ** 2 * T
    ) / (sigma * sq)
    # Delta = exp(-q*T) * N(d1) for call, exp(-q*T) * (N(d1)-1) for put
    disc = math.exp(-q * T)
    return disc * norm_cdf(d1) if is_call else disc * (norm_cdf(d1) - 1.0)


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
        # RE-P2-04: use localize() not replace(tzinfo=IST).
        # replace() attaches LMT offset (+05:53), not IST (+05:30)
        # — a 23-minute error in T that distorts every BS delta.
        exp_naive = datetime.strptime(
            expiry_iso, "%Y-%m-%d"
        ).replace(hour=15, minute=30)
        exp_dt = IST.localize(exp_naive)
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
        # P0-1: composite history deque (was never initialised)
        self._composite_history: deque = deque(maxlen=288)

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
        # AUDIT RE-07: was 1 cycle. After a restart, _conf values
        # are reloaded from SQLite (potentially stale). Require 3
        # cycles before acting so the persistence filter has had
        # a chance to confirm or reject the reloaded values.
        self._warmup_required = 3

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

        # AUDIT RE-N03: macro override must precede warmup gate.
        # A restart on an event day must not suppress EVENT_HEDGE.
        if macro_active:
            new_regime = config.REGIME_EVENT
            logger.info(f"Macro override: {macro_name}")
        # Warmup gate (after macro check)
        elif self._refresh_count <= self._warmup_required:
            new_regime = config.REGIME_NEUTRAL
            logger.info(
                f"Warmup ({self._refresh_count}/"
                f"{self._warmup_required}) — NEUTRAL"
            )
        else:
            # Step 7: Weighted aggregation
            # AUDIT #2.2: rebuild weights from config
            # each cycle so config.WEIGHT_* tuning is live.
            # P2-2: flow_none_frac computed and passed to _build_weights
            # so weight redistribution actually fires when flow is
            # frequently None (first 20 min of session, expiry days, etc.)
            _flow_lookback = getattr(
                config, "FLOW_WEIGHT_NONE_LOOKBACK", 10
            )
            _recent_score_history = list(
                self.score_history
            )[-_flow_lookback:]
            if _recent_score_history:
                _flow_none_count = sum(
                    1 for _e in _recent_score_history
                    if _e.get("raw_flow") is None
                )
                _flow_none_frac = (
                    _flow_none_count
                    / len(_recent_score_history)
                )
            else:
                _flow_none_frac = 0.0
            _live_weights = _build_weights(
                flow_none_frac=_flow_none_frac
            )
            composite = sum(
                _live_weights[m] * self._conf[m]
                for m in MODULES
            )
            self.raw_composite = float(
                max(-1.0, min(1.0, composite))
            )
            # P0-2: single append (duplicate removed)
            self._composite_history.append(self.raw_composite)
            # RE-B2: gate STRONG_SELL on minimum confirming modules.
            # Count modules with a non-zero confirmed score AND a
            # non-None raw reading this cycle (genuinely live).
            _min_modules = getattr(
                config,
                "STRONG_SELL_MIN_CONFIRMING_MODULES",
                3,
            )
            _live_nonzero = sum(
                1 for m in MODULES
                if self._conf.get(m, 0.0) != 0.0
                and self._raw.get(m) is not None
            )

            # Step 8: Regime mapping
            new_regime = self._map_regime(self.raw_composite)
            # Cap at MILD_SELL when insufficient confirming modules
            if (
                new_regime == config.REGIME_STRONG_SELL
                and _live_nonzero < _min_modules
            ):
                logger.info(
                    f"RE-B2: STRONG_SELL capped at MILD_SELL — "
                    f"only {_live_nonzero}/{_min_modules} "
                    f"confirming modules live"
                )
                new_regime = config.REGIME_MILD_SELL

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

        # LOG-RE-02: log this cycle for walk-forward analysis
        # _live_weights was computed earlier in this method;
        # rebuild it here so the log captures what was actually used.
        try:
            _log_weights = _build_weights()
        except Exception:
            _log_weights = {
                "vol": config.WEIGHT_VOL,
                "edge": config.WEIGHT_EDGE,
                "trend": config.WEIGHT_TREND,
                "flow": config.WEIGHT_FLOW,
            }
        self._log_regime_cycle(now, _log_weights)

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
        # BUG-02 FIX: use near-expiry ATM IV as spot vol proxy instead
        # of India VIX. VIX is a variance-swap integral across all OTM
        # strikes and naturally trades 1-2.5pts higher than ATM IV due
        # to put skew. Comparing far-month ATM IV against VIX creates a
        # structural negative bias (t_spread almost always negative),
        # making term_score permanently -1 or 0.
        # Fix: compare far ATM IV vs near ATM IV (same methodology,
        # different tenors). Fall back to VIX only if iv_atm unavailable.
        v_fwd = self.dm.forward_iv
        _near_iv = self.dm.iv_atm  # near-expiry ATM IV (decimal)
        # RE-01: detect whether forward_iv is a genuine far-expiry
        # ATM IV or the VIX/100 fallback. When it is the fallback,
        # the 'term spread' compares a 30-day variance-swap integral
        # against a 3-6 day ATM number — not a term spread at all.
        # Return None (honest zero) so _persist decays gracefully
        # rather than injecting a structurally biased constant.
        _fwd_is_vix_proxy = (
            v_fwd is not None
            and self.dm.vix is not None
            and abs(v_fwd - self.dm.vix / 100.0) < 0.001
        )
        if v_fwd is not None and not _fwd_is_vix_proxy:
            v_fwd_pct  = v_fwd * 100.0
            if _near_iv is not None and _near_iv > 0:
                v_spot_pct = _near_iv * 100.0
            else:
                v_spot_pct = vix
            t_spread   = v_fwd_pct - v_spot_pct
            # RC3: tenor-specific TERM_THRESHOLD for Path A
            # Genuine 30-45 DTE monthly spread is structurally
            # 1.5-3.0pp.  Using 0.8pp fires almost always, creating
            # a 6-hourly artificial composite step-change when the
            # monthly expiry loads.  Use 2.0pp for monthly tenor.
            _term_threshold_monthly = 2.0
            if t_spread > _term_threshold_monthly:
                term_score = 1    # contango = sell vol
            elif t_spread < -_term_threshold_monthly:
                term_score = -1   # backwardation = buy vol
            else:
                term_score = 0
            term_txt = (
                f"T_spread {t_spread:+.2f}% "
                f"({'CONTANGO' if term_score==1 else 'BACKWARDATION' if term_score==-1 else 'FLAT'})"
            )
        elif _fwd_is_vix_proxy:
            # PATCH R-10: when no 30-45 DTE expiry, use near vs far weekly.
            # Near = active expiry (DTE=4-8), Far = next weekly (DTE=10-16).
            # This is always computable in the NIFTY weekly series.
            _near_iv_val = _near_iv  # already set above
            _far_iv_val = None
            try:
                _today_ts = __import__("datetime").date.today()
                _avail = self.dm.get_available_expiries()
                for _exp_str in sorted(_avail):
                    _exp_d = __import__("datetime").datetime.strptime(
                        _exp_str, "%Y-%m-%d"
                    ).date()
                    _dte_chk = (_exp_d - _today_ts).days
                    if 10 <= _dte_chk <= 16:
                        _far_chain = self.dm.get_chain_for_expiry(_exp_str)
                        if _far_chain and spot:
                            _far_atm = min(
                                _far_chain.keys(),
                                key=lambda k: abs(k - spot)
                            )
                            _fc = _far_chain[_far_atm].get("call", {})
                            _fp = _far_chain[_far_atm].get("put", {})
                            _fc_iv = _fc.get("iv", 0)
                            _fp_iv = _fp.get("iv", 0)
                            if _fc_iv > 0 and _fp_iv > 0:
                                _far_iv_val = (_fc_iv + _fp_iv) / 2.0
                        break
            except Exception:
                _far_iv_val = None
            if _far_iv_val is not None and _near_iv_val is not None and _near_iv_val > 0:
                _near_pct = _near_iv_val * 100.0
                _far_pct  = _far_iv_val * 100.0
                t_spread  = _far_pct - _near_pct
                if t_spread > TERM_THRESHOLD:
                    term_score = 1
                elif t_spread < -TERM_THRESHOLD:
                    term_score = -1
                else:
                    term_score = 0
                term_txt = (
                    f"T_spread(weekly) {t_spread:+.2f}%% "
                    f"near={_near_pct:.1f}%% far={_far_pct:.1f}%%"
                )
            else:
                term_score = None
                term_txt   = "T_spread n/a (forward_iv is VIX proxy, no far weekly)"
                notes.append("forward IV is VIX/100 proxy — not a term spread")
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

        # RE-SK-01: tightened tolerance from 0.15 to 0.05.
        # The old 0.15 tolerance accepted deltas 0.10-0.40 — a
        # 5-10 vol point range that introduced interpolation
        # artifacts into the skew Z-score. 0.05 restricts to
        # 0.20-0.30 delta range for a meaningful skew signal.
        if (
            best_c and best_p
            and abs(best_c[0] - 0.25) < 0.08  # PATCHED: 0.05→0.08
            and abs(best_p[0] + 0.25) < 0.08  # PATCHED: 0.05→0.08
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

        # RE-01: if term_score is None (VIX proxy), use only skew
        if term_score is None:
            vol_score = skew_score  # full weight on skew
        else:
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
        # C4-09: horizon-matched RV window.
        # Comparing 20-day RV against a 6-day option is a structural
        # directional bias: 20-day RV lags and understates near-term
        # risk, making IV look 'rich' more often than justified.
        # Use the active expiry's DTE to match the RV window to the
        # option's tenor. Falls back to get_estimated_rv() as before.
        _dte_for_rv = 20
        if self.dm._active_expiry:
            try:
                from datetime import date as _date
                _exp = datetime.strptime(
                    self.dm._active_expiry, "%Y-%m-%d"
                ).date()
                _dte_for_rv = max(5, min(20, (_exp - _date.today()).days))
            except Exception:
                _dte_for_rv = 20

        rv = self.dm.get_estimated_rv()
        if rv is None:
            return None, "RV unavailable (no daily candles or VIX)"
        # AUDIT RE-N01: track whether we are using actual or
        # estimated (VIX-derived) RV.
        _rv_is_estimated = (
            self.dm.rv_20d is None or self.dm.rv_20d <= 0
        )
        # If actual RV is available, recompute over the matched window
        if not _rv_is_estimated and len(self.dm.candles_daily) >= _dte_for_rv + 1:
            import math as _math
            import numpy as _np
            try:
                _closes = [
                    c["close"] for c in list(self.dm.candles_daily)
                    if c.get("close", 0) > 0
                ]
                if len(_closes) >= _dte_for_rv + 1:
                    _rets = [
                        _math.log(_closes[i] / _closes[i - 1])
                        for i in range(
                            len(_closes) - _dte_for_rv,
                            len(_closes),
                        )
                    ]
                    rv = float(_np.std(_rets) * _math.sqrt(252))
            except Exception:
                pass  # keep original rv estimate

        # CAL-01: defensive unit normalisation guard.
        # get_estimated_rv() returns decimal (e.g. 0.08 for 8%).
        # But if rv somehow arrives as percentage (e.g. 8.0 for 8%),
        # multiplying by 100 gives 800% -> edge always -1 (buy vol).
        # Guard: values > 5.0 are already percentage-scale.
        # Normal NIFTY RV is 8-25% (decimal 0.08-0.25, pct 8-25).
        # A decimal > 5.0 would mean 500%+ RV — impossible in practice.
        if rv > 5.0:
            rv_pct = rv          # already in percentage
        else:
            rv_pct = rv * 100.0  # convert decimal to percentage

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

        # PATCH R-06: use relative VRP (IV/RV ratio) as primary signal.
        # Absolute spread (EDGE_RICH=2.0pp) only fires at VIX>14.
        # Relative VRP fires at VIX=11 (22%>15%) through VIX=22.
        _vrp_relative = (iv_atm - rv_pct) / rv_pct if rv_pct > 0 else 0.0
        _vrp_rich_threshold = 0.35   # P3-1: VRP rich threshold raised (was 0.15; NIFTY structural VRP 27-50%% made edge=+1 always)
        _vrp_cheap_threshold = -0.05  # IV below RV by 5%
        if _vrp_relative >= _vrp_rich_threshold or edge > EDGE_RICH:
            raw = 1
            tag = f"RICH (VRP_rel={_vrp_relative:.2%} seller edge)"
        elif _vrp_relative <= _vrp_cheap_threshold or edge < EDGE_CHEAP:
            raw = -1
            tag = f"CHEAP (VRP_rel={_vrp_relative:.2%} buyer edge)"
        else:
            raw = 0
            tag = f"FAIR (VRP_rel={_vrp_relative:.2%})"

        rv_src = "actual" if self.dm.rv_20d else "est(VIX×0.70)"
        # AUDIT RE-N01: cap at 0 when RV is estimated (circular signal)
        if _rv_is_estimated and raw != 0:
            logger.info(
                f"Edge: estimated RV in use — capping score 0 "
                f"(was {raw})"
            )
            raw = 0
            tag = "ESTIMATED_RV (neutral)"
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

        # RE-EMA-01: filter to intraday bars only for EMA slope.
        # The 21-bar lookback spans ~1.7 trading days. Overnight
        # gaps (12h of global moves) contaminate the slope, causing
        # trend flips at the open based on gap size not intraday trend.
        # ADX uses all bars (it measures trend strength, not direction)
        # but EMA slope uses only today's session bars.
        try:
            _today_str = datetime.now(self._IST).strftime("%Y-%m-%d")
            _intraday = [
                b for b in bars
                if str(b.get("timestamp", "")).startswith(_today_str)
            ]
            # Fall back to all bars if today has fewer than 5
            # (e.g. early morning before enough bars accumulate)
            _slope_bars = _intraday if len(_intraday) >= 5 else bars
        except Exception:
            _slope_bars = bars

        closes = [b.get("close", b.get("c", 0)) for b in bars]
        _slope_closes = [
            b.get("close", b.get("c", 0)) for b in _slope_bars
        ]
        ax = adx14([
            {"h": b["high"], "l": b["low"], "c": b["close"]}
            for b in bars
        ])
        # BUG-01 FIX: compute EMA on ALL historical bars so span=50
        # always has sufficient data. The old code used intraday bars
        # for both the EMA computation AND the slope, which caused
        # ema_series(N_bars, span=N_bars) to return a 1-element list
        # (seed consumes all values, leaving nothing to recurse on).
        # Slope is still measured over intraday bars only (overnight
        # gap fix preserved) by indexing the full EMA at intraday
        # positions rather than recomputing from intraday closes.
        ema = ema_series(closes, 50)

        if ax is None or len(ema) < 2:
            return None, "indicator warmup"

        adx_v, pdi, ndi = ax
        # CAT4-B: _slope_bars wired into slope calculation
        # Previously _slope_bars was assigned but never used.
        # Use intraday-only bars for slope to avoid overnight
        # gap contamination. Fall back to full EMA if insufficient
        # intraday bars (< EMA_PERIOD + 5).
        _min_intraday = config.EMA_PERIOD + 5
        if len(_slope_bars) >= _min_intraday:
            _slope_closes = [
                b.get("close", b.get("c", 0))
                for b in _slope_bars
            ]
            _ema_intraday = ema_series(
                _slope_closes, config.EMA_PERIOD
            )
            if len(_ema_intraday) >= 2:
                _lookback_intra = min(
                    21, len(_ema_intraday)
                )
                slope = (
                    _ema_intraday[-1]
                    - _ema_intraday[-_lookback_intra]
                )
            else:
                _lookback = min(21, len(ema))
                slope = ema[-1] - ema[-_lookback]
        else:
            _lookback = min(21, len(ema))
            slope = ema[-1] - ema[-_lookback]
        slope_pct = slope / spot * 100.0 if spot else 0.0
        above     = spot > ema[-1]

        # RE-R02: require three-way directional agreement to
        # avoid false signals at EMA crossings:
        #   bullish: spot > EMA AND slope > 0 AND +DI > -DI
        #   bearish: spot < EMA AND slope < 0 AND -DI > +DI
        # Without this, a falling EMA with spot marginally above
        # it scores +1 (bullish) despite a bearish trend.
        # RE-P1-01: restore bipolar trend score.
        # RE-02 made trend always -1 or 0, making STRONG_SELL_VOL
        # unreachable (max composite = 0.75 without trend's 0.25).
        # Correct semantics for a premium-selling engine:
        #   +1 = range-bound (low ADX, flat EMA) = favorable for selling
        #    0 = neutral / mixed signals
        #   -1 = strong confirmed trend = unfavorable (gamma risk)
        # This preserves the RE-02 intent (trend reduces short-vol
        # conviction) while allowing the composite to reach STRONG_SELL.
        if adx_v > ADX_TREND and abs(slope_pct) > EMA_SLOPE_PCT:
            _slope_up = slope > 0
            _di_bull  = pdi > ndi
            if above and _slope_up and _di_bull:
                # C4-08: asymmetric penalty. Bullish trend is less
                # dangerous to short-vol than bearish (IV compresses
                # on up moves, expands on down moves). Partial penalty.
                raw  = -0.4
                dirn = "bullish trend (partial -0.4 short-vol penalty)"
            elif not above and not _slope_up and not _di_bull:
                # Full penalty: bearish trend expands IV, kills short-gamma
                raw  = -1.0
                dirn = "bearish trend (full -1.0 short-vol penalty)"
            else:
                raw  = 0
                dirn = "mixed signals (no 3-way agreement)"
        elif adx_v < ADX_RANGE_THRESHOLD and abs(slope_pct) < EMA_SLOPE_PCT * 0.5:
            # RE-B1: range-bound requires positive evidence, not just
            # absence of trend. ADX_RANGE_THRESHOLD=15 already exists
            # in config but was never wired here. Require BOTH low ADX
            # AND flat slope to score +1 (genuinely range-bound).
            raw  = 1
            dirn = "range-bound (confirmed: low ADX + flat slope)"
        else:
            # Indeterminate: ADX between range and trend thresholds,
            # or slope inconsistent with ADX. Honest answer is 0.
            raw  = 0
            dirn = "indeterminate (between range and trend thresholds)"

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
        # RE-OI-01: skip strikes with DTE < 3 to avoid expiry
        # rollover contamination. On weekly expiry day, OI drops
        # mechanically as positions roll, creating false flow signals.
        # DTE is computed from near_expiry which is already set above.
        _flow_dte = 999
        if near_expiry:
            try:
                from datetime import date as _date
                _exp_d = datetime.strptime(
                    near_expiry, "%Y-%m-%d"
                ).date()
                _flow_dte = (_exp_d - _date.today()).days
            except Exception:
                _flow_dte = 999
        if _flow_dte < 3:
            logger.info(
                f"Flow: skipping OI snapshot (DTE={_flow_dte} < 3, "
                f"expiry rollover contamination risk)"
            )
            return None, "flow skipped: DTE < 3 (expiry rollover)"

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
        # BUG-04 FIX: restored to min=600s/target=900s.
        # NSE OI data via Upstox REST API updates at ~3-5 min intervals.
        # The CAL-02 patch lowered to 120/180s, but this often captured
        # two readings from the same OI snapshot (ΔOI=0, flow=None).
        # 10/15 min window aligns with actual API update cadence and
        # provides a meaningful OI delta for institutional flow detection.
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
            # CAL-03: use median instead of mean for SPR baseline.
            # A single extreme spread print pulls the arithmetic mean,
            # biasing the ratio. Median is robust to outliers.
            if span_min >= 20:
                spr_avg = statistics.median(v for _, v in hist)

        if net_flow is None or spr_avg is None:
            why = []
            if net_flow is None:
                why.append("net-flow warming (needs 10-15 min old snapshot)")
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

        # BUG-03 FIX: widened from ±1.5% to ±10%.
        # For a ₹4-8 OTM put, tick size ₹0.05 = 0.8-1.5% of mid.
        # The old ±1.5% threshold was smaller than a single tick
        # move, causing false WIDENING/CONTRACTING on every tick
        # change in best bid/ask. ±10% requires a meaningful spread
        # change (e.g., bid halves from ₹1 to ₹0.50) to signal.
        # S5-1: spread ratio band widened to +-25%%
        # Old +-10%% fired on single-tick noise (one tick = 6-7%% of mid).
        # +-25%% requires a meaningful spread change before signalling.
        if spr < spr_avg * 0.75:
            spr_state = "CONTRACTING"
        elif spr > spr_avg * 1.25:
            spr_state = "WIDENING"
        else:
            spr_state = "FLAT"

        # PRF-R01: smooth net_flow with EMA to reduce snapshot noise.
        # Single OI snapshots are noisy; EMA gives more stable signal.
        if not hasattr(self, "_flow_ema") or self._flow_ema is None:
            self._flow_ema = float(net_flow)
        else:
            self._flow_ema = (
                0.67 * self._flow_ema + 0.33 * float(net_flow)
            )
        _net_flow_smoothed = self._flow_ema

        # RH1: flow signal direction corrected for premium selling
        # Original logic was designed for a directional engine:
        #   bullish flow + tight spreads = +1 (buy signal)
        #   panic flow + wide spreads = -1 (sell signal)
        # For a premium-selling engine the correct interpretation is:
        #   wide spreads + institutional put-buying = fear premium
        #   = elevated IV = BEST time to sell vol → +1
        #   tight spreads + bullish flow = complacency
        #   = compressed IV = WORST time to sell vol → -1
        if _net_flow_smoothed < 0 and spr_state == "WIDENING":
            raw = 1
            tag = "fear premium: wide spreads + put-buying (sell vol)"
        elif _net_flow_smoothed > 0 and spr_state == "CONTRACTING":
            raw = -1
            tag = "complacency: tight spreads + bullish flow (avoid selling)"
        else:
            raw = 0
            tag = "mixed"

        detail = (
            f"Net_dOI(15m) {net_flow:+,.0f} | "
            f"SPR {spr:.4f} vs avg {spr_avg:.4f} "
            f"-> {spr_state} | {tag}"
        )
        # S5-2: flow warmup discontinuity suppressed
        # When flow transitions from None (warmup) to a live value,
        # return None for one additional cycle so the persistence
        # filter absorbs the transition rather than immediately
        # confirming the new score and causing a composite step-change.
        _prev_none = getattr(self, '_flow_prev_raw_was_none', True)
        self._flow_prev_raw_was_none = False  # current cycle has live data
        if _prev_none:
            logger.info(
                "S5-2: flow transitioning from None — "
                "suppressing for 1 cycle to avoid composite step-change"
            )
            detail += " | warmup-transition suppressed"
            return None, detail
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
            # RE-T03/T04: wall-clock decay using last_valid_at.
            # Cycle-count decay was imprecise (API delays could
            # make 10 cycles take 20+ min). Use actual elapsed
            # exchange time. last_valid_at is persisted in SQLite
            # (see _save_state/_load_state) so restarts don't
            # reset the grace period.
            _lva_key = "_last_valid_at_" + name
            _last_valid = getattr(self, _lva_key, None)
            if _last_valid is not None:
                try:
                    _elapsed = (
                        datetime.now(self._IST)
                        - _last_valid
                    ).total_seconds()
                    # S6-2: symmetric decay — shorter grace for negative scores
                    # Positive confirmed scores (sell-vol bias) get 10-min grace
                    # to avoid whipsaws on brief data gaps.
                    # Negative confirmed scores (buy-vol bias) get 2-min grace
                    # so bearish bias does not persist when data resumes.
                    _grace_period = (
                        120 if self._conf[name] < 0 else 600
                    )
                    if _elapsed > _grace_period and self._conf[name] != 0:
                        _old = self._conf[name]
                        # Decay by 10% of current value per
                        # additional 5-minute interval
                        _intervals = int(
                            (_elapsed - 600) / 300
                        ) + 1
                        _decay = _old * (0.90 ** _intervals)
                        if abs(_decay) < 0.05:
                            _decay = 0.0
                        self._conf[name] = _decay
                        logger.info(
                            f"RE-T03: {name} score decayed "
                            f"{_old:.3f} -> {_decay:.3f} "
                            f"(elapsed={_elapsed:.0f}s)"
                        )
                except Exception:
                    pass
            return self._conf[name]

        # RE-T02: keep scores as floats. Rounding ±0.5 to ±1
        # (even symmetrically) overstates conviction when only
        # one of two sub-signals fired. Confirm on sign-stability
        # across 3 consecutive readings instead of exact-integer
        # equality, which is meaningless for floats anyway.
        _raw_f = float(raw)
        buf    = self._buf[name]
        # RE-T03/T04: record when this module last had real data
        setattr(
            self,
            "_last_valid_at_" + name,
            datetime.now(self._IST),
        )
        buf.append(_raw_f)
        if len(buf) > 3:
            buf.pop(0)

        # IMM-05: adaptive persistence.
        # Use fewer readings when the composite signal is strong.
        # Strong signals (high conviction) should not be delayed.
        import math as _math_p
        _adaptive = getattr(
            config, "ADAPTIVE_PERSISTENCE_ENABLED", False
        )
        _fast_thresh = getattr(
            config, "ADAPTIVE_PERSISTENCE_FAST_THRESHOLD", 0.60
        )
        _fast_n = getattr(
            config, "ADAPTIVE_PERSISTENCE_FAST_READINGS", 2
        )
        _slow_n = getattr(
            config, "ADAPTIVE_PERSISTENCE_SLOW_READINGS", 3
        )
        # Determine required readings based on composite magnitude
        _composite_mag = abs(self.raw_composite)
        if _adaptive and _composite_mag >= _fast_thresh:
            _required = _fast_n
        else:
            _required = _slow_n

        # S6-1: sign-consistency check before confirmation
        # Original average-sign logic confirmed [+1,-1,+1] as +0.33
        # (oscillating signal treated as weak positive).
        # New logic: ALL readings in the buffer must have the same sign
        # before confirming.  Mixed-sign buffers hold the previous
        # confirmed value, preventing oscillating signals from
        # contaminating the composite.
        # Exception: all-zero buffer confirms 0 (genuinely neutral).
        if len(buf) >= _required:
            _lastN = buf[-_required:]
            _avg = sum(_lastN) / len(_lastN)
            _all_positive = all(v > 0 for v in _lastN)
            _all_negative = all(v < 0 for v in _lastN)
            _all_zero     = all(abs(v) < 0.05 for v in _lastN)
            _signs_consistent = (
                _all_positive or _all_negative or _all_zero
            )
            if _signs_consistent and abs(_avg) >= 0.10:
                self._conf[name] = _avg
                logger.info(
                    f"Persistence confirmed (consistent sign): "
                    f"{name}={_avg:.3f} "
                    f"(all {'positive' if _all_positive else 'negative'} "
                    f"over {_required} readings, "
                    f"composite={_composite_mag:.3f})"
                )
            elif _all_zero:
                self._conf[name] = 0.0
                logger.info(
                    f"Persistence confirmed (neutral): "
                    f"{name}=0.0 (all-zero buffer)"
                )
            else:
                logger.info(
                    f"Persistence unconfirmed: {name} "
                    f"avg={_avg:.3f} signs_consistent={_signs_consistent} "
                    f"holding={self._conf[name]:.3f}"
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
                # RE-03: anchor to market OPEN (09:15) on the event
                # date. The old code anchored to 09:15 and used
                # EVENT_PRE_HOURS=6, making the pre-window 03:15-09:15
                # IST — the market is closed for all of it. The engine
                # never de-risked BEFORE a Budget/RBI print.
                # New: pre-window = EVENT_PRE_HOURS before market open
                # on the event date, so it covers the trading session.
                event_market_open = self._IST.localize(
                    datetime.strptime(
                        event_date_str, "%Y-%m-%d"
                    ).replace(
                        hour=9, minute=15,
                        second=0, microsecond=0,
                    )
                )
                # PATCH R-09: pre-window starts at T-1 market close (15:30).
                # Old: event_open - 6h = 03:15 IST (market closed, never fires).
                # New: previous trading day 15:30 IST so engine de-risks
                # during the last session BEFORE the event.
                # S12-2: macro pre-window skips non-trading days
                # Walk backwards from event_date - 1 until a trading
                # day is found.  Monday events previously had their
                # pre-window on Sunday (market closed).
                _event_dt_naive = __import__("datetime").datetime.strptime(
                    event_date_str, "%Y-%m-%d"
                )
                _prev_td = _event_dt_naive.date() - __import__("datetime").timedelta(days=1)
                _max_lookback = 7
                for _lb in range(_max_lookback):
                    _td_str = _prev_td.strftime("%Y-%m-%d")
                    _is_trading = (
                        _prev_td.weekday() < 5
                        and _td_str not in config.NSE_MARKET_HOLIDAYS
                    )
                    if _is_trading:
                        break
                    _prev_td -= __import__("datetime").timedelta(days=1)
                _prev_close = self._IST.localize(
                    __import__("datetime").datetime(
                        _prev_td.year, _prev_td.month, _prev_td.day,
                        15, 30, 0
                    )
                )
                pre_window_start = _prev_close
                post_window_end = (
                    event_market_open
                    + __import__("datetime").timedelta(
                        hours=EVENT_POST_HOURS
                    )
                )
                diff_h = (
                    (now - event_market_open).total_seconds() / 3600.0
                )
                # Skip events > 7 days past
                if diff_h > 7 * 24:
                    continue
                if pre_window_start <= now <= post_window_end:
                    return True, event_name
            except Exception:
                continue
        return False, ""

    # ─────────────────────────────────────────────────────────────────
    # Step 8: Regime mapping
    # ─────────────────────────────────────────────────────────────────

    def _map_regime(self, composite: float) -> str:
        """Regime mapping with explicit config-driven hysteresis.
        RE-T01: thresholds are read from config.STRONG_SELL_ENTER
        etc. so operators can tune them directly. The previous
        inline `threshold ± 0.05` created hidden effective
        thresholds that contradicted the documented base values.
        """
        current = self.confirmed_regime

        # Persistence: stay in current regime until the EXIT
        # threshold is crossed in the opposite direction.
        if current == config.REGIME_STRONG_SELL:
            if composite > getattr(
                config, "STRONG_SELL_EXIT",
                config.STRONG_SELL_THRESHOLD - 0.05
            ):
                return config.REGIME_STRONG_SELL
        elif current == config.REGIME_MILD_SELL:
            _ms_exit = getattr(
                config, "MILD_SELL_EXIT",
                config.MILD_SELL_THRESHOLD - 0.05
            )
            _ss_exit = getattr(
                config, "STRONG_SELL_EXIT",
                config.STRONG_SELL_THRESHOLD - 0.05
            )
            if _ms_exit <= composite <= (
                getattr(
                    config, "STRONG_SELL_ENTER",
                    config.STRONG_SELL_THRESHOLD
                )
            ):
                return config.REGIME_MILD_SELL
        elif current == config.REGIME_NEUTRAL:
            _nb_exit = getattr(
                config, "MILD_BUY_EXIT",
                config.MILD_BUY_THRESHOLD - 0.05
            )
            _ns_enter = getattr(
                config, "MILD_SELL_ENTER",
                config.MILD_SELL_THRESHOLD
            )
            if _nb_exit < composite < _ns_enter:
                return config.REGIME_NEUTRAL
        elif current == config.REGIME_BUY_VOL:
            _bv_exit = getattr(
                config, "STRONG_BUY_EXIT",
                config.STRONG_BUY_THRESHOLD - 0.05
            )
            _bv_top = getattr(
                config, "MILD_BUY_ENTER",
                config.MILD_BUY_THRESHOLD
            )
            if _bv_exit <= composite <= _bv_top:
                return config.REGIME_BUY_VOL
        elif current == config.REGIME_STRONG_BUY:
            if composite < getattr(
                config, "STRONG_BUY_EXIT",
                config.STRONG_BUY_THRESHOLD - 0.05
            ):
                return config.REGIME_STRONG_BUY

        # Entry: use ENTER thresholds (fall back to base if not set)
        _ss_enter = getattr(
            config, "STRONG_SELL_ENTER",
            config.STRONG_SELL_THRESHOLD
        )
        _ms_enter = getattr(
            config, "MILD_SELL_ENTER",
            config.MILD_SELL_THRESHOLD
        )
        _mb_enter = getattr(
            config, "MILD_BUY_ENTER",
            config.MILD_BUY_THRESHOLD
        )
        _sb_enter = getattr(
            config, "STRONG_BUY_ENTER",
            config.STRONG_BUY_THRESHOLD
        )
        if composite > _ss_enter:
            return config.REGIME_STRONG_SELL
        if composite >= _ms_enter:
            return config.REGIME_MILD_SELL
        if composite > _mb_enter:
            return config.REGIME_NEUTRAL
        if composite >= _sb_enter:
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
            # LOG-RE-03: decision logging table for walk-forward analysis
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS regime_cycle_log (
                    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp                TEXT NOT NULL,
                    spot                     REAL,
                    vix                      REAL,
                    iv_atm                   REAL,
                    rv_20d                   REAL,
                    adx                      REAL,
                    ema_slope_pct            REAL,
                    raw_vol                  REAL,
                    raw_edge                 REAL,
                    raw_trend                REAL,
                    raw_flow                 REAL,
                    conf_vol                 REAL,
                    conf_edge                REAL,
                    conf_trend               REAL,
                    conf_flow                REAL,
                    weight_vol               REAL,
                    weight_edge              REAL,
                    weight_trend             REAL,
                    weight_flow              REAL,
                    composite_score          REAL,
                    confirmed_regime         TEXT,
                    regime_changed           INTEGER,
                    persistence_count        INTEGER,
                    entry_gate_passed        INTEGER,
                    entry_gate_blocked_reason TEXT,
                    active_expiry            TEXT,
                    active_expiry_dte        INTEGER
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_regime_cycle_ts
                ON regime_cycle_log(timestamp)
            """)
            now_str = datetime.now(self._IST).isoformat()
            _today_save = datetime.now(self._IST).date().isoformat()
            for key, value in [
                ("skew_history",    self._skew_history),
                ("flow_snapshots",  self._flow_snapshots[-20:]),
                ("buffers",         self._buf),
                ("confirmed",       self._conf),
                ("last_save_date",  _today_save),
                ("confirmed_regime", self.confirmed_regime),
                ("previous_regime",  self.previous_regime),
                ("raw_composite",    self.raw_composite),
                ("last_valid_at", {
                    m: getattr(
                        self,
                        "_last_valid_at_" + m,
                        None,
                    ).isoformat()
                    if getattr(
                        self,
                        "_last_valid_at_" + m,
                        None,
                    ) is not None else None
                    for m in MODULES
                }),
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

    def _log_regime_cycle(
        self,
        now: datetime,
        weights: dict,
        entry_gate_passed: bool = False,
        blocked_reason: str = "",
    ) -> None:
        """LOG-RE-01: log every regime refresh cycle for walk-forward analysis.

        Called at the end of _refresh_locked() so every cycle
        (including warmup and macro-override cycles) is recorded.
        The resulting regime_cycle_log table is consumed by
        decision_journal.py to build the daily LLM prompt.
        """
        try:
            # Compute active expiry DTE
            _dte = None
            _active = self.dm._active_expiry
            if _active:
                try:
                    from datetime import date as _date
                    _exp = datetime.strptime(
                        _active, "%Y-%m-%d"
                    ).date()
                    _dte = (_exp - _date.today()).days
                except Exception:
                    pass

            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO regime_cycle_log (
                    timestamp, spot, vix, iv_atm, rv_20d,
                    adx, ema_slope_pct,
                    raw_vol, raw_edge, raw_trend, raw_flow,
                    conf_vol, conf_edge, conf_trend, conf_flow,
                    weight_vol, weight_edge, weight_trend, weight_flow,
                    composite_score, confirmed_regime,
                    regime_changed, persistence_count,
                    entry_gate_passed, entry_gate_blocked_reason,
                    active_expiry, active_expiry_dte
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, (
                now.isoformat(),
                self.dm.spot,
                self.dm.vix,
                self.dm.iv_atm,
                self.dm.rv_20d,
                self.dm.adx,
                self.dm.ema_slope,
                self._raw.get("vol"),
                self._raw.get("edge"),
                self._raw.get("trend"),
                self._raw.get("flow"),
                float(self._conf.get("vol", 0)),
                float(self._conf.get("edge", 0)),
                float(self._conf.get("trend", 0)),
                float(self._conf.get("flow", 0)),
                weights.get("vol"),
                weights.get("edge"),
                weights.get("trend"),
                weights.get("flow"),
                self.raw_composite,
                self.confirmed_regime,
                1 if self.regime_changed else 0,
                self.persistence_count,
                1 if entry_gate_passed else 0,
                blocked_reason or "",
                _active,
                _dte,
            ))
            # Keep last 30 days only to avoid unbounded growth
            conn.execute("""
                DELETE FROM regime_cycle_log
                WHERE timestamp < datetime('now', '-30 days')
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"_log_regime_cycle: {e}")

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
                            m: float(data.get(m, 0))
                            for m in MODULES
                        }
                    elif key == "confirmed_regime":
                        # RE8-P1-01 FIX: only restore regime when
                        # saved today. If _conf was cleared (stale
                        # date), restoring the regime creates a
                        # contradiction: STRONG_SELL with zero evidence.
                        # Regime is restored after the date check below.
                        _restored_regime = data if isinstance(data, str) else ""
                    elif key == "previous_regime":
                        _restored_prev_regime = data if isinstance(data, str) else ""
                    elif key == "raw_composite":
                        try:
                            _restored_composite = float(data)
                        except (TypeError, ValueError):
                            _restored_composite = 0.0
                    elif key == "last_valid_at":
                        for m in MODULES:
                            ts_str = data.get(m)
                            if ts_str:
                                try:
                                    _ts = datetime.fromisoformat(
                                        ts_str
                                    )
                                    if _ts.tzinfo is None:
                                        _ts = self._IST.localize(_ts)
                                    setattr(
                                        self,
                                        "_last_valid_at_" + m,
                                        _ts,
                                    )
                                except Exception:
                                    pass
                except Exception:
                    pass
            # AUDIT RE-05: clear confirmed scores on a new trading day
            # so stale yesterday values don't bias today's composite.
            today_iso = datetime.now(self._IST).date().isoformat()
            last_save_str = ""
            try:
                for _k, _v in rows:
                    if _k == "last_save_date":
                        last_save_str = json.loads(_v)
                        break
            except Exception:
                pass
            if last_save_str and last_save_str != today_iso:
                logger.info(
                    "RE-05: new trading day — clearing stale "
                    "confirmed module scores"
                )
                self._conf = {m: 0 for m in MODULES}
                self._buf  = {m: [] for m in MODULES}
            # RE7-P1-02: read last_save_date and clear stale
            # module scores if saved on a different day.
            _last_save = ""
            for _k, _v in rows:
                if _k == "last_save_date":
                    try:
                        _last_save = json.loads(_v)
                    except Exception:
                        pass
                    break
            _today_iso = datetime.now(self._IST).date().isoformat()
            # RE10-P1-01 FIX: initialise deferred restore vars BEFORE
            # the date check. The RE8-P1-01 fix had a scoping bug:
            # getattr(self, '_restored_regime', '') was called AFTER
            # the loop, overwriting the value just read from SQLite
            # with an attribute that is never set on self. Result:
            # confirmed_regime was never restored on same-day restart.
            # These variables are populated by the loop above.
            _restored_regime      = locals().get("_restored_regime", "")
            _restored_prev_regime = locals().get("_restored_prev_regime", "")
            _restored_composite   = locals().get("_restored_composite", 0.0)
            if _last_save and _last_save != _today_iso:
                logger.info(
                    f"RE7-P1-02: last save was {_last_save}, "
                    f"today is {_today_iso} — clearing stale "
                    f"module scores to prevent spurious transitions"
                )
                self._conf = {m: 0.0 for m in MODULES}
                self._buf  = {m: []  for m in MODULES}
                # RE8-P1-01: do NOT restore regime when evidence cleared.
                # Starting NEUTRAL is safe; starting STRONG_SELL with
                # zero evidence is dangerous.
                logger.info(
                    "RE8-P1-01: regime reset to NEUTRAL (stale save date)"
                )
            else:
                # Same day: safe to restore regime alongside evidence
                if _restored_regime:
                    self.confirmed_regime = _restored_regime
                if _restored_prev_regime:
                    self.previous_regime = _restored_prev_regime
                if _restored_composite:
                    self.raw_composite = _restored_composite
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
