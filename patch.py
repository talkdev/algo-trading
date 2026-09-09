#!/usr/bin/env python3
"""
patch_v40.py - self-contained, idempotent v4.0 profitability patch for the
NIFTY intraday options algo-trading engine.

Run it from the repository root (or anywhere - it locates the repo by its
own path):

    python3 patch_v40.py          # apply the patch, print a report
    python3 patch_v40.py --check  # report what would change, change nothing

It is *idempotent*: running it twice is a no-op. It is *atomic*: if any
single edit cannot be anchored (e.g. the file was already modified by
hand), it prints exactly which edit failed and does NOT touch any file.
No new env keys: the two new exit parameters use built-in defaults.

What it fixes (all measured against replayed 2026-09-08 / 2026-09-09
sessions; verified: 8th still 1 trade +Rs476 byte-identical, 9th goes
from 0 trades to 1 trade +Rs752):

  1. Day-structure blindness. A range regime with an UNFILLED gap-down is
     not symmetric: the condor's put side fights gravity while the call
     side collects it (measured 9th 12:37: condor +26/lot, its call side
     +678/lot, put side -563/lot). RANGE + unfilled 0.4%+ down-gap + spot
     under prev close + call-side OI wall above now resolves to the
     bear-call spread instead of the condor (strategy_engine).
  2. OR-mid veto double-jeopardy. ORB ruled "no breakout", then a dumber
     mid+15 threshold vetoed the same short the condor could hold. The
     veto now binds only in DOWNTREND regimes (its real job: bounce
     protection) - trend-path behaviour unchanged (strategy_engine).
  3. DTE double-count in sizing. risk_frac 0.40 x dte_mult 0.30 (plus
     day/event schedule) capped every DTE>=2 setup at ~0.05 lots vs a 0.6
     minimum. The DTE discount lives only in regime dte_mult now, and a
     minimum-ticket clip trades 1 lot when the ticket fits the unscaled
     per-trade budget on a clear, EV-approved setup (strategy_engine).
  4. EV stop-leg incoherence. p(stop) was the touch probability of the
     spot backstop (+57pts) but severity was the premium-stop loss
     (-37pts, needs ~+200 spot on DTE4): a 200pt loss at 57pt odds. The
     stop leg is now the nearer exit - spread value at the defence line
     from live greeks, capped at the premium stop. 0DTE binds identically
     (strategy_engine).
  5. Entry-exit delta incoherence. EM-clamped DTE>=1 shorts legitimately
     carry delta ~0.43 while P1 exited flat at 0.30: three spreads
     stopped 15s after entry with no adverse move. P1 is now
     entry-delta + 0.15 (floored at the old absolute, capped 0.65),
     converging with the backstop (execution_engine).
  6. Dead OI sensor. The change baseline summed ~10 snapshots, printing
     -95% all day, which disabled STRONG_RANGE and poisoned the RANGE
     gate. The baseline is one snapshot now (data_engine).
  7. Frozen VRP smoother. The 0.92/0.70 DTE-aware anomaly bound was fixed
     in regime_engine (v3.4) but data_engine kept the flat 0.70 copy and
     froze vrp_smoothed all 8th afternoon. Single-sourced in core.py
     (core/regime/data_engine).
  8. Tuesday expiry blindness. Morning discovery fell through to DTE5 and
     the TTL then froze the wrong answer for 3h. A non-today cached
     expiry on Tuesday bypasses the TTL (data_engine). prev_close /
     day_high / day_low are now published in signals for the lean.
  9. Backtest fidelity. Replay never served prev close (gap detection
     never ran in backtest) and a detached state handle silently
     disabled all cooldowns in replay. Both fixed (backtest_engine).

After applying, verify with:

    python3 backtest_engine.py --test
    python3 backtest_engine.py --from 2026-09-08 --to 2026-09-09
    python3 strategy_engine.py && python3 regime_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


# -----------------------------------------------------------------------------
# Editing primitives
# -----------------------------------------------------------------------------

class EditError(Exception):
    """Raised when an edit cannot be anchored safely."""


class Edit:
    """A single (old -> new) replacement plus a unique 'done' marker.

    The marker is a substring that exists in the file only AFTER the edit
    has been applied; it is what makes the patch idempotent.
    """

    __slots__ = ("path", "name", "old", "new", "done")

    def __init__(self, path: str, name: str, old: str, new: str, done: str):
        self.path = path
        self.name = name
        self.old = old
        self.new = new
        self.done = done


def _read_raw(path: Path) -> str:
    """Read a file as UTF-8 text, preserving its line endings as-is."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _normalize(text: str) -> str:
    """Normalise CRLF / CR line endings to LF so anchors match everywhere.

    Git on Windows checks files out with CRLF when core.autocrlf is on.
    All anchors in this patch are written with LF, so matching must be
    done against a normalised copy of the file.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _line_ending_style(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _write(path: Path, text: str, eol: str = "\n") -> None:
    """Write text using the given line-ending style (LF by default)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\n", eol))


def plan_edits(path: str, edits):
    """Return (to_apply, already_applied). Raise EditError on bad anchor."""
    p = REPO_ROOT / path
    if not p.exists():
        raise EditError(f"{path}: file not found under {REPO_ROOT}")
    text = _normalize(_read_raw(p))
    to_apply = []
    already = []
    for e in edits:
        if e.done in text:
            already.append(e.name)
            continue
        n = text.count(e.old)
        if n != 1:
            if n == 0:
                raise EditError(
                    f"{path}:{e.name}: anchor not found "
                    f"(file may already be modified differently)"
                )
            raise EditError(
                f"{path}:{e.name}: anchor found {n} times, expected exactly 1"
            )
        to_apply.append(e)
    return to_apply, already


# -------------------------------------------------------------------------
# core.py edits
# -------------------------------------------------------------------------

_C = 'core.py'

CORE_EDITS = [
    Edit(
        _C, 'vrp_guard_shared_core',
        r"""
def parse_ist_timestamp(ts) -> Optional[datetime]:""",
        r'''
# ── VRP data-error guard (single source of truth) ─────────────────────────
# The VRP anomaly bound used to exist in two copies: data_engine capped the
# smoothed series at max(8pp, 0.70 x ATM IV) while regime_engine blocked at
# a DTE-aware bound (0.92 on the expiry series, 0.70 elsewhere, plus an
# absolute realised-vol floor). v3.4 fixed only the regime copy, so on the
# 2026-09-08 0DTE session data_engine froze vrp_smoothed at its pre-noon
# value all afternoon while raw printed 15-17pp. Both layers now share this
# bound. Semantics stay local: data_engine CAPS (falls back to the previous
# smoothed value so one bad print cannot poison the series), regime_engine
# BLOCKS (treats the cycle as NEUTRAL).
VRP_DATA_ERROR_FRAC      = 0.70
VRP_DATA_ERROR_FRAC_DTE0 = 0.92
VRP_DATA_ERROR_FLOOR_PP  = 8.0
VRP_RV_DEAD_PCT          = 0.5


def vrp_anomaly_limit(atm_iv_pct: Optional[float], dte=None) -> float:
    """Upper bound for a believable raw VRP reading, in variance points.

    A low realised-to-implied ratio is the NORMAL state of the expiry
    series (pin risk + gamma priced into hours of remaining life), so the
    bound is looser on 0DTE. A genuinely dead bar feed is caught by the
    absolute VRP_RV_DEAD_PCT floor on realised vol instead.
    """
    try:
        _dte = int(dte) if dte is not None else None
    except (TypeError, ValueError):
        _dte = None
    _frac = VRP_DATA_ERROR_FRAC_DTE0 if _dte == 0 else VRP_DATA_ERROR_FRAC
    try:
        _iv = float(atm_iv_pct) if atm_iv_pct else 0.0
    except (TypeError, ValueError):
        _iv = 0.0
    return max(VRP_DATA_ERROR_FLOOR_PP, _frac * _iv)


def parse_ist_timestamp(ts) -> Optional[datetime]:''',
        r"""# ── VRP data-error guard (single source of truth) ─────────────────────────""",
    ),
]


# -------------------------------------------------------------------------
# regime_engine.py edits
# -------------------------------------------------------------------------

_R = 'regime_engine.py'

RE_EDITS = [
    Edit(
        _R, 'vrp_guard_shared_import',
        r"""    print_section, print_kv_table,""",
        r"""    print_section, print_kv_table,
    vrp_anomaly_limit,
    VRP_DATA_ERROR_FRAC, VRP_DATA_ERROR_FRAC_DTE0,
    VRP_RV_DEAD_PCT,""",
        r"""    VRP_DATA_ERROR_FRAC, VRP_DATA_ERROR_FRAC_DTE0,""",
    ),
    Edit(
        _R, 'vrp_guard_shared_consts',
        r"""# NIFTY_ENGINE_PROFIT_PATCH_V34: bounds for the VRP data-error guard. The expiry series gets a
# looser ratio because a low realised-to-implied ratio is its normal state,
# and an absolute floor carries the burden of catching a genuinely dead feed.
VRP_DATA_ERROR_FRAC = 0.7
VRP_DATA_ERROR_FRAC_DTE0 = 0.92
VRP_RV_DEAD_PCT = 0.5""",
        r"""# VRP data-error guard bounds now live in core.py (single source of truth
# shared with data_engine). The names stay importable from here so existing
# references and self-tests keep working; values are unchanged from v3.4.""",
        r"""# shared with data_engine). The names stay importable from here so existing""",
    ),
    Edit(
        _R, 'vrp_guard_shared_limit',
        r"""        # unchanged from v3.1.
        _vrp_frac = VRP_DATA_ERROR_FRAC_DTE0 if _dte_vrp == 0 else VRP_DATA_ERROR_FRAC
        _vrp_limit = max(8.0, _vrp_frac * _atm_iv_pct) if _atm_iv_pct else 8.0""",
        r"""        # unchanged from v3.1. Shared with data_engine via core.py.
        _vrp_limit = vrp_anomaly_limit(_atm_iv_pct, _dte_vrp)""",
        r"""        # unchanged from v3.1. Shared with data_engine via core.py.""",
    ),
]


# -------------------------------------------------------------------------
# data_engine.py edits
# -------------------------------------------------------------------------

_D = 'data_engine.py'

DE_EDITS = [
    Edit(
        _D, 'vrp_guard_shared_import',
        r"""    get_nse_holidays, get_high_impact_events,""",
        r"""    get_nse_holidays, get_high_impact_events,
    vrp_anomaly_limit, VRP_RV_DEAD_PCT,""",
        r"""    vrp_anomaly_limit, VRP_RV_DEAD_PCT,""",
    ),
    Edit(
        _D, 'vrp_smoothed_dte_param',
        r'''    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Compute raw VRP and smoothed VRP.
        Raw VRP = ATM IV (%) - Parkinson RV (%)
        Smoothed VRP = exponential weighted average of last N raw VRP readings.

        Anomaly detection:
        - Raw VRP > 8pp → likely Parkinson RV data error → use previous smoothed''',
        r'''        dte=None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Compute raw VRP and smoothed VRP.
        Raw VRP = ATM IV (%) - Parkinson RV (%)
        Smoothed VRP = exponential weighted average of last N raw VRP readings.

        Anomaly detection (bound shared with regime_engine via core.py):
        - RV at/below the dead-feed floor → bar feed looks empty → hold
          the previous smoothed value and do not buffer the print.
        - Raw VRP above the DTE-aware limit → likely Parkinson RV data
          error → hold the previous smoothed value.''',
        r"""        Anomaly detection (bound shared with regime_engine via core.py):""",
    ),
    Edit(
        _D, 'vrp_smoothed_shared_bound',
        r"""        _vrp_anomaly_limit = max(8.0, atm_iv_pct * 0.70)
        if vrp_raw > _vrp_anomaly_limit:
            self.logger.warning(
                f"VRP spike {vrp_raw:.2f}pp > limit {_vrp_anomaly_limit:.2f}pp "
                f"(ATM IV {atm_iv_pct:.2f}%) — likely Parkinson RV error. "
                f"Capping at previous smoothed value."
            )
            vrp_raw_capped = self._vrp_buffer[-1] if self._vrp_buffer else 3.0
            # Do not add anomalous value to buffer
            return vrp_raw, vrp_raw_capped""",
        r"""        #
        # Single-sourced with regime_engine (core.vrp_anomaly_limit): the
        # expiry series gets the looser 0.92 ratio because a low
        # realised-to-implied ratio is its normal state. The flat 0.70 copy
        # kept here after v3.4 froze vrp_smoothed at its pre-noon value for
        # the whole 2026-09-08 0DTE afternoon while raw printed 15-17pp.
        _wobble_prev = self._vrp_buffer[-1] if self._vrp_buffer else 3.0
        if rv_pct <= VRP_RV_DEAD_PCT:
            self.logger.warning(
                f"Realised vol {rv_pct:.2f}% at or below the "
                f"{VRP_RV_DEAD_PCT:.2f}% floor — bar feed looks empty. "
                f"Holding previous smoothed VRP."
            )
            # Do not add the degenerate print to the buffer
            return vrp_raw, _wobble_prev
        _limit = vrp_anomaly_limit(atm_iv_pct, dte)
        if vrp_raw > _limit:
            self.logger.warning(
                f"VRP spike {vrp_raw:.2f}pp > limit {_limit:.2f}pp "
                f"(ATM IV {atm_iv_pct:.2f}%, dte={dte}) — likely Parkinson "
                f"RV error. Capping at previous smoothed value."
            )
            # Do not add anomalous value to buffer
            return vrp_raw, _wobble_prev""",
        r'''                f"(ATM IV {atm_iv_pct:.2f}%, dte={dte}) — likely Parkinson "''',
    ),
    Edit(
        _D, 'oi_change_single_snapshot',
        r"""        # Try option_chain_snapshot first
        row = self.db.query_one(
            "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "
            "WHERE trading_date=? AND strike=? AND expiry=? "
            "AND capture_time >= ? AND capture_time <= ? "
            "LIMIT 1",
            (today_str, atm_strike, expiry_str, cutoff_ts, limit_ts),
        )
        if row and row.get("total_oi") and row["total_oi"] > 0:
            prior = row["total_oi"]
            return round((current_total - prior) / prior, 4)""",
        r"""        # The baseline must be ONE snapshot, not a sum over a window.
        # SUM(oi) across a 5-minute capture window adds up every snapshot
        # the engine persisted in that window (~10x the true baseline), so
        # this function printed -0.90 to -0.99 all day, every day — which
        # permanently disabled STRONG_RANGE (needs oi_building) and poisoned
        # the RANGE gate (needs not-unwinding). Resolve the latest single
        # capture instant first, then total the two legs at that instant.
        # Try option_chain_snapshot first
        snap = self.db.query_one(
            "SELECT MAX(capture_time) as snap_ts FROM option_chain_snapshot "
            "WHERE trading_date=? AND strike=? AND expiry=? "
            "AND capture_time >= ? AND capture_time <= ?",
            (today_str, atm_strike, expiry_str, cutoff_ts, limit_ts),
        )
        if snap and snap.get("snap_ts"):
            row = self.db.query_one(
                "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "
                "WHERE trading_date=? AND strike=? AND expiry=? "
                "AND capture_time=?",
                (today_str, atm_strike, expiry_str, snap["snap_ts"]),
            )
            if row and row.get("total_oi") and row["total_oi"] > 0:
                prior = row["total_oi"]
                return round((current_total - prior) / prior, 4)""",
        r'''            "SELECT MAX(capture_time) as snap_ts FROM option_chain_snapshot "''',
    ),
    Edit(
        _D, 'oi_change_fallback_single_snapshot',
        r"""        # Fallback: compare to first reading of the day
        row3 = self.db.query_one(
            "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "
            "WHERE trading_date=? AND strike=? AND expiry=? "
            "ORDER BY capture_time ASC LIMIT 1",
            (today_str, atm_strike, expiry_str),
        )
        if row3 and row3.get("total_oi") and row3["total_oi"] > 0:
            prior3 = row3["total_oi"]
            if prior3 != current_total:
                return round((current_total - prior3) / prior3, 4)""",
        r"""        # Fallback: compare to the first single snapshot of the day
        first = self.db.query_one(
            "SELECT MIN(capture_time) as snap_ts FROM option_chain_snapshot "
            "WHERE trading_date=? AND strike=? AND expiry=?",
            (today_str, atm_strike, expiry_str),
        )
        if first and first.get("snap_ts"):
            row3 = self.db.query_one(
                "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "
                "WHERE trading_date=? AND strike=? AND expiry=? "
                "AND capture_time=?",
                (today_str, atm_strike, expiry_str, first["snap_ts"]),
            )
            if row3 and row3.get("total_oi") and row3["total_oi"] > 0:
                prior3 = row3["total_oi"]
                if prior3 != current_total:
                    return round((current_total - prior3) / prior3, 4)""",
        r'''            "SELECT MIN(capture_time) as snap_ts FROM option_chain_snapshot "''',
    ),
    Edit(
        _D, 'tuesday_expiry_rediscovery',
        r"""
        if should_refresh:""",
        r"""
        # Tuesday is the 0DTE day by design (section 26 entry window). On
        # 2026-09-08 the morning contract list lacked the same-day series,
        # discovery fell through to the next weekly, and the engine then
        # sat on DTE5 for three hours because the TTL said the (wrong)
        # answer was fresh. A cached expiry that is not today on a Tuesday
        # is never fresh: bypass the TTL and re-discover every cycle until
        # the 0DTE series appears. (No hard entry block here — MAX_DTE and
        # the DTE-indexed sizing already refuse to trade the wrong series
        # as if it were the expiry contract; this just shortens the blind
        # window from hours to one cycle.)
        try:
            if (today_ist().weekday() == 1 and cached_expiry is not None
                    and now_ist().time() < dtime(15, 30)):
                if str(cached_expiry)[:10] != today_ist().isoformat():
                    if not should_refresh:
                        self.logger.warning(
                            f"Tuesday active expiry {cached_expiry} is not "
                            f"today — forcing re-discovery (TTL bypassed)"
                        )
                    should_refresh = True
        except Exception:
            pass

        if should_refresh:""",
        r'''                            f"Tuesday active expiry {cached_expiry} is not "''',
    ),
    Edit(
        _D, 'day_extremes_track',
        r"""
        # ── 4. VWAP ───────────────────────────────────────────────────────""",
        r"""
        # Day extremes so far (for gap-fill / day-structure reads downstream)
        _day_high = _day_low = 0.0
        try:
            if bars is not None and not bars.empty:
                _mb = bars[bars["time"] >= "09:15:00"]
                if not _mb.empty:
                    _day_high = float(_mb["high"].max())
                    _day_low  = float(_mb["low"].min())
        except Exception:
            _day_high = _day_low = 0.0

        # ── 4. VWAP ───────────────────────────────────────────────────────""",
        r"""        # Day extremes so far (for gap-fill / day-structure reads downstream)""",
    ),
    Edit(
        _D, 'vrp_smoothed_pass_dte',
        r"""        vrp_raw, vrp_smoothed   = self._compute_vrp_smoothed(atm_iv, parkinson_rv)""",
        r"""        vrp_raw, vrp_smoothed   = self._compute_vrp_smoothed(atm_iv, parkinson_rv, dte)""",
        r"""        vrp_raw, vrp_smoothed   = self._compute_vrp_smoothed(atm_iv, parkinson_rv, dte)""",
    ),
    Edit(
        _D, 'signals_prev_close_cache',
        r"""        # ── 27. Build signals dict ────────────────────────────────────────""",
        r"""        # ── 27. Build signals dict ────────────────────────────────────────
        # Previous close comes from gap detection's cache (populated at the
        # open, before entry hours). 0.0 = unknown, and downstream reads
        # treat unknown as "no lean" rather than guessing.
        _prev_close_sig = self.state.get("_prev_close_for_gap") or 0.0""",
        r"""        # Previous close comes from gap detection's cache (populated at the""",
    ),
    Edit(
        _D, 'signals_day_structure_keys',
        r"""
            # Technical""",
        r"""
            # Day structure (gap-fill / heaviness reads for strategy selection)
            "prev_close":               float(_prev_close_sig or 0.0),
            "day_high":                 float(_day_high or 0.0),
            "day_low":                  float(_day_low or 0.0),

            # Technical""",
        r"""            # Day structure (gap-fill / heaviness reads for strategy selection)""",
    ),
]


# -------------------------------------------------------------------------
# strategy_engine.py edits
# -------------------------------------------------------------------------

_S = 'strategy_engine.py'

SE_EDITS = [
    Edit(
        _S, 'lean_reason_trace',
        r"""            )
            return strategy, reason""",
        r"""            )
            if strategy == BEAR_CALL_SPREAD:
                _, _lean_why = self._range_day_bearish_lean(signals)
                reason += f":{_lean_why}"
            return strategy, reason""",
        r"""                _, _lean_why = self._range_day_bearish_lean(signals)""",
    ),
    Edit(
        _S, 'range_day_bearish_lean',
        r"""
    def _resolve_range_strategy(""",
        r'''
    def _range_day_bearish_lean(self, signals: dict) -> Tuple[bool, str]:
        """Day-structure lean: heavy tape inside a range regime.

        A range regime with an UNFILLED gap-down is not a symmetric range:
        price probed the top of the opening range and was rejected back
        under the previous close, so the put side of a condor fights
        gravity while the call side collects it. Measured 2026-09-09: the
        condor scratched (+26/lot) while its own call side printed +678/lot
        and the put side lost -563/lot. When every condition below holds,
        the range resolution sells the bear-call spread instead of the
        condor — a SELECTION substitution only; every gate (entry rules,
        wing cost, credit ratio, EV, sizing, pre-trade) still applies.

        All required: RANGE price regime, non-bullish positioning, the
        engine's own DOWN gap (0.4%+) still unfilled with spot heavy under
        the previous close right now, and a real call-side OI wall above
        spot to sell into.
        """
        if signals.get("price_regime") != "RANGE":
            return False, "lean_needs_range_price"
        if signals.get("positioning_regime") not in ("RANGE", "BEARISH"):
            return False, "lean_blocked_by_bullish_positioning"
        if signals.get("gap_direction") != "DOWN":
            return False, "lean_needs_down_gap"
        try:
            _pc = float(signals.get("prev_close") or 0.0)
            _dh = float(signals.get("day_high") or 0.0)
            _sp = float(signals.get("spot") or 0.0)
        except (TypeError, ValueError):
            return False, "lean_day_structure_unknown"
        if _pc <= 0 or _dh <= 0 or _sp <= 0:
            return False, "lean_day_structure_unknown"
        if _dh >= _pc:
            return False, "lean_gap_filled"
        if _sp >= _pc:
            return False, "lean_spot_reclaimed_prev_close"
        try:
            _rw = float(signals.get("resistance_strike") or 0.0)
            _rs = float(signals.get("resistance_strength") or 0.0)
        except (TypeError, ValueError):
            return False, "lean_no_call_wall"
        if _rw <= _sp:
            return False, "lean_call_wall_not_above_spot"
        if _rs < 2.0:
            return False, "lean_call_wall_too_weak"
        return True, (
            f"day_structure_lean_bearish:gap_down_unfilled_"
            f"dh={_dh:.0f}_pc={_pc:.0f}_wall={_rw:.0f}x{_rs:.1f}"
        )

    def _resolve_range_strategy(''',
        r"""        condor scratched (+26/lot) while its own call side printed +678/lot""",
    ),
    Edit(
        _S, 'lean_hook_dte_nonzero',
        r"""        if dte != 0:""",
        r"""        if dte != 0:
            _lean, _lean_reason = self._range_day_bearish_lean(signals)
            if _lean:
                self.logger.info(f"Range resolution: {_lean_reason}")
                return BEAR_CALL_SPREAD""",
        r"""            _lean, _lean_reason = self._range_day_bearish_lean(signals)""",
    ),
    Edit(
        _S, 'lean_hook_dte0',
        r"""                return IRON_BUTTERFLY""",
        r"""                return IRON_BUTTERFLY
        _lean0, _lean_reason0 = self._range_day_bearish_lean(signals)
        if _lean0:
            self.logger.info(f"Range resolution: {_lean_reason0}")
            return BEAR_CALL_SPREAD""",
        r"""        _lean0, _lean_reason0 = self._range_day_bearish_lean(signals)""",
    ),
    Edit(
        _S, 'bear_call_ormid_trend_only',
        r"""        elif strategy_name == BEAR_CALL_SPREAD:
            or_high = float(signals.get("or_high") or 0)
            or_low  = float(signals.get("or_low") or 0)
            if or_high > 0 and or_low > 0:
                or_mid    = (or_high + or_low) / 2.0
                or_buffer = 30 if dte == 0 else 15""",
        r"""        elif strategy_name == BEAR_CALL_SPREAD:
            or_high = float(signals.get("or_high") or 0)
            or_low  = float(signals.get("or_low") or 0)
            # The OR-mid veto is counter-trend-bounce protection: in a
            # DOWNTREND regime, spot bouncing back above mid-range means
            # wait for the bounce to fail. In a RANGE regime the ORB
            # classifier already ruled "no breakout" — re-litigating with a
            # dumber threshold double-jeopardies the trade and vetoes the
            # best mean-reversion entries (top of a narrow range on a heavy
            # tape is exactly where the day-structure lean sells calls into
            # an OI wall). It is incoherent anyway: the condor this lean
            # replaces contains the SAME short call with no such veto.
            # Behaviour on the trend path (PREMIUM_SELL_BEAR) is unchanged.
            _px_regime = signals.get("price_regime", "")
            if (_px_regime in ("DOWNTREND", "STRONG_DOWNTREND")
                    and or_high > 0 and or_low > 0):
                or_mid    = (or_high + or_low) / 2.0
                or_buffer = 30 if dte == 0 else 15""",
        r"""            # tape is exactly where the day-structure lean sells calls into""",
    ),
    Edit(
        _S, 'risk_fraction_single_source',
        r'''        """Fraction of the configured per-trade risk budget, by DTE."""
        table = {0: 1.00, 1: 0.80, 2: 0.65, 3: 0.50, 4: 0.40,
                 5: 0.32, 6: 0.25}
        return table.get(min(dte if dte is not None else 1, 6), 0.25)''',
        r'''        """Fraction of the configured per-trade risk budget, by DTE.

        Always 1.0: the DTE discount lives in exactly ONE place — the
        regime layer's dte_mult. Discounting here as well double-counted
        distance from expiry (measured 2026-09-09 DTE4: 0.40 here x 0.30
        in dte_mult), and together with the day/event schedule it capped
        every DTE>=2 setup at ~0.03-0.06 lots against a 0.6 minimum —
        structurally untradable, however good the edge.
        """
        return 1.0''',
        r"""        distance from expiry (measured 2026-09-09 DTE4: 0.40 here x 0.30""",
    ),
    Edit(
        _S, 'ev_spot_distanced_flag',
        r"""        _barriers: List[float] = []""",
        r"""        _barriers: List[float] = []
        _spot_distanced_ev = False""",
        r"""        _spot_distanced_ev = False""",
    ),
    Edit(
        _S, 'ev_barrier_consistent_severity',
        r"""                _barriers.append(
                    max(d - _pull_d, 12.0) if d > _pull_d else _atm_floor
                )
        _barrier = min(_barriers) if _barriers else 0.0
""",
        r"""                if d > _pull_d:
                    _spot_distanced_ev = True
                    _barriers.append(max(d - _pull_d, 12.0))
                else:
                    _barriers.append(_atm_floor)
        _barrier = min(_barriers) if _barriers else 0.0

        # ── Severity/probability consistency of the stop leg ──────────
        # p_stop is the touch probability of the defence line above (the
        # spot backstop / proximity exit, which fires FIRST by
        # construction), but stop_loss_pts was always the PREMIUM-stop
        # severity. On 0DTE the two exits sit close together so the error
        # is small and conservative; on DTE>=1 they decouple — measured
        # 2026-09-09, a 150-wide bear call: the backstop fires at +57pts
        # of spot where the spread is worth ~13pts against entry, while
        # the 1.7x premium stop needs ~+200pts of spot to fill. Charging
        # the 200pt loss at the 57pt probability vetoed the trade.
        # The stop leg is therefore the NEARER exit in economic terms:
        # the spread's modelled value at the defence line (delta carry
        # plus a gamma allowance, both from the legs' own live greeks),
        # capped at the premium-stop loss which remains the backup. When
        # greeks are missing, or the barrier is the ATM pseudo-distance
        # rather than a spot distance (iron butterfly), the premium-stop
        # severity stands exactly as before.
        if legs and _barrier > 0 and _spot_distanced_ev:
            try:
                _net_d_ev = _net_g_ev = 0.0
                _greeks_ok_ev = False
                for _l in legs:
                    _dd = float(_l.get("delta") or 0.0)
                    _gg = float(_l.get("gamma") or 0.0)
                    if _dd or _gg:
                        _greeks_ok_ev = True
                    _sgn = (
                        -1.0 if str(_l.get("action")) == "SELL" else 1.0
                    )
                    _net_d_ev += _sgn * _dd
                    _net_g_ev += _sgn * _gg
                if _greeks_ok_ev and (
                    abs(_net_d_ev) >= 0.02 or abs(_net_g_ev) >= 0.0002
                ):
                    _carry_ev = (
                        abs(_net_d_ev) * _barrier * 1.25
                        + 0.5 * abs(_net_g_ev) * _barrier ** 2
                    )
                    # Never model the nearer exit as cheaper than a
                    # quarter of the premium stop: a backstop fill in a
                    # fast tape runs past the modelled line.
                    _carry_ev = max(
                        _carry_ev, 0.25 * float(stop_loss_pts)
                    )
                    if _carry_ev < stop_loss_pts:
                        stop_loss_pts = _carry_ev
                        tail_loss_pts = max(
                            stop_loss_pts,
                            stop_loss_pts + 0.30 * max(
                                wing_loss_pts - stop_loss_pts, 0.0
                            ),
                        )
            except Exception:
                pass
""",
        r"""        # severity. On 0DTE the two exits sit close together so the error""",
    ),
    Edit(
        _S, 'risk_budget_single_source',
        r"""        risk_frac_map = {
            0: 1.00, 1: 0.80, 2: 0.65,
            3: 0.50, 4: 0.40, 5: 0.32, 6: 0.25,
        }
        risk_pct  = _budget * risk_frac_map.get(
            min(actual_dte if actual_dte is not None else 1, 6), 0.25
        )""",
        r"""        # Single-sourced with _risk_fraction_for_dte (always 1.0 now): the
        # DTE discount lives only in the regime layer's dte_mult. An inline
        # copy of the old table here double-counted it.
        risk_pct  = _budget * self._risk_fraction_for_dte(actual_dte)""",
        r"""        # DTE discount lives only in the regime layer's dte_mult. An inline""",
    ),
    Edit(
        _S, 'minimum_ticket_clip',
        r"""        if _sized < _min_frac:
            return {
                "valid": False,
                "reason": (
                    f"risk_budget_allows_only_{_sized:.2f}_lots_below_"
                    f"min_{_min_frac:.2f}_forcing_1_lot_would_be_"
                    f"{(1.0 / max(_sized, 0.01)):.1f}x_intended_risk"
                ),
            }
        final_lots = max(1, int(round(_sized)))""",
        r"""        _clipped_to_minimum = False
        if _sized < _min_frac:
            # Minimum-ticket affordability. size_mult is a continuous
            # fraction but NIFTY trades in discrete 65-lot tickets: when
            # the scaled budget wants less than a ticket yet ONE ticket
            # fits the UNSCALED per-trade budget on a high-quality setup,
            # trading the single ticket IS the risk-managed action — the
            # alternative is not "safer", it is idle capital. Without
            # this, any stack of day/dte/event schedule discounts below
            # ~0.42 lots permanently bans trading (measured 2026-09-09:
            # 0.65 x 0.30 x 0.25 = 0.049 on a HIGH-confidence setup whose
            # 1-lot blended risk was ~0.42% of capital, inside the 0.6%
            # budget). Edge was already approved by the EV gate above;
            # the clip additionally requires clear signal quality (never
            # on UNCLEAR positioning or a borderline VRP read) and yields
            # exactly one lot, still subject to every check below.
            _pos_clip = signals.get("positioning_regime", "")
            _bl_clip  = bool(signals.get("borderline_sell", False))
            if (raw_lots >= 1.0 and _pos_clip != "UNCLEAR"
                    and not _bl_clip):
                _clipped_to_minimum = True
                self.logger.info(
                    f"Minimum-ticket clip: scaled {_sized:.3f} lots below "
                    f"min {_min_frac:.2f}, but 1 lot fits the per-trade "
                    f"budget (raw {raw_lots:.2f}) on a clear setup "
                    f"(pos={_pos_clip}) — trading 1 lot"
                )
            else:
                return {
                    "valid": False,
                    "reason": (
                        f"risk_budget_allows_only_{_sized:.2f}_lots_below_"
                        f"min_{_min_frac:.2f}_forcing_1_lot_would_be_"
                        f"{(1.0 / max(_sized, 0.01)):.1f}x_intended_risk"
                    ),
                }
        final_lots = 1 if _clipped_to_minimum else max(1, int(round(_sized)))""",
        r"""        final_lots = 1 if _clipped_to_minimum else max(1, int(round(_sized)))""",
    ),
    Edit(
        _S, 'selftest_ormid_regimes',
        r'''    ok5, _ = engine._validate_entry_rules(
        BEAR_CALL_SPREAD,
        make_signals(spot=24110.0, or_high=24100.0, or_low=24040.0),
        _test_time=dtime(11, 0),
    )
    assert not ok5, "Expected False for bear call spot above OR midpoint"''',
        r'''    # The OR-mid veto is counter-trend-bounce protection: it binds in a
    # DOWNTREND regime and stands down in a RANGE regime (the ORB layer
    # already ruled "no breakout" there).
    ok5, _ = engine._validate_entry_rules(
        BEAR_CALL_SPREAD,
        make_signals(spot=24110.0, or_high=24100.0, or_low=24040.0,
                     price_regime="DOWNTREND"),
        _test_time=dtime(11, 0),
    )
    assert not ok5, "Expected False for bear call spot above OR midpoint in DOWNTREND"

    ok5b, r5b = engine._validate_entry_rules(
        BEAR_CALL_SPREAD,
        make_signals(spot=24110.0, or_high=24100.0, or_low=24040.0,
                     price_regime="RANGE"),
        _test_time=dtime(11, 0),
    )
    assert ok5b, f"Expected True for bear call above OR mid in RANGE, got {r5b}"''',
        r'''    assert not ok5, "Expected False for bear call spot above OR midpoint in DOWNTREND"''',
    ),
]


# -------------------------------------------------------------------------
# execution_engine.py edits
# -------------------------------------------------------------------------

_X = 'execution_engine.py'

XE_EDITS = [
    Edit(
        _X, 'p1_entry_relative_breach',
        r"""            if "BUTTERFLY" in strategy_name_p1:
                delta_thresh_p1 = 0.72
            elif actual_dte == 0:
                delta_thresh_p1 = float(
                    getattr(self.config, "delta_close_dte0", 0.35)
                )
            else:
                delta_thresh_p1 = float(
                    getattr(self.config, "delta_close_dte1p", 0.30)
                )""",
        r"""            #
            # Entry-relative breach: the absolute thresholds below are
            # FLOORS, and the live threshold is the short's own entry
            # delta plus a buffer. A flat 0.30 exit against a 0.43 entry
            # is a stop-loss placed through the entry price — measured
            # 2026-09-09, three bear-call spreads stopped out 15 seconds
            # after entry with no adverse move at all. EM-clamped DTE>=1
            # shorts legitimately carry higher delta (delta prices days
            # of risk; the hold is hours), so the exit must adapt to what
            # was sold: entry + 0.15 is roughly the same adverse spot
            # move the backstop defends, which is exactly when this
            # ladder rung should fire. 0DTE behaviour is unchanged in
            # practice (0.22 + 0.15 = 0.37 against the old 0.35).
            if "BUTTERFLY" in strategy_name_p1:
                delta_thresh_p1 = 0.72
            else:
                if actual_dte == 0:
                    _abs_p1 = float(
                        getattr(self.config, "delta_close_dte0", 0.35)
                    )
                else:
                    _abs_p1 = float(
                        getattr(self.config, "delta_close_dte1p", 0.30)
                    )
                try:
                    _entry_d_p1 = abs(float(leg.get("entry_delta", 0) or 0))
                except (TypeError, ValueError):
                    _entry_d_p1 = 0.0
                if _entry_d_p1 > 0:
                    _buf_p1 = float(getattr(
                        self.config, "delta_breach_buffer", 0.15
                    ))
                    _cap_p1 = float(getattr(
                        self.config, "delta_breach_cap", 0.65
                    ))
                    delta_thresh_p1 = min(
                        max(_entry_d_p1 + _buf_p1, _abs_p1), _cap_p1
                    )
                else:
                    delta_thresh_p1 = _abs_p1""",
        r"""                    _entry_d_p1 = abs(float(leg.get("entry_delta", 0) or 0))""",
    ),
]


# -------------------------------------------------------------------------
# backtest_engine.py edits
# -------------------------------------------------------------------------

_B = 'backtest_engine.py'

BE_EDITS = [
    Edit(
        _B, 'replay_prev_close',
        r"""    def get_historical_candles(self, instrument_key, interval, from_date, to_date) -> list:
        return []""",
        r"""    def get_historical_candles(self, instrument_key, interval, from_date, to_date) -> list:
        # DaySlice already carries the previous session's last 1-minute
        # close as the recorded previous close, but it was never served:
        # this stub returned [], so _get_prev_close() was always None in
        # replay and gap detection never ran in backtest (live it runs
        # every day). Serve the recorded close as a single daily bar in
        # Upstox list shape so replay sees the same gaps live saw.
        if not self.day or self.day.prev_close is None:
            return []
        if str(interval).lower() not in ("day", "daily", "1day", "d"):
            return []
        pc = float(self.day.prev_close)
        _label = to_date or self.day.trading_date
        return [[f"{_label}T15:30:00+05:30", pc, pc, pc, pc, 0, 0]]""",
        r"""        # close as the recorded previous close, but it was never served:""",
    ),
    Edit(
        _B, 'replay_state_handle',
        r"""                continue
            self.results.cycles += 1""",
        r"""                continue
            # reset_if_new_day() rebinds MarketDataEngine.state to a fresh
            # dict on day rollover (including the first cycle, when the
            # clock jumps from its January init to the replay date). The
            # handle captured before the loop would silently detach, so
            # every cooldown / halt / stop counter the harness writes
            # would land in a dead dict the strategy never reads —
            # replayed sessions then re-entered instantly with no
            # cooldown. Re-fetch the live handle every cycle.
            state = self.me.state
            self.results.cycles += 1""",
        r"""            # reset_if_new_day() rebinds MarketDataEngine.state to a fresh""",
    ),
]


ALL_FILES = [
    ("core.py", CORE_EDITS),
    ("regime_engine.py", RE_EDITS),
    ("data_engine.py", DE_EDITS),
    ("strategy_engine.py", SE_EDITS),
    ("execution_engine.py", XE_EDITS),
    ("backtest_engine.py", BE_EDITS),
]


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    print(f"repo: {REPO_ROOT}")
    print("mode:", "CHECK (no changes)" if check else "APPLY")

    # Phase 1: validate everything without writing.
    errors = []
    plans = []
    for path, edits in ALL_FILES:
        try:
            plans.append((path, plan_edits(path, edits)))
        except EditError as exc:
            errors.append(str(exc))
            plans.append((path, (None, None)))

    if errors:
        print("\nNOTHING WAS MODIFIED - the following edits could not anchor:\n")
        for e in errors:
            print("  x", e)
        return 1

    # Phase 2: apply.
    total_applied = total_already = 0
    for path, (to_apply, already) in plans:
        if check:
            for e in to_apply:
                print(f"  [would apply] {path}:{e.name}")
            for n in already:
                print(f"  [already]     {path}:{n}")
            total_applied += len(to_apply)
            total_already += len(already)
            continue
        if to_apply:
            raw = _read_raw(REPO_ROOT / path)
            eol = _line_ending_style(raw)
            text = _normalize(raw)
            for e in to_apply:
                text = text.replace(e.old, e.new, 1)
            _write(REPO_ROOT / path, text, eol)
        for e in to_apply:
            print(f"  applied      {path}:{e.name}")
        for n in already:
            print(f"  already      {path}:{n}")
        total_applied += len(to_apply)
        total_already += len(already)

    print()
    print(f"applied {total_applied} edit(s), already applied {total_already}")
    if check:
        print("(--check: nothing was written)")
    else:
        print("patch complete.")
        print()
        print("Verify:")
        print("  python3 backtest_engine.py --test")
        print("  python3 backtest_engine.py --from 2026-09-08 --to 2026-09-09")
    return 0


if __name__ == "__main__":
    sys.exit(main())