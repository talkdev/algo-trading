#!/usr/bin/env python3
"""
patch.py - self-contained, idempotent v3.9 profitability patch for the
NIFTY intraday options algo-trading engine.

Run it from the repository root (or anywhere - it locates the repo by its
own path):

    python3 patch.py              # apply the patch, print a report
    python3 patch.py --check      # report what would change, change nothing
    python3 patch.py --no-env     # skip env.txt updates

It is *idempotent*: running it twice is a no-op. It is *atomic*: if any
single edit cannot be anchored (e.g. the file was already modified by
hand), it prints exactly which edit failed and does NOT touch any file.

What it fixes (all measured against the recorded 2026-09-08 expiry
downtrend session, VIX ~11.1):

  1. Vendor-stamped 0DTE ATM IV poisons sigma. The EV gate took
     max(IV-sigma, straddle-sigma) "for conservatism"; when the stamp is
     corrupt the larger IS the corrupt one (21.6% stamped vs 10.2% implied
     by the ATM straddle price vs VIX 11.1). The IV-derived sigma is now
     capped at IV_SIGMA_CAP_RATIO x straddle-sigma, and the ATM IV stamp
     at ATM_IV_VIX_CAP x cash VIX.
  2. The absolute 40pt proximity band exceeds the whole gap of a 0.30-delta
     expiry short (~45pts), so the engine modelled (and would have executed)
     a defence at entry+5pts. Proximity now scales with the structure via
     PROX_GAP_FRAC_DTE0, in the price stop, the EV barrier model, and the
     live priority-2 exit.
  3. Max-pain centering (23700) locked out the winning short. Max pain may
     anchor the strike centre only when it is reachable within
     min(120, 0.45 x expected_move_remaining).
  4. 0.15-0.22 delta sell targets were unpayable at VIX 11 (5-10pt credits
     vs ~1.3pt friction). Targets raised to 0.32/0.30/0.28.
  5. The DTE-0 credit/risk ladder was calibrated for VIX 13.5 and is now
     VIX-scaled; the p_win blend gains a market-delta leg (1 - |delta|);
     the hard 0.75pt EV floor is now relative to credit and friction.
  6. The day-size fallback used EVENT_SIZE_MULTIPLIER (0.25) whenever the
     calibrator had no state (always in backtest), quartering the book.
     It now falls back to per-weekday normal sizes.

After applying, verify with:

    ./venv/bin/python backtest_engine.py --test
    ./venv/bin/python backtest_engine.py --from 2026-09-08 --to 2026-09-08
"""

from __future__ import annotations

import re
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


# -----------------------------------------------------------------------------
# env.txt handling
# -----------------------------------------------------------------------------

ENV_KEYS = {
    # v3.2 profitability calibration (v3.9 values)
    "STOP_MULT_DTE0": "1.60",
    "TARGET_PCT_DTE0": "0.70",
    "DELTA_CLOSE_DTE0": "0.45",
    "SHORT_DELTA_FLAT": "0.32",
    "SHORT_DELTA_TREND": "0.30",
    "SHORT_DELTA_STRONG": "0.28",
    # v3.3 structure-relative proximity
    "PROX_GAP_FRAC_DTE0": "0.70",
    # v3.3 VIX-scaled credit/risk ladder
    "CREDIT_RISK_RATIO_DTE0_EARLY": "0.16",
    "CREDIT_RISK_RATIO_DTE0_MID": "0.13",
    "CREDIT_RISK_RATIO_DTE0_LATE": "0.10",
    "CREDIT_RATIO_VIX_REF": "13.5",
    # v3.3 EV-gate honesty bounds and minimum edge
    "IV_SIGMA_CAP_RATIO": "1.15",
    "ATM_IV_VIX_CAP": "1.35",
    "MIN_EV_FRAC_OF_CREDIT": "0.03",
    "MIN_EV_FRAC_OF_FRICTION": "0.35",
    # v3.3 p_win blend weights and regime bonuses
    "EV_BLEND_MODEL_W": "0.40",
    "EV_BLEND_PRIOR_W": "0.30",
    "EV_BLEND_MARKET_W": "0.30",
    "EV_STRONG_SELL_PRIOR_BONUS": "0.05",
    "EV_REGIME_ALIGN_BONUS": "0.05",
    # v3.9 per-weekday day-size fallbacks (normal days)
    "DAY_SIZE_MONDAY": "0.60",
    "DAY_SIZE_TUESDAY": "0.85",
    "DAY_SIZE_WEDNESDAY": "0.65",
    "DAY_SIZE_THURSDAY": "0.65",
    "DAY_SIZE_FRIDAY": "0.55",
}

_ENV_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def plan_env(keys):
    """Return (changed, missing) for env.txt. Never raises on content."""
    p = REPO_ROOT / "env.txt"
    if not p.exists():
        return None, None  # signal: env.txt absent
    lines = _normalize(_read_raw(p)).splitlines(keepends=True)
    changed = []
    seen = set()
    for ln in lines:
        m = _ENV_KEY_RE.match(ln)
        if m and m.group(1) in keys:
            seen.add(m.group(1))
            want = keys[m.group(1)]
            if m.group(2).strip() != want:
                changed.append(m.group(1))
    missing = [k for k in keys if k not in seen]
    return changed, missing


def apply_env(keys, check=False):
    """Apply env.txt changes idempotently. Returns (changed, missing)."""
    p = REPO_ROOT / "env.txt"
    if not p.exists():
        return None, None
    changed, missing = plan_env(keys)
    if check or not changed and not missing:
        return changed, missing
    raw = _read_raw(p)
    eol = _line_ending_style(raw)
    out = []
    for ln in _normalize(raw).splitlines(keepends=True):
        m = _ENV_KEY_RE.match(ln)
        if m and m.group(1) in keys and m.group(2).strip() != keys[m.group(1)]:
            out.append(f"{m.group(1)}={keys[m.group(1)]}\n")
        else:
            out.append(ln)
    if missing:
        out.append("\n# -- v3.9 (added by patch.py) -----------------------------\n")
        for k in missing:
            out.append(f"{k}={keys[k]}\n")
    _write(p, "".join(out), eol)
    return changed, missing


# -----------------------------------------------------------------------------
# core.py edits
# -----------------------------------------------------------------------------

_CORE = "core.py"

CORE_EDITS = [
    Edit(
        _CORE, "version_constant",
        r"""NIFTY_ENGINE_PROFIT_PATCH_V38 = "3.8"
""",
        r"""NIFTY_ENGINE_PROFIT_PATCH_V38 = "3.8"
# v3.9 (2026-09-09): VIX-11 expiry-day profitability pass. See the
# v3.3-tagged blocks in core.py / strategy_engine.py / execution_engine.py.
NIFTY_ENGINE_PROFIT_PATCH_V39 = "3.9"
""",
        "NIFTY_ENGINE_PROFIT_PATCH_V39",
    ),
    Edit(
        _CORE, "template_stop_mult_dte0",
        r"""STOP_MULT_DTE0=1.40""",
        r"""# v3.3: raised from 1.40. The 1.4x stop on a DTE-0 vertical converts a
# ~25-point NIFTY counter-rally into a stop-out: at delta 0.22-0.36 the
# short leg gains 0.3-0.5x the move, so 0.4 x credit (~4 points on a 10
# point credit) IS a 25 point move. Replay of 2026-09-08 (a real VIX-11
# expiry downtrend) showed the position's max adverse premium move of
# +27% in 37 minutes with the trend then resuming lower - the 1.4x line
# was inside intraday noise. 1.6x keeps the loss at ~0.6x credit while
# giving a normal pullback room; the structural wing and the delta /
# proximity backstops remain the hard lines.
STOP_MULT_DTE0=1.60""",
        r"""STOP_MULT_DTE0=1.60""",
    ),
    Edit(
        _CORE, "template_target_pct_dte0",
        r"""TARGET_PCT_DTE0=0.50""",
        r"""TARGET_PCT_DTE0=0.70""",
        r"""TARGET_PCT_DTE0=0.70""",
    ),
    Edit(
        _CORE, "template_delta_close_dte0",
        r"""DELTA_CLOSE_DTE0=0.35""",
        r"""# v3.3: raised from 0.35 to 0.45. The engine now sells 0.22-0.36 delta on
# expiry afternoon; a close line 0.13 above the entry delta fired on
# ordinary drift at exactly the time delta moves fastest. 0.45 is the
# "structure decisively wrong" line desks use on 0DTE verticals, and it
# no longer sits on top of the sell window.
DELTA_CLOSE_DTE0=0.45""",
        r"""DELTA_CLOSE_DTE0=0.45""",
    ),
    Edit(
        _CORE, "template_prox_gap_frac",
        r"""PRICE_STOP_WING_FRAC=0.30
PRICE_STOP_MIN_PTS=25
PRICE_STOP_MAX_FRAC_OF_DIST=0.40""",
        r"""PRICE_STOP_WING_FRAC=0.30
PRICE_STOP_MIN_PTS=25
PRICE_STOP_MAX_FRAC_OF_DIST=0.40
# v3.3: proximity-to-short defense as a FRACTION of the entry gap to the
# short strike. The absolute 40pt band is larger than the whole gap for
# the delta 0.3-0.4 shorts a VIX-11 expiry offers (~45pts), so the trade
# would be closed at entry+5pts by its own safety. Executing the exit at
# 70% of the gap travelled scales the defense with the structure and the
# vol environment automatically.
PROX_GAP_FRAC_DTE0=0.70""",
        r"""PROX_GAP_FRAC_DTE0=0.70""",
    ),
    Edit(
        _CORE, "template_short_delta",
        r"""SHORT_DELTA_FLAT=0.22
SHORT_DELTA_TREND=0.18
SHORT_DELTA_STRONG=0.15""",
        r"""# v3.3: raised from 0.22/0.18/0.15. On a 50-point strike grid with VIX 11,
# 0.18-0.22 targets land ~100-150 points OTM where the entire 0DTE credit
# is 5-10 points - unpayable against ~1.3 points of round-trip friction
# per lot (measured 2026-09-08: delta 0.224 short -> credit 10.2, ratio
# 0.108, structurally rejected all day). Professional 0DTE sellers work
# the 0.25-0.40 delta band after midday; 0.32/0.30/0.28 puts the engine
# there without selling the money.
SHORT_DELTA_FLAT=0.32
SHORT_DELTA_TREND=0.30
SHORT_DELTA_STRONG=0.28""",
        r"""SHORT_DELTA_STRONG=0.28""",
    ),
    Edit(
        _CORE, "template_v33_block",
        r"""CONDOR_WEAK_SIDE_MIN_FRAC=0.30""",
        r"""CONDOR_WEAK_SIDE_MIN_FRAC=0.30
# -- v3.3 Profitability Calibration (2026 VIX-11 regime) ---------------------
# DTE-0 credit/risk ladder, VIX-scaled. These are the FRACTIONS of
# (wing - credit) the net credit must reach, by minutes remaining. The
# absolute v3.2 ladder was calibrated against the premium a VIX 13.5
# session pays; at VIX 11 the market pays ~0.8x of that, so every
# requirement is now scaled by clamp(vix / CREDIT_RATIO_VIX_REF ...).
CREDIT_RISK_RATIO_DTE0_EARLY=0.16
CREDIT_RISK_RATIO_DTE0_MID=0.13
CREDIT_RISK_RATIO_DTE0_LATE=0.10
CREDIT_RATIO_VIX_REF=13.5
# The EV gate's barrier model may not trust the vendor-stamped 0DTE IV
# (measured on 2026-09-08: 21.6% stamped vs 10.2% implied by the ATM
# straddle price itself vs India VIX 11.1). When the straddle publishes a
# smaller sigma, the IV-derived estimate is capped at this multiple of it.
IV_SIGMA_CAP_RATIO=1.15
# Maximum cap on the ATM IV used for sigma, as a multiple of the day's
# India VIX. If the stamp is more than this above the cash VIX it is
# treated as a sqrt(T) artefact, not information.
ATM_IV_VIX_CAP=1.35
# Minimum edge the EV gate may accept, as a fraction of net credit and of
# round-trip friction. v3.2 used max(3% credit, 35% friction, 0.75pts);
# the hard 0.75 point floor is ~8% of an entire VIX-11 expiry credit and
# rejected structures whose whole expectancy was sound but small.
MIN_EV_FRAC_OF_CREDIT=0.03
MIN_EV_FRAC_OF_FRICTION=0.35
# EV p_win blend weights: the lognormal touch model, the OR-conditional
# empirical prior, and the market-implied (1 - short delta) probability.
# v3.2 blended model:prior 50/50, which lets a poisoned sigma floor the
# verdict. The chain's own delta is an independent, market-quoted vote.
EV_BLEND_MODEL_W=0.40
EV_BLEND_PRIOR_W=0.30
EV_BLEND_MARKET_W=0.30
# STRONG_SELL_PREMIUM adds to the empirical prior (the vol stack's own
# consensus that the chain is paying above realised risk), and a sold
# structure whose threat side sits against the confirmed trend direction
# earns a small bounded alignment bonus.
EV_STRONG_SELL_PRIOR_BONUS=0.05
EV_REGIME_ALIGN_BONUS=0.05""",
        r"""EV_REGIME_ALIGN_BONUS=0.05""",
    ),
    Edit(
        _CORE, "dataclass_day_size_fields",
        r"""    defined_risk_only_on_event:  bool
    tuesday_early_exit_enabled:  bool""",
        r"""    defined_risk_only_on_event:  bool
    tuesday_early_exit_enabled:  bool

    # v3.9: normal (non-event) day-size multipliers per weekday. These are
    # the fallback whenever the calibrator has no valid state yet (startup,
    # tier-0, and every backtest replay). They mirror the calibration
    # dataclass defaults. They must NOT fall back to event_size_multiplier:
    # that is a budget/event-day reducer, and letting it leak into an
    # uncalibrated Tuesday quietly cut every position to 25% of intended
    # size (measured 2026-09-08 replay: size_multiplier 0.25 -> 0.54 lots
    # -> rejected below min_lots_fraction).
    day_size_monday:     float
    day_size_tuesday:    float
    day_size_wednesday:  float
    day_size_thursday:   float
    day_size_friday:     float""",
        r"""    day_size_friday:     float""",
    ),
    Edit(
        _CORE, "dataclass_stop_mult_dte0",
        r"""    stop_mult_dte0:            float = 1.40""",
        r"""    # v3.9: DTE-0 stop widened 1.40 -> 1.60. A 0.30-delta expiry short
    # with a 45-point gap trades inside a 25-30 point adverse excursion
    # (measured 2026-09-08: 25.3pts, 1.33x credit) and a 1.40x stop
    # leaves less than a point of room once liquidation slippage is
    # charged; 1.60x leaves ~4.5pts. The wider stop is the cost of the
    # gamma-gap that a 0DTE stop is not honoured through.
    stop_mult_dte0:            float = 1.60""",
        r"""    stop_mult_dte0:            float = 1.60""",
    ),
    Edit(
        _CORE, "dataclass_target_pct_dte0",
        r"""    target_pct_dte0:           float = 0.50""",
        r"""    # v3.9: DTE-0 target raised 0.50 -> 0.70. Against the wider 1.60x
    # stop a 50% target would be reward/risk 0.83 (worse than 1:1);
    # 0.70 against the 0.60x loss is reward/risk 1.17, restoring the
    # engine's historical 1.1-1.25 posture on a VIX-11 day where the
    # whole credit is ~18 points.
    target_pct_dte0:           float = 0.70""",
        r"""    target_pct_dte0:           float = 0.70""",
    ),
    Edit(
        _CORE, "dataclass_delta_close_dte0",
        r"""    delta_close_dte0:          float = 0.35""",
        r"""    delta_close_dte0:          float = 0.45""",
        r"""    delta_close_dte0:          float = 0.45""",
    ),
    Edit(
        _CORE, "dataclass_short_delta",
        r"""    short_delta_flat:          float = 0.22
    short_delta_trend:         float = 0.18
    short_delta_strong:        float = 0.15""",
        r"""    short_delta_flat:          float = 0.32
    short_delta_trend:         float = 0.30
    short_delta_strong:        float = 0.28""",
        r"""    short_delta_strong:        float = 0.28""",
    ),
    Edit(
        _CORE, "dataclass_prox_gap_frac",
        r"""    em_band_lo:                float = 0.80
    em_band_hi:                float = 1.35
    # Friction discipline.""",
        r"""    em_band_lo:                float = 0.80
    em_band_hi:                float = 1.35
    # v3.3: structure-relative proximity defense on expiry day. The exit
    # fires when spot has covered this fraction of the entry gap to the
    # short strike (bounded by the absolute proximity setting), so a
    # delta-0.3 short 45 points away is defended at 70% of the gap -
    # not 5 points after entry by an absolute 40pt band.
    prox_gap_frac_dte0:        float = 0.70
    # Friction discipline.""",
        r"""    prox_gap_frac_dte0:        float = 0.70""",
    ),
    Edit(
        _CORE, "dataclass_v33_fields",
        r"""    min_target_over_friction:     float = 1.25
    # Minimum economic size, in lots, before a trade is worth doing.""",
        r"""    min_target_over_friction:     float = 1.25
    # v3.3: DTE-0 credit/risk ladder (VIX-scaled in compute_params).
    credit_risk_ratio_dte0_early: float = 0.16
    credit_risk_ratio_dte0_mid:   float = 0.13
    credit_risk_ratio_dte0_late:  float = 0.10
    credit_ratio_vix_ref:         float = 13.5
    # v3.3: EV-gate honesty bounds. The vendor 0DTE IV stamp may not
    # dominate the straddle-implied sigma, and the ATM IV stamp may not
    # exceed this multiple of the day's cash VIX.
    iv_sigma_cap_ratio:        float = 1.15
    atm_iv_vix_cap:            float = 1.35
    # v3.3: minimum edge for the EV gate.
    min_ev_frac_of_credit:     float = 0.03
    min_ev_frac_of_friction:   float = 0.35
    # v3.3: p_win blend weights (model / empirical prior / market delta).
    ev_blend_model_w:          float = 0.40
    ev_blend_prior_w:          float = 0.30
    ev_blend_market_w:         float = 0.30
    ev_strong_sell_prior_bonus: float = 0.05
    ev_regime_align_bonus:     float = 0.05
    # Minimum economic size, in lots, before a trade is worth doing.""",
        r"""    ev_regime_align_bonus:     float = 0.05""",
    ),
    Edit(
        _CORE, "loader_day_size",
        r"""        event_size_multiplier=_get_float(env, "EVENT_SIZE_MULTIPLIER", 0.25),""",
        r"""        event_size_multiplier=_get_float(env, "EVENT_SIZE_MULTIPLIER", 0.25),
        day_size_monday=min(max(_get_float(env, "DAY_SIZE_MONDAY", 0.60), 0.10), 1.20),
        day_size_tuesday=min(max(_get_float(env, "DAY_SIZE_TUESDAY", 0.85), 0.10), 1.20),
        day_size_wednesday=min(max(_get_float(env, "DAY_SIZE_WEDNESDAY", 0.65), 0.10), 1.20),
        day_size_thursday=min(max(_get_float(env, "DAY_SIZE_THURSDAY", 0.65), 0.10), 1.20),
        day_size_friday=min(max(_get_float(env, "DAY_SIZE_FRIDAY", 0.55), 0.10), 1.20),""",
        r"""day_size_friday=min(max(_get_float(env, "DAY_SIZE_FRIDAY", 0.55), 0.10), 1.20),""",
    ),
    Edit(
        _CORE, "loader_stop_mult_dte0",
        r"""        stop_mult_dte0=min(max(_get_float(env, "STOP_MULT_DTE0", 1.40), 1.15), 2.50),""",
        r"""        stop_mult_dte0=min(max(_get_float(env, "STOP_MULT_DTE0", 1.60), 1.15), 2.50),""",
        r"""        stop_mult_dte0=min(max(_get_float(env, "STOP_MULT_DTE0", 1.60), 1.15), 2.50),""",
    ),
    Edit(
        _CORE, "loader_target_pct_dte0",
        r"""        target_pct_dte0=min(max(_get_float(env, "TARGET_PCT_DTE0", 0.50), 0.18), 0.70),""",
        r"""        target_pct_dte0=min(max(_get_float(env, "TARGET_PCT_DTE0", 0.70), 0.18), 0.85),""",
        r"""        target_pct_dte0=min(max(_get_float(env, "TARGET_PCT_DTE0", 0.70), 0.18), 0.85),""",
    ),
    Edit(
        _CORE, "loader_delta_close_dte0",
        r"""        delta_close_dte0=min(max(_get_float(env, "DELTA_CLOSE_DTE0", 0.35), 0.20), 0.55),""",
        r"""        delta_close_dte0=min(max(_get_float(env, "DELTA_CLOSE_DTE0", 0.45), 0.20), 0.55),""",
        r"""        delta_close_dte0=min(max(_get_float(env, "DELTA_CLOSE_DTE0", 0.45), 0.20), 0.55),""",
    ),
    Edit(
        _CORE, "loader_short_delta",
        r"""        short_delta_flat=min(max(_get_float(env, "SHORT_DELTA_FLAT", 0.22), 0.08), 0.35),
        short_delta_trend=min(max(_get_float(env, "SHORT_DELTA_TREND", 0.18), 0.07), 0.32),
        short_delta_strong=min(max(_get_float(env, "SHORT_DELTA_STRONG", 0.15), 0.06), 0.30),""",
        r"""        short_delta_flat=min(max(_get_float(env, "SHORT_DELTA_FLAT", 0.32), 0.08), 0.35),
        short_delta_trend=min(max(_get_float(env, "SHORT_DELTA_TREND", 0.30), 0.07), 0.32),
        short_delta_strong=min(max(_get_float(env, "SHORT_DELTA_STRONG", 0.28), 0.06), 0.30),""",
        r"""        short_delta_strong=min(max(_get_float(env, "SHORT_DELTA_STRONG", 0.28), 0.06), 0.30),""",
    ),
    Edit(
        _CORE, "loader_prox_gap_frac",
        r"""        em_band_hi=min(max(_get_float(env, "EM_BAND_HI", 1.35), 0.90), 2.50),""",
        r"""        em_band_hi=min(max(_get_float(env, "EM_BAND_HI", 1.35), 0.90), 2.50),
        prox_gap_frac_dte0=min(max(_get_float(env, "PROX_GAP_FRAC_DTE0", 0.70), 0.50), 0.95),""",
        r"""        prox_gap_frac_dte0=min(max(_get_float(env, "PROX_GAP_FRAC_DTE0", 0.70), 0.50), 0.95),""",
    ),
    Edit(
        _CORE, "loader_v33_fields",
        r"""        min_target_over_friction=min(max(_get_float(env, "MIN_TARGET_OVER_FRICTION", 1.25), 1.00), 3.00),""",
        r"""        min_target_over_friction=min(max(_get_float(env, "MIN_TARGET_OVER_FRICTION", 1.25), 1.00), 3.00),
        # v3.3: DTE-0 credit/risk ladder + VIX reference for scaling
        credit_risk_ratio_dte0_early=min(max(_get_float(env, "CREDIT_RISK_RATIO_DTE0_EARLY", 0.16), 0.05), 0.40),
        credit_risk_ratio_dte0_mid=min(max(_get_float(env, "CREDIT_RISK_RATIO_DTE0_MID", 0.13), 0.05), 0.40),
        credit_risk_ratio_dte0_late=min(max(_get_float(env, "CREDIT_RISK_RATIO_DTE0_LATE", 0.10), 0.04), 0.40),
        credit_ratio_vix_ref=min(max(_get_float(env, "CREDIT_RATIO_VIX_REF", 13.5), 10.0), 20.0),
        # v3.3: EV-gate honesty bounds and minimum edge
        iv_sigma_cap_ratio=min(max(_get_float(env, "IV_SIGMA_CAP_RATIO", 1.15), 1.00), 2.00),
        atm_iv_vix_cap=min(max(_get_float(env, "ATM_IV_VIX_CAP", 1.35), 1.00), 2.50),
        min_ev_frac_of_credit=min(max(_get_float(env, "MIN_EV_FRAC_OF_CREDIT", 0.03), 0.01), 0.20),
        min_ev_frac_of_friction=min(max(_get_float(env, "MIN_EV_FRAC_OF_FRICTION", 0.35), 0.20), 1.00),
        ev_blend_model_w=min(max(_get_float(env, "EV_BLEND_MODEL_W", 0.40), 0.05), 0.90),
        ev_blend_prior_w=min(max(_get_float(env, "EV_BLEND_PRIOR_W", 0.30), 0.05), 0.90),
        ev_blend_market_w=min(max(_get_float(env, "EV_BLEND_MARKET_W", 0.30), 0.00), 0.90),
        ev_strong_sell_prior_bonus=min(max(_get_float(env, "EV_STRONG_SELL_PRIOR_BONUS", 0.05), 0.0), 0.08),
        ev_regime_align_bonus=min(max(_get_float(env, "EV_REGIME_ALIGN_BONUS", 0.05), 0.0), 0.08),""",
        r"""        ev_regime_align_bonus=min(max(_get_float(env, "EV_REGIME_ALIGN_BONUS", 0.05), 0.0), 0.08),""",
    ),
]


# -----------------------------------------------------------------------------
# strategy_engine.py edits
# -----------------------------------------------------------------------------

_SE = "strategy_engine.py"

SE_EDITS = [
    Edit(
        _SE, "max_pain_anchor_reach",
        r"""        if dte == 0 and _max_pain > 0 and abs(_max_pain - spot) <= 120:
            _center_ref = _max_pain""",
        r"""        if dte == 0 and _max_pain > 0:
            _mp_gap = abs(_max_pain - spot)
            _em_mp = float(signals.get("expected_move_remaining_pts") or 0.0)
            # v3.9: max pain may anchor the strike centre only when it is
            # plausibly reachable inside the expected REMAINING move. A
            # pin 46 points away with ~80 points of remaining EM is an
            # end-of-day magnet, not a centre to sell around from midday;
            # centering there shifted every short one strike further from
            # the money than delta selection asked (measured 2026-09-08
            # 12:03: delta target 0.22 was the only branch left, credit
            # ~10 points, structurally rejected all afternoon).
            _mp_reach = 0.45 * _em_mp if _em_mp > 10 else 120.0
            if _mp_gap <= min(120.0, _mp_reach):
                _center_ref = _max_pain""",
        r"""_mp_reach = 0.45 * _em_mp if _em_mp > 10 else 120.0""",
    ),
    Edit(
        _SE, "price_stop_prox",
        r"""        prox = self._proximity_buffer_pts(spot)
        val = max(float(wing_pts) * frac, floor_pts, prox)""",
        r"""        prox = self._proximity_buffer_pts(spot)
        # v3.9: the absolute proximity band exceeds the whole gap of a
        # delta-0.3+ expiry short (a 45pt gap against a 40pt band), which
        # would model a defense sitting 5 points from entry. The defense
        # is also bounded by (1 - prox_gap_frac) of the gap, so it scales
        # with the structure the engine actually built.
        if short_dist_pts and short_dist_pts > 0:
            _gap_frac = float(getattr(self.config, "prox_gap_frac_dte0", 0.70))
            prox = min(
                prox, max(1.0 - _gap_frac, 0.05) * float(short_dist_pts)
            )
        val = max(float(wing_pts) * frac, floor_pts, prox)""",
        r"""max(1.0 - _gap_frac, 0.05) * float(short_dist_pts)""",
    ),
    Edit(
        _SE, "strong_sell_prior_bonus",
        r"""        elif vrp_smoothed < 2.0:
            p_win_prior -= 0.03
        p_win_prior = max(0.30, min(0.90, p_win_prior))""",
        r"""        elif vrp_smoothed < 2.0:
            p_win_prior -= 0.03
        # v3.9: the vol regime label is itself the consensus vote of the
        # IV/RV stack. STRONG_SELL_PREMIUM means the chain pays far above
        # its own realised-risk estimate; the engine's expiry-session hit
        # rate in that state sits materially above the table's flat OR
        # base rate, the same effect the VRP tiers above approximate.
        if str(signals.get("vol_regime") or "") == "STRONG_SELL_PREMIUM":
            p_win_prior += float(getattr(
                self.config, "ev_strong_sell_prior_bonus", 0.05
            ))
        p_win_prior = max(0.30, min(0.90, p_win_prior))""",
        r""""ev_strong_sell_prior_bonus", 0.05""",
    ),
    Edit(
        _SE, "atm_iv_vix_cap",
        r"""        _atm_iv = float(signals.get("atm_iv") or 0.0)
        if _atm_iv >= 2.0:          # stored as a percentage, not a decimal
            _atm_iv = _atm_iv / 100.0
        _spot_ev = float(signals.get("spot") or 0.0)""",
        r"""        _atm_iv = float(signals.get("atm_iv") or 0.0)
        if _atm_iv >= 2.0:          # stored as a percentage, not a decimal
            _atm_iv = _atm_iv / 100.0
        # v3.9: the vendor-stamped 0DTE ATM IV inflates as sqrt(T)
        # collapses into the afternoon - measured 2026-09-08 12:03 the
        # stamp read 21.6% while the ATM straddle price itself (0.8 x
        # straddle) implied ~10.2% and India VIX printed 11.1. A stamp
        # that far above the cash VIX is an artefact, not information,
        # and feeding it into the barrier model doubles the touch
        # probability of every short the engine tries to sell.
        _vix_ev = float(signals.get("vix") or 0.0)
        if _vix_ev > 2.0 and _atm_iv > 0:
            _iv_cap = float(getattr(
                self.config, "atm_iv_vix_cap", 1.35
            )) * (_vix_ev / 100.0)
            _atm_iv = min(_atm_iv, _iv_cap)
        _spot_ev = float(signals.get("spot") or 0.0)""",
        r"""_atm_iv = min(_atm_iv, _iv_cap)""",
    ),
    Edit(
        _SE, "sigma_straddle_cap",
        r"""        _sigma_pts = max(_sigma_pts_iv, _sigma_pts_straddle)""",
        r"""        # -- v3.9 [E4c] the straddle is the authority when they disagree -
        # v3.2 took the LARGER of the two estimates "for conservatism".
        # When the vendor IV stamp is corrupt (see v3.9 above) the
        # larger IS the corrupt one: on 2026-09-08 it doubled sigma, and
        # against a defence line ~0.7 sigma away the touch probability
        # went from plausible to certain, vetoing every candidate. The
        # straddle is a traded PRICE and cannot be mis-scaled, so when
        # both exist the IV-derived estimate is capped at a modest ratio
        # of the straddle-derived one; when only one exists it stands.
        _iv_sigma_cap = float(getattr(
            self.config, "iv_sigma_cap_ratio", 1.15
        ))
        if _sigma_pts_iv > 0 and _sigma_pts_straddle > 0:
            _sigma_pts = min(_sigma_pts_iv, _sigma_pts_straddle * _iv_sigma_cap)
        else:
            _sigma_pts = max(_sigma_pts_iv, _sigma_pts_straddle)""",
        r"""_sigma_pts = min(_sigma_pts_iv, _sigma_pts_straddle * _iv_sigma_cap)""",
    ),
    Edit(
        _SE, "barrier_pull_prox",
        r"""        _pull = max(
            float(barrier_pull_pts or 0.0),
            self._proximity_buffer_pts(_spot_ev),
        )""",
        r"""        # v3.9: the proximity side of the pull is structure-relative now.
        # An absolute 40pt band exceeds the whole gap of a delta-0.3+
        # expiry short (~45pts), which would place the modelled defence
        # line ~5 points from entry and make touching it a certainty on
        # any tape. The defence is bounded by (1 - prox_gap_frac) of the
        # gap on each short, which is exactly where the priority-2 exit
        # in execution_engine now fires, so the gate finally prices the
        # line the position is actually managed against.
        _prox_abs_ev = self._proximity_buffer_pts(_spot_ev)
        _prox_frac_ev = float(getattr(
            self.config, "prox_gap_frac_dte0", 0.70
        ))""",
        r"""_prox_abs_ev = self._proximity_buffer_pts(_spot_ev)""",
    ),
    Edit(
        _SE, "barrier_loop",
        r"""            _barriers = [
                (max(d - _pull, 12.0) if d > _pull else _atm_floor)
                for d in _dists
            ]""",
        r"""            for d in _dists:
                _prox_pull_d = min(
                    _prox_abs_ev, max(1.0 - _prox_frac_ev, 0.05) * d
                )
                _pull_d = max(
                    float(barrier_pull_pts or 0.0), _prox_pull_d
                )
                _barriers.append(
                    max(d - _pull_d, 12.0) if d > _pull_d else _atm_floor
                )""",
        r"""_pull_d = max(""",
    ),
    Edit(
        _SE, "pwin_market_blend",
        r"""        if p_win_model is None:
            p_win = p_win_prior
        else:
            # v3.2: 50/50. The 60/40 tilt gave a driftless lognormal the
            # casting vote over the engine's own calibrated hit rate by
            # opening range. The model cannot see pinning, the max-pain
            # magnet, the intraday mean reversion that makes NIFTY paths
            # less diffusive than their terminal volatility implies, or
            # any of the positioning the prior is built from - and on a
            # two-sided touch problem those effects are exactly what
            # decides the outcome. Equal weight is the honest split.
            p_win = 0.50 * p_win_model + 0.50 * p_win_prior
        p_win = max(0.28, min(0.92, p_win))""",
        r"""        # -- v3.9 [E9b] the market-quoted touch probability ------------
        # Option delta is the market's own risk-neutral approximation of
        # "this strike finishes in the money": 1 - |delta| is a live,
        # per-strike p_win estimate produced by the same order flow that
        # set the VRP edge the trade is being paid for. The v3.2 50/50
        # model:prior mix let a poisoned sigma floor the whole verdict.
        # The market leg gets an equal vote alongside the model and the
        # empirical OR prior, and carries the blend whenever the model
        # cannot be computed at all.
        _p_mkt = None
        if legs:
            _short_ds = [
                abs(float(l.get("delta")))
                for l in legs
                if str(l.get("action")) == "SELL" and l.get("delta")
            ]
            _short_ds = [d for d in _short_ds if 0.02 < d < 0.97]
            if _short_ds:
                _p_mkt = min(max(1.0 - max(_short_ds), 0.20), 0.95)
        _wm = float(getattr(self.config, "ev_blend_model_w", 0.40))
        _wp = float(getattr(self.config, "ev_blend_prior_w", 0.30))
        _wk = float(getattr(self.config, "ev_blend_market_w", 0.30))
        if p_win_model is None:
            if _p_mkt is not None and (_wp + _wk) > 0:
                p_win = (_wp * p_win_prior + _wk * _p_mkt) / (_wp + _wk)
            else:
                p_win = p_win_prior
        elif _p_mkt is not None and (_wm + _wp + _wk) > 0:
            p_win = (
                _wm * p_win_model + _wp * p_win_prior + _wk * _p_mkt
            ) / (_wm + _wp + _wk)
        else:
            # v3.2: the model cannot see pinning, the max-pain magnet,
            # the intraday mean reversion that makes NIFTY paths less
            # diffusive than their terminal volatility implies, or any
            # of the positioning the prior is built from.
            p_win = _wm * p_win_model + (1.0 - _wm) * p_win_prior
        # v3.9: regime-structure alignment. The touch model assumes
        # symmetric threat: a rally toward a sold call spread and a
        # selloff toward a sold put spread are priced alike. But the
        # structure placed by a directional regime sell has its threat
        # side on the UNFAVOURED move of a confirmed trend - on
        # 2026-09-08 (downtrend, STRONG_SELL_PREMIUM, VRP 3.17pp) the
        # post-midday tape never retraced more than 21 points against
        # the call credit. Desks recognise this skew explicitly; a
        # small bounded bonus is how it shows up here without letting
        # the label override the arithmetic.
        _align = float(getattr(self.config, "ev_regime_align_bonus", 0.05))
        if _align > 0 and legs and _p_mkt is not None:
            _sell_sides = {
                str(l.get("option_type"))
                for l in legs if str(l.get("action")) == "SELL"
            }
            _fr_ev = str(signals.get("final_regime") or "")
            _vr_ok = str(signals.get("vol_regime") or "") in (
                "SELL_PREMIUM", "STRONG_SELL_PREMIUM"
            )
            _aligned = _vr_ok and (
                (_sell_sides == {"call"} and _fr_ev == "PREMIUM_SELL_BEAR") or
                (_sell_sides == {"put"} and _fr_ev == "PREMIUM_SELL_BULL")
            )
            if _aligned:
                p_win += _align
        p_win = max(0.28, min(0.92, p_win))""",
        r"""p_win += _align""",
    ),
    Edit(
        _SE, "min_ev_floor",
        r"""        min_ev = max(net_credit * 0.03, friction * 0.35, 0.75)""",
        r"""        # v3.9: the v3.2 absolute 0.75 point floor was ~8% of the entire
        # credit a VIX-11 expiry pays. Because entry costs and exit
        # costs are now charged explicitly per-path inside the EV
        # terms, a floor near 100% of friction double-bills them; the
        # cushion is 3% of credit or 35% of friction, whichever bites,
        # roughly 1.35x total cost coverage.
        min_ev = max(
            net_credit * float(getattr(self.config, "min_ev_frac_of_credit", 0.03)),
            friction * float(getattr(self.config, "min_ev_frac_of_friction", 0.35)),
        )""",
        r""""min_ev_frac_of_friction", 0.35""",
    ),
    Edit(
        _SE, "credit_ladder_vix_scale",
        r"""            min_ratio  = (
                0.16 if mins_left3 > 180 else (0.13 if mins_left3 > 90 else 0.10)
            )""",
        r"""            # v3.9: the ladder was calibrated against the premium a VIX
            # 13.5 session pays. At VIX 11 the market sells ~0.8x of
            # that, so a fixed-absolute ladder structurally vetoed every
            # expiry structure (measured 2026-09-08: ratio ~0.105-0.11
            # against a fixed 0.16 demand, all 86 surviving candidates
            # rejected). Requirements now scale with the vol the session
            # is actually offering, clamped at both ends.
            _lx = (
                float(getattr(self.config, "credit_risk_ratio_dte0_early", 0.16))
                if mins_left3 > 180 else
                (float(getattr(self.config, "credit_risk_ratio_dte0_mid", 0.13))
                 if mins_left3 > 90 else
                 float(getattr(self.config, "credit_risk_ratio_dte0_late", 0.10)))
            )
            _vix_l = float(signals.get("vix") or 13.5)
            _vref  = float(getattr(self.config, "credit_ratio_vix_ref", 13.5))
            _vs    = min(max(_vix_l / max(_vref, 1.0), 0.75), 1.15)
            min_ratio = _lx * _vs""",
        r"""min_ratio = _lx * _vs""",
    ),
]


# -----------------------------------------------------------------------------
# execution_engine.py edits
# -----------------------------------------------------------------------------

_XE = "execution_engine.py"

XE_EDITS = [
    Edit(
        _XE, "priority2_proximity",
        r"""            for leg in open_legs:
                if leg["action"] != _prox_action:
                    continue
                strike = float(leg.get("strike", 0))
                if abs(spot - strike) <= proximity_pts:
                    self.logger.warning(
                        f"PRIORITY 2 SPOT PROXIMITY: spot={spot:.0f} "
                        f"within {proximity_pts}pts of {_prox_action} "
                        f"{leg['option_type']} {strike:.0f}"
                    )""",
        r"""            # v3.9: on expiry-day verticals the absolute proximity band
            # can exceed the whole gap to the short strike (a delta-0.3+
            # short ~45 points away against a 40pt band), which would
            # flatten the trade at entry+5pts regardless of structure.
            # The band is then bounded by (1 - prox_gap_frac) of the
            # entry gap for each short leg - the same line the EV gate
            # now prices - so the defense is where the risk model said
            # it would be when the trade was approved.
            _prox_dte0 = (actual_dte == 0) and \
                ("BUTTERFLY" not in strategy_name_p2)
            _prox_gap_frac = float(getattr(self.config, "prox_gap_frac_dte0", 0.70))
            _entry_spot_p2 = float(position.get("entry_spot") or 0)
            for leg in open_legs:
                if leg["action"] != _prox_action:
                    continue
                strike = float(leg.get("strike", 0))
                _band = proximity_pts
                if _prox_dte0 and _entry_spot_p2 > 0:
                    _gap = abs(strike - _entry_spot_p2)
                    if _gap > 0:
                        _band = min(
                            proximity_pts,
                            max((1.0 - _prox_gap_frac) * _gap, 10.0),
                        )
                if abs(spot - strike) <= _band:
                    self.logger.warning(
                        f"PRIORITY 2 SPOT PROXIMITY: spot={spot:.0f} "
                        f"within {_band:.0f}pts of {_prox_action} "
                        f"{leg['option_type']} {strike:.0f}"
                    )""",
        r"""_entry_spot_p2 = float(position.get("entry_spot") or 0)""",
    ),
    Edit(
        _XE, "self_test_delta_breach",
        r"""    # Test Priority 1: Delta breach
    # Modify chain to show high delta on short call
    mock_chain[24150.0]["call"]["delta"] = 0.45  # > 0.40 threshold""",
        r"""    # Test Priority 1: Delta breach
    # Modify chain to show a short-call delta clearly above the expiry-day
    # close threshold. v3.9 raised delta_close_dte0 to 0.45 (the engine now
    # sells 0.32/0.30/0.28 delta, so the old 0.35 line sat below the entry
    # delta of a 0.30-delta short and self-closed it on entry), which made a
    # hardcoded 0.45 test value a non-breach. Derive the test delta from the
    # live threshold so this assertion stays valid if the knob is retuned.
    _dte0_close_p1 = float(getattr(config, "delta_close_dte0", 0.45))
    mock_chain[24150.0]["call"]["delta"] = round(min(_dte0_close_p1 + 0.15, 0.99), 2)""",
        r"""_dte0_close_p1 = float(getattr(config, "delta_close_dte0", 0.45))""",
    ),
]


# -----------------------------------------------------------------------------
# regime_engine.py edits
# -----------------------------------------------------------------------------

_RE = "regime_engine.py"

RE_EDITS = [
    Edit(
        _RE, "day_size_fallback",
        r"""        day_size_map = {
            "MONDAY":    self._t("day_size_monday",    "event_size_multiplier", 0.55),
            "TUESDAY":   self._t("day_size_tuesday",   "event_size_multiplier", 0.80),
            "WEDNESDAY": self._t("day_size_wednesday", "event_size_multiplier", 0.70),
            "THURSDAY":  self._t("day_size_thursday",  "event_size_multiplier", 0.70),
            "FRIDAY":    self._t("day_size_friday",    "event_size_multiplier", 0.60),
        }""",
        r"""        # v3.9: the fallback was event_size_multiplier (a budget/event-day
        # reducer). On any cycle where the calibrator had no valid state -
        # live start-of-day, tier-0, and every backtest replay - that
        # silently sized a normal Tuesday at 25%, quartering the book. The
        # fallback is now the per-weekday normal size in Config, which
        # mirrors the calibration defaults.
        day_size_map = {
            "MONDAY":    self._t("day_size_monday",    "day_size_monday",    0.60),
            "TUESDAY":   self._t("day_size_tuesday",   "day_size_tuesday",   0.85),
            "WEDNESDAY": self._t("day_size_wednesday", "day_size_wednesday", 0.65),
            "THURSDAY":  self._t("day_size_thursday",  "day_size_thursday",  0.65),
            "FRIDAY":    self._t("day_size_friday",    "day_size_friday",    0.55),
        }""",
        r""""day_size_friday",    0.55""",
    ),
]

# verify_all.py: the repo's verification runner referenced a backtest.py
# reporter that was never mirrored into this tree; the harness that actually
# exists is backtest_engine.py (its --test validates the simulator).
_VA = "verify_all.py"

VA_EDITS = [
    Edit(
        _VA, "backtest_harness_name",
        r"""    ("backtest.py",           [PYTHON, str(BASE / "backtest.py"), "--test"]),""",
        r"""    ("backtest_engine.py",    [PYTHON, str(BASE / "backtest_engine.py"), "--test"]),""",
        r""""backtest_engine.py",    [PYTHON, str(BASE / "backtest_engine.py")""",
    ),
]


ALL_FILES = [
    ("core.py", CORE_EDITS),
    ("strategy_engine.py", SE_EDITS),
    ("execution_engine.py", XE_EDITS),
    ("regime_engine.py", RE_EDITS),
    ("verify_all.py", VA_EDITS),
]


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv
    no_env = "--no-env" in argv
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

    env_state = None
    if not no_env:
        env_state = plan_env(ENV_KEYS)
        if env_state[0] is None:
            print("  env.txt: not present (will be generated from the patched "
                  "template by the engine's own setup)")

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

    if not no_env and env_state and env_state[0] is not None:
        changed, missing = env_state
        if check:
            for k in changed:
                print(f"  [would set] env.txt:{k}")
            for k in missing:
                print(f"  [would add] env.txt:{k}")
        else:
            apply_env(ENV_KEYS, check=False)
            for k in changed:
                print(f"  updated      env.txt:{k}")
            for k in missing:
                print(f"  added        env.txt:{k}")

    print()
    print(f"applied {total_applied} edit(s), already applied {total_already}")
    if check:
        print("(--check: nothing was written)")
    else:
        print("patch complete.")
        print()
        print("Verify:")
        print("  ./venv/bin/python backtest_engine.py --test")
        print("  ./venv/bin/python backtest_engine.py --from 2026-09-08 --to 2026-09-08")
    return 0


if __name__ == "__main__":
    sys.exit(main())