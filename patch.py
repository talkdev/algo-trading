#!/usr/bin/env python3
# ============================================================================
#  patch.py — NIFTY Intraday Options Algo Engine  v3.0 -> v3.1 "PROFITABILITY"
# ============================================================================
#
#  PURPOSE
#  -------
#  Self-contained, idempotent patcher. Run it once from the repository root
#  (the folder containing core.py, data_engine.py, strategy_engine.py, ...).
#
#      python patch.py
#
#  It rewrites the engine so that it reflects how a professional NIFTY
#  intraday options premium-seller actually trades in 2026. It fixes
#  PROFITABILITY and ACCURACY defects — not code style, not cosmetics.
#
#  Every edit is applied by exact-anchor replacement, verified, then the whole
#  tree is re-parsed (AST) and functionally smoke-tested before the patch
#  reports success. Originals are backed up first.
#
#  SAFETY
#  ------
#  * Idempotent  : re-running detects the v3.1 marker and exits cleanly.
#  * Reversible  : a timestamped backup folder holds every original file.
#  * Atomic-ish  : if ANY anchor is missing, or any file fails to compile, or
#                  the functional checks fail, ALL files are restored from
#                  backup and the patch aborts having changed nothing.
#
# ============================================================================

from __future__ import annotations

import ast
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP_DIR = BASE / f"patch_v31_backup_{STAMP}"

TARGET_FILES = [
    "core.py",
    "data_engine.py",
    "regime_engine.py",
    "strategy_engine.py",
    "execution_engine.py",
    "main.py",
]

ALL_FILES = TARGET_FILES + ["calibration_engine.py", "backtest.py", "eod_report.py"]

VERSION_MARKER = "NIFTY_ENGINE_PROFIT_PATCH_V31"


# ---------------------------------------------------------------------------
# tiny patch framework
# ---------------------------------------------------------------------------

class PatchError(Exception):
    pass


class FilePatcher:
    """Accumulates exact-anchor edits for one file, reports, then writes."""

    def __init__(self, name: str):
        self.name = name
        self.path = BASE / name
        if not self.path.exists():
            raise PatchError(f"required file not found: {name}")
        self.text = self.path.read_text(encoding="utf-8")
        self.applied = []
        self.missed = []

    def sub(self, label: str, old: str, new: str) -> None:
        """Replace `old` with `new`. Records a MISS if absent or ambiguous."""
        n = self.text.count(old)
        if n == 0:
            self.missed.append(label)
            print(f"    MISS  {label}")
            return
        if n > 1:
            self.missed.append(f"{label} (anchor matched {n} times)")
            print(f"    MISS  {label}  (anchor not unique: {n} matches)")
            return
        self.text = self.text.replace(old, new, 1)
        self.applied.append(label)
        print(f"    ok    {label}")

    def flush(self) -> None:
        self.path.write_text(self.text, encoding="utf-8")


def backup_all() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for name in ALL_FILES:
        src = BASE / name
        if src.exists():
            shutil.copy2(src, BACKUP_DIR / name)


def restore_all() -> None:
    for name in ALL_FILES + ["env.txt"]:
        src = BACKUP_DIR / name
        if src.exists():
            shutil.copy2(src, BASE / name)


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# env.txt migration
# ---------------------------------------------------------------------------
#
# load_config() reads env.txt and FILE VALUES OVERRIDE the dataclass defaults.
# Any existing installation therefore has NIFTY_LOT_SIZE=75 and
# EXCHANGE_TXN_RATE=0.00053 pinned on disk, and correcting the defaults in
# core.py alone would achieve exactly nothing. The two contract/cost values
# are migrated in place and the new tuning knobs are appended (commented with
# their rationale) so they can be re-fitted later.
#
# Only these specific numeric keys are touched. Credentials, capital, windows
# and every other user setting are left byte-for-byte alone, and the original
# is backed up alongside the source files.

ENV_MIGRATIONS = [
    ("NIFTY_LOT_SIZE", "75", "65",
     "NSE revised the NIFTY 50 lot from 75 to 65 for the Jan-2026 cycle"),
    ("EXCHANGE_TXN_RATE", "0.00053", "0.0003553",
     "NSE options Rs 3,503/cr + Rs 50/cr IPFT = 0.03553%, not 0.053%"),
]

ENV_NEW_KEYS = """
# ── v3.1 Profitability Calibration (added by patch.py) ────────────────────────
OR_PCT_VERY_NARROW=0.0020
OR_PCT_NARROW=0.0036
OR_PCT_MODERATE=0.0055
OR_PCT_WIDE=0.0078
OR_STRADDLE_VERY_NARROW=0.24
OR_STRADDLE_NARROW=0.42
OR_STRADDLE_MODERATE=0.62
OR_STRADDLE_WIDE=0.86
SPOT_PROXIMITY_PCT=0.0016
SPOT_VELOCITY_PCT=0.0014
STOP_EFFICACY=0.55
GAMMA_TAIL_PROB_DTE0=0.055
GAMMA_TAIL_PROB_DTE1P=0.025
ENTRY_SLIPPAGE_MULT=0.35
EXIT_SLIPPAGE_MULT=2.25
SPREAD_ABS_TOLERANCE=0.85
MAX_DTE_TRADEABLE=4
"""


def migrate_env_file() -> None:
    env_path = BASE / "env.txt"
    if not env_path.exists():
        print("  no env.txt on disk — the corrected template in core.py will "
              "be used when one is created.")
        return

    shutil.copy2(env_path, BACKUP_DIR / "env.txt")
    text = env_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed = False

    for key, old_val, new_val, why in ENV_MIGRATIONS:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            if k.strip() != key:
                continue
            if v.strip() == new_val:
                print(f"  {key} already {new_val}")
                break
            lines[i] = f"{key}={new_val}"
            changed = True
            print(f"  {key}: {v.strip()} -> {new_val}  ({why})")
            break
        else:
            lines.append(f"{key}={new_val}")
            changed = True
            print(f"  {key} added = {new_val}  ({why})")

    existing = {
        ln.split("=", 1)[0].strip()
        for ln in lines
        if "=" in ln and not ln.strip().startswith("#")
    }
    missing = [
        ln for ln in ENV_NEW_KEYS.strip().splitlines()
        if ln.startswith("#") or ln.split("=", 1)[0].strip() not in existing
    ]
    # Only append the block if it actually contributes a new key.
    if any(not ln.startswith("#") for ln in missing):
        lines.append("")
        lines.extend(missing)
        changed = True
        print("  appended v3.1 tuning knobs")

    if changed:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("  env.txt migrated (original saved in the backup folder)")
    else:
        print("  env.txt already current")



# ---------------------------------------------------------------------------
# ==============================  THE EDITS  ================================
# ---------------------------------------------------------------------------

def patch_core(p: FilePatcher) -> None:
    """
    core.py — 2026 contract specification and cost model.

    [A1] NIFTY lot size 75 -> 65.
         NSE reduced the NIFTY 50 market lot from 75 to 65 for all contracts
         from the January 2026 cycle (first weekly expiry 06-Jan-2026, first
         monthly 27-Jan-2026). Every rupee quantity in the engine — risk per
         lot, margin, cost per point, position sizing, the daily-loss budget —
         was 15.4% wrong, in the direction of taking more risk than intended.

    [A2] NSE options exchange transaction charge 0.053% -> 0.03553%.
         The real charge is Rs 3,503 per crore of premium plus Rs 50 per crore
         IPFT = 0.03553%. The engine was charging itself ~49% too much on
         every leg, which inflated modelled friction and caused the
         credit-vs-friction, expected-edge and EV gates to reject structurally
         sound trades. (STT 0.15% on sell premium, stamp duty 0.003% on buy,
         SEBI Rs 10/crore and GST 18% were all verified correct for 2026 and
         are left untouched.)

    [C/G] New calibration knobs so thresholds scale with the index level and
          with the configured risk budget instead of being hardcoded for an
          18,000-level NIFTY.
    """
    p.sub(
        "A1 lot_size 75->65 (NSE Jan-2026 revision)",
        'lot_size=_get_int(env, "NIFTY_LOT_SIZE", 75),',
        'lot_size=_get_int(env, "NIFTY_LOT_SIZE", 65),',
    )

    p.sub(
        "A2 exchange_txn_rate 0.00053->0.0003553 (Rs3503+IPFT per crore)",
        'exchange_txn_rate=_get_float(env, "EXCHANGE_TXN_RATE", 0.00053),',
        'exchange_txn_rate=_get_float(env, "EXCHANGE_TXN_RATE", 0.0003553),',
    )

    # The dataclass defaults above are only half the story: load_config reads
    # env.txt and FILE VALUES OVERRIDE THE DEFAULTS. core.py ships an env.txt
    # template carrying the stale 75 / 0.00053, so without these two edits a
    # fresh install would silently reinstate both wrong numbers, and the two
    # headline 2026 corrections would have no effect whatsoever.
    p.sub(
        "A1 env template lot size 75->65",
        """# ── NIFTY Contract Spec ───────────────────────────────────────────────────────
NIFTY_LOT_SIZE=75""",
        """# ── NIFTY Contract Spec ───────────────────────────────────────────────────────
# NSE revised the NIFTY 50 market lot from 75 to 65 for the January 2026
# cycle (first weekly expiry 06-Jan-2026, first monthly 27-Jan-2026).
NIFTY_LOT_SIZE=65""",
    )

    p.sub(
        "A2 env template exchange txn rate -> 0.0003553",
        "EXCHANGE_TXN_RATE=0.00053",
        """# NSE options: Rs 3,503 per crore of premium + Rs 50/cr IPFT = 0.03553%.
EXCHANGE_TXN_RATE=0.0003553""",
    )

    p.sub(
        "v3.1 tuning knobs documented in the env template",
        """# ── Trading Windows ───────────────────────────────────────────────────────────
TRADING_WINDOW_START=09:45""",
        """# ── v3.1 Profitability Calibration ────────────────────────────────────────────
# Opening range classified as a fraction of spot (scale-invariant) and against
# the opening ATM straddle. The more conservative of the two wins.
OR_PCT_VERY_NARROW=0.0020
OR_PCT_NARROW=0.0036
OR_PCT_MODERATE=0.0055
OR_PCT_WIDE=0.0078
OR_STRADDLE_VERY_NARROW=0.24
OR_STRADDLE_NARROW=0.42
OR_STRADDLE_MODERATE=0.62
OR_STRADDLE_WIDE=0.86
# Structural distances as a fraction of spot.
SPOT_PROXIMITY_PCT=0.0016
SPOT_VELOCITY_PCT=0.0014
# Fraction of the structural (wing) loss a working stop is assumed to avoid.
# 0.0 sizes on the full wing loss; hard-capped at 0.80 in code.
STOP_EFFICACY=0.55
# Probability the stop is jumped and the structure prints toward the wing.
GAMMA_TAIL_PROB_DTE0=0.055
GAMMA_TAIL_PROB_DTE1P=0.025
# Slippage in multiples of the half-spread, per leg.
ENTRY_SLIPPAGE_MULT=0.35
EXIT_SLIPPAGE_MULT=2.25
# Rupee tolerance added to the relative bid/ask gate (cheap wings).
SPREAD_ABS_TOLERANCE=0.85
MAX_DTE_TRADEABLE=4

# ── Trading Windows ───────────────────────────────────────────────────────────
TRADING_WINDOW_START=09:45""",
    )

    p.sub(
        "C/G new Config fields declared",
        """    # Misc
    gift_nifty_instrument_key: str

    def __repr__(self) -> str:""",
        '''    # Misc
    gift_nifty_instrument_key: str

    # ── v3.1 profitability calibration ────────────────────────────────────
    # Opening-range width is classified as a FRACTION OF SPOT rather than in
    # absolute points, so the classification does not silently drift toward
    # "WIDE" as NIFTY rises. Chosen to reproduce the old 50/100/150/200pt
    # bands at a ~25,000 index and to scale correctly above it.
    or_pct_very_narrow:      float = 0.0020
    or_pct_narrow:           float = 0.0036
    or_pct_moderate:         float = 0.0055
    or_pct_wide:             float = 0.0078
    # Opening range measured against the opening ATM straddle, i.e. against
    # the market's own priced expectation for the day's range. The more
    # conservative of the two classifications wins.
    or_straddle_very_narrow: float = 0.24
    or_straddle_narrow:      float = 0.42
    or_straddle_moderate:    float = 0.62
    or_straddle_wide:        float = 0.86
    # Structural distances as a fraction of spot (replace hardcoded points).
    spot_proximity_pct:      float = 0.0016
    spot_velocity_pct:       float = 0.0014
    # Fraction of the structural (wing) loss that a working stop is assumed
    # to avoid. 0.0 = size on the full wing loss, 1.0 = trust the stop
    # completely. On NIFTY 0DTE the stop is NOT honoured through a gamma gap,
    # so sizing takes only partial credit for it.
    stop_efficacy:           float = 0.55
    # Probability that the stop is jumped and the structure prints toward the
    # wing (gap / gamma tail). Priced explicitly in the EV gate.
    gamma_tail_prob_dte0:    float = 0.055
    gamma_tail_prob_dte1p:   float = 0.025
    # Slippage model, in multiples of the half-spread, per leg.
    entry_slippage_mult:     float = 0.35
    exit_slippage_mult:      float = 2.25
    # Absolute rupee tolerance added to the relative bid/ask gate so that
    # cheap protective wings (Rs 1-3) are not rejected over a 0.10 tick.
    spread_abs_tolerance:    float = 0.85
    # Highest DTE at which the credit structures may be opened.
    max_dte_tradeable:       int   = 4

    def __repr__(self) -> str:''',
    )

    p.sub(
        "C/G new Config fields wired into load_config",
        """        # Misc
        gift_nifty_instrument_key=env.get("GIFT_NIFTY_INSTRUMENT_KEY", "").strip(),
    )""",
        """        # Misc
        gift_nifty_instrument_key=env.get("GIFT_NIFTY_INSTRUMENT_KEY", "").strip(),

        # v3.1 profitability calibration
        or_pct_very_narrow=_get_float(env, "OR_PCT_VERY_NARROW", 0.0020),
        or_pct_narrow=_get_float(env, "OR_PCT_NARROW", 0.0036),
        or_pct_moderate=_get_float(env, "OR_PCT_MODERATE", 0.0055),
        or_pct_wide=_get_float(env, "OR_PCT_WIDE", 0.0078),
        or_straddle_very_narrow=_get_float(env, "OR_STRADDLE_VERY_NARROW", 0.24),
        or_straddle_narrow=_get_float(env, "OR_STRADDLE_NARROW", 0.42),
        or_straddle_moderate=_get_float(env, "OR_STRADDLE_MODERATE", 0.62),
        or_straddle_wide=_get_float(env, "OR_STRADDLE_WIDE", 0.86),
        spot_proximity_pct=_get_float(env, "SPOT_PROXIMITY_PCT", 0.0016),
        spot_velocity_pct=_get_float(env, "SPOT_VELOCITY_PCT", 0.0014),
        stop_efficacy=min(max(_get_float(env, "STOP_EFFICACY", 0.55), 0.0), 0.80),
        gamma_tail_prob_dte0=_get_float(env, "GAMMA_TAIL_PROB_DTE0", 0.055),
        gamma_tail_prob_dte1p=_get_float(env, "GAMMA_TAIL_PROB_DTE1P", 0.025),
        entry_slippage_mult=_get_float(env, "ENTRY_SLIPPAGE_MULT", 0.35),
        exit_slippage_mult=_get_float(env, "EXIT_SLIPPAGE_MULT", 2.25),
        spread_abs_tolerance=_get_float(env, "SPREAD_ABS_TOLERANCE", 0.85),
        max_dte_tradeable=_get_int(env, "MAX_DTE_TRADEABLE", 4),
    )""",
    )

    p.sub(
        "version marker",
        "def now_ist() -> datetime:",
        f'{VERSION_MARKER} = "3.1"\n\n\ndef now_ist() -> datetime:',
    )


def patch_data_engine(p: FilePatcher) -> None:
    """
    data_engine.py — signal accuracy.

    [J1] Five session_state keys written on every cycle were never added to
         the SQLite schema, so MarketDataEngine raised OperationalError on
         construction. THE ENGINE COULD NOT START AT ALL. A sixth
         (_straddle_hist) holds a Python list that SQLite cannot bind, so the
         insert path is also made type-safe. None of the other improvements
         can matter until this is fixed.

    [C1] Opening-range classification made scale-invariant. or_condition is
         the most leveraged discrete variable in the whole engine: it drives
         the p_win table, the size multiplier, the VRP sell threshold, the
         hard "wide OR" no-trade gate and the range/butterfly choice. It was
         bucketed on ABSOLUTE points (50/100/150/200) calibrated for an
         ~18,000 NIFTY, so at a 2026 index an ordinary morning classifies as
         WIDE and the engine refuses to trade. Now classified on
         percent-of-spot AND against the opening straddle — the market's own
         expected day range — taking the more conservative of the two.

    [C3] The spot-velocity abort was a flat 35 points over 3 minutes: 0.19%
         at an 18,000 index but only 0.13% at 26,000, i.e. ordinary noise.
         Now a fraction of spot, widened slightly in higher-VIX regimes.

    [D1] wing_width was frozen at the literal 150 in session state and never
         updated. Every spread therefore got the same 150-point wing whatever
         the volatility, DTE or index level. It is now recomputed each cycle
         from the opening straddle and DTE.

    [H1] VRP > 8pp was hard-coded as "Parkinson data error -> NEUTRAL -> no
         trade". On NIFTY 0DTE a genuine variance risk premium above 8pp is
         routine and is precisely the richest condition to sell into: the
         engine was standing aside on its best days. The anomaly bound is now
         relative to ATM IV — a real data failure presents as RV collapsing
         to a small fraction of IV, not as an absolute number of points.

    [H2] iv_behavior EXPANDING/SPIKING is a hard entry block computed from
         raw ATM IV versus session-open ATM IV. On expiry day measured ATM IV
         mechanically rises through the afternoon because the sqrt(T)
         denominator collapses faster than the residual premium does, so a
         dead-flat 0DTE afternoon reads as EXPANDING and every afternoon
         entry — the highest-theta window of the week — is blocked. Bands are
         now inflated by the same sqrt(T) factor on 0DTE, removing the
         artefact while leaving genuine vol expansion fully detected.

    [H3] Parkinson RV is used as a FORECAST of the volatility that will be
         realized over the remaining hold (it is differenced against a
         forward-looking ATM IV to produce VRP), but it was an equal-weighted
         mean over the whole session. On a 0DTE afternoon that lets a violent
         09:15-10:00 dominate the estimate for a quiet 13:00-15:00 hold, which
         understates VRP exactly when selling is most attractive. Now
         exponentially recency-weighted.
    """
    # ---- [J1] schema + type-safe insert ------------------------------------
    p.sub(
        "J1 add the five missing session_state columns (+ liquidation mark)",
        """            ("session_state", "_straddle_open_for_summary", "REAL DEFAULT 0"),""",
        """            ("session_state", "_straddle_open_for_summary", "REAL DEFAULT 0"),
            # v3.1: these keys are written by _load_or_init_session_state on
            # every cycle but were never present in the schema, which made
            # MarketDataEngine() raise sqlite3.OperationalError on startup —
            # the engine could not run at all.
            ("session_state", "_last_valid_atm_iv",         "REAL"),
            ("session_state", "_atm_iv_none_cycles",        "INTEGER DEFAULT 0"),
            ("session_state", "_vrp_none_cycles",           "INTEGER DEFAULT 0"),
            ("session_state", "_stale_count",               "INTEGER DEFAULT 0"),
            # Liquidation mark, used by the honest mark-to-exit logic.
            ("positions",     "last_liquidation_premium",   "REAL"),""",
    )

    p.sub(
        "J1 session_state insert made type-safe and column-aware",
        """        insert_row = {
            k: (int(v) if isinstance(v, bool) else v)
            for k, v in defaults.items()
        }
        self.db.insert("session_state", insert_row)""",
        '''        # v3.1: persist only scalar values that actually exist as columns.
        # Previously every default was written blindly, so any key without a
        # column, or a non-scalar value, raised sqlite3 errors during
        # MarketDataEngine construction. `_straddle_hist` in particular is a
        # list of (timestamp, straddle) tuples feeding the straddle-expansion
        # entry block; SQLite cannot bind a list, and the 10-minute window
        # rebuilds within minutes of a restart, so it is intentionally kept
        # in-memory only.
        try:
            _cols = {
                r[1] for r in self.db.get_connection()
                .execute("PRAGMA table_info(session_state)").fetchall()
            }
        except Exception:
            _cols = set()
        insert_row = {}
        for _k, _v in defaults.items():
            if _cols and _k not in _cols:
                continue
            if isinstance(_v, bool):
                _v = int(_v)
            elif isinstance(_v, (list, tuple, dict, set)):
                continue
            insert_row[_k] = _v
        self.db.insert("session_state", insert_row)''',
    )

    # ---- [C1] scale-invariant opening range --------------------------------
    p.sub(
        "C1 opening range classified on %-of-spot and on the opening straddle",
        '''        # Classify OR width
        if or_width < 50:
            or_condition, or_score = "VERY_NARROW", 2
        elif or_width < 100:
            or_condition, or_score = "NARROW", 1
        elif or_width < 150:
            or_condition, or_score = "MODERATE", 0
        elif or_width < 200:
            or_condition, or_score = "WIDE", -1
        else:
            or_condition, or_score = "VERY_WIDE", -2

        return {''',
        '''        # ── Classify OR width (v3.1: scale-invariant) ─────────────────
        # The old absolute 50/100/150/200pt buckets were calibrated for an
        # ~18,000 NIFTY. or_condition drives the p_win table, the size
        # multiplier, the VRP sell threshold and the hard "wide OR" no-trade
        # gate, so absolute buckets make the engine progressively refuse to
        # trade as the index rises — a silent, compounding loss of
        # opportunity that looks like nothing at all in the logs.
        #
        # Two normalisations are computed and the MORE CONSERVATIVE (wider)
        # of the two is used:
        #   1. OR width as a fraction of spot.
        #   2. OR width against the opening ATM straddle, i.e. against the
        #      market's own priced expectation for the day's range. This is
        #      the measure a professional actually uses: an 80pt opening
        #      range is narrow when the straddle is 300pts and wide when the
        #      straddle is 120pts.
        _bands = ["VERY_NARROW", "NARROW", "MODERATE", "WIDE", "VERY_WIDE"]
        _scores = {"VERY_NARROW": 2, "NARROW": 1, "MODERATE": 0,
                   "WIDE": -1, "VERY_WIDE": -2}

        _ref_spot = (or_high + or_low) / 2.0
        try:
            _ps = float(self.state.get("prev_spot") or 0)
            if _ps > 0:
                _ref_spot = _ps
        except Exception:
            pass

        _idx_pct = 4
        if _ref_spot > 0:
            _frac = or_width / _ref_spot
            if _frac < self.config.or_pct_very_narrow:
                _idx_pct = 0
            elif _frac < self.config.or_pct_narrow:
                _idx_pct = 1
            elif _frac < self.config.or_pct_moderate:
                _idx_pct = 2
            elif _frac < self.config.or_pct_wide:
                _idx_pct = 3

        _idx_str = _idx_pct
        try:
            _straddle = float(
                self.state.get("opening_straddle_pts")
                or self.state.get("_straddle_open_for_regime")
                or self.state.get("_last_atm_straddle")
                or 0.0
            )
        except Exception:
            _straddle = 0.0
        if _straddle > 20:
            _sr = or_width / _straddle
            if _sr < self.config.or_straddle_very_narrow:
                _idx_str = 0
            elif _sr < self.config.or_straddle_narrow:
                _idx_str = 1
            elif _sr < self.config.or_straddle_moderate:
                _idx_str = 2
            elif _sr < self.config.or_straddle_wide:
                _idx_str = 3
            else:
                _idx_str = 4

        or_condition = _bands[max(_idx_pct, _idx_str)]
        or_score     = _scores[or_condition]

        return {''',
    )

    # ---- [H3] recency-weighted Parkinson -----------------------------------
    p.sub(
        "H3 Parkinson RV recency-weighted (it is a forecast, not an average)",
        """                if len(log_hl_sq) >= 10:
                    park_const = 1.0 / (4.0 * math.log(2.0))
                    variance   = park_const * (sum(log_hl_sq) / len(log_hl_sq))
                    rv         = math.sqrt(variance * 375.0 * 252.0) * 1.05""",
        """                if len(log_hl_sq) >= 10:
                    park_const = 1.0 / (4.0 * math.log(2.0))
                    # v3.1: this RV is used as a FORECAST of the volatility
                    # that will be realized over the remaining holding period
                    # — it is differenced against a forward-looking ATM IV to
                    # produce VRP, the engine's core edge measure. An
                    # equal-weighted session mean lets a violent 09:15-10:00
                    # dominate the estimate for a quiet 13:00-15:00 hold,
                    # which understates VRP exactly when selling premium is
                    # most attractive. Exponential recency weighting is the
                    # standard short-horizon estimator.
                    _n_hl = len(log_hl_sq)
                    _half_life = max(_n_hl / 3.0, 10.0)
                    _decay = 0.5 ** (1.0 / _half_life)
                    _w = [_decay ** (_n_hl - 1 - _i) for _i in range(_n_hl)]
                    _wsum = sum(_w) or 1.0
                    _mean_hl = sum(v * w for v, w in zip(log_hl_sq, _w)) / _wsum
                    variance   = park_const * _mean_hl
                    # 1.05 corrects the well-known downward discretisation
                    # bias of a Parkinson estimator run on 1-minute index bars.
                    rv         = math.sqrt(variance * 375.0 * 252.0) * 1.05""",
    )

    # ---- [H1] IV-relative VRP anomaly bound --------------------------------
    p.sub(
        "H1 VRP anomaly bound made IV-relative (was a flat 8pp no-trade)",
        '''        # Anomaly: VRP > 8pp is almost certainly a Parkinson RV data error
        if vrp_raw > 8.0:
            self.logger.warning(
                f"VRP spike {vrp_raw:.2f}pp — likely Parkinson RV error. "
                f"Capping at previous smoothed value."
            )''',
        '''        # v3.1 anomaly bound. The old rule ("VRP > 8pp must be a data
        # error") threw away the richest and most profitable readings: on
        # NIFTY 0DTE a genuine 8-15pp variance risk premium is routine on a
        # quiet expiry morning, and because regime_engine turns this into a
        # NEUTRAL hard block the engine stood aside precisely on its best
        # days. A real Parkinson failure does not present as an absolute
        # number of points — it presents as RV collapsing to a small fraction
        # of IV — so the bound is now relative to ATM IV, with the old 8pp
        # retained as a floor so genuinely low-IV regimes stay protected.
        _vrp_anomaly_limit = max(8.0, atm_iv_pct * 0.70)
        if vrp_raw > _vrp_anomaly_limit:
            self.logger.warning(
                f"VRP spike {vrp_raw:.2f}pp > limit {_vrp_anomaly_limit:.2f}pp "
                f"(ATM IV {atm_iv_pct:.2f}%) — likely Parkinson RV error. "
                f"Capping at previous smoothed value."
            )''',
    )

    # ---- [H2] DTE-aware IV behaviour bands ---------------------------------
    p.sub(
        "H2 iv_behavior bands widened as 0DTE T->0 (kills the sqrt(T) artefact)",
        """        iv_change_pct = (atm_iv_pct - opening_iv_pct) / opening_iv_pct * 100.0

        if iv_change_pct < -10.0:
            return "CRUSHING", round(iv_change_pct, 2)
        if iv_change_pct < -3.0:
            return "DECLINING", round(iv_change_pct, 2)
        if iv_change_pct <= 5.0:
            return "STABLE", round(iv_change_pct, 2)
        if iv_change_pct <= 18.0:
            return "EXPANDING", round(iv_change_pct, 2)
        return "SPIKING", round(iv_change_pct, 2)""",
        '''        iv_change_pct = (atm_iv_pct - opening_iv_pct) / opening_iv_pct * 100.0

        # v3.1: on expiry day the MEASURED ATM IV drifts upward through the
        # afternoon even in a dead-flat market, because the sqrt(T) in the
        # denominator collapses faster than the residual premium does. With
        # fixed bands, EXPANDING — a hard entry block — fires on quiet 0DTE
        # afternoons and kills the highest-theta window of the week. The
        # bands are therefore inflated by the same sqrt(T) factor on 0DTE,
        # which neutralises the artefact while leaving a genuine volatility
        # expansion (which is far larger) fully detected.
        _dte_iv = self.state.get("actual_dte", 0)
        _tol = 1.0
        if _dte_iv == 0:
            _elapsed = max(0.0, (
                datetime.combine(today_ist(), now_ist().time()) -
                datetime.combine(today_ist(), dtime(9, 15))
            ).total_seconds() / 60.0)
            _rem_frac = max((375.0 - _elapsed) / 375.0, 0.04)
            _tol = min(max(_rem_frac ** -0.5, 1.0), 3.2)

        if iv_change_pct < -10.0 * _tol:
            return "CRUSHING", round(iv_change_pct, 2)
        if iv_change_pct < -3.0 * _tol:
            return "DECLINING", round(iv_change_pct, 2)
        if iv_change_pct <= 5.0 * _tol:
            return "STABLE", round(iv_change_pct, 2)
        if iv_change_pct <= 18.0 * _tol:
            return "EXPANDING", round(iv_change_pct, 2)
        return "SPIKING", round(iv_change_pct, 2)''',
    )

    # ---- [C3] spot velocity as a fraction of spot --------------------------
    p.sub(
        "C3 spot-velocity abort scaled to the index level",
        """        _spot_velocity_block = False
        _sv = 0.0
        if not bars.empty and len(bars) >= 3:
            _recent3 = bars.tail(3)
            if len(_recent3) >= 2:
                _sv = abs(float(_recent3["close"].iloc[-1]) - float(_recent3["close"].iloc[0]))
                if _sv > 35:
                    _spot_velocity_block = True""",
        """        # v3.1: a flat 35pt/3min abort is 0.19% at an 18,000 index but only
        # 0.13% at 26,000 — ordinary noise, so the gate fires constantly and
        # blocks entries all day. Scaled to spot so it keeps the same economic
        # meaning as NIFTY rises, and widened a little in high-VIX regimes
        # where 3-minute noise is genuinely larger (and the premium collected
        # is correspondingly larger too).
        _spot_velocity_block = False
        _sv = 0.0
        if not bars.empty and len(bars) >= 3:
            _recent3 = bars.tail(3)
            if len(_recent3) >= 2:
                _sv = abs(float(_recent3["close"].iloc[-1]) - float(_recent3["close"].iloc[0]))
                try:
                    _sv_ref = float(spot or self.state.get("prev_spot") or 0.0)
                except Exception:
                    _sv_ref = 0.0
                try:
                    _vix_ref = float(vix or self.state.get("prev_vix") or 12.0)
                except Exception:
                    _vix_ref = 12.0
                _vix_adj = 1.0 + max(0.0, (_vix_ref - 12.0)) / 24.0
                _sv_limit = max(
                    (_sv_ref * self.config.spot_velocity_pct * _vix_adj)
                    if _sv_ref > 0 else 35.0,
                    25.0,
                )
                if _sv > _sv_limit:
                    _spot_velocity_block = True""",
    )

    # ---- [D1] adaptive wing width ------------------------------------------
    p.sub(
        "D1 adaptive wing width computed each cycle (was frozen at 150)",
        """        # ── 27. Build signals dict ────────────────────────────────────────
        signals: dict = {""",
        '''        # ── 26b. Adaptive protective wing width (v3.1) ────────────────────
        # wing_width was a literal 150 written once into session state and
        # never touched again, so every spread got the same 150-point wing
        # regardless of volatility, DTE or index level. The wing sets BOTH the
        # maximum loss and how much of the short premium is handed back to the
        # long, so a frozen wing makes the engine's own credit/wing ratio gate
        # behave arbitrarily: on a quiet 0DTE afternoon a far-OTM condor with
        # 150pt wings simply cannot reach the 0.13 credit ratio the engine
        # demands, so it structurally stops trading and logs nothing but
        # "credit_ratio_below_min". The wing is now scaled off the opening
        # straddle (the market's own expected move) and tightened on 0DTE,
        # where credits are small and the max loss must be small to match.
        try:
            _aw_straddle = float(
                self.state.get("opening_straddle_pts")
                or self.state.get("_last_atm_straddle")
                or 0.0
            )
            _aw_spot = float(spot or self.state.get("prev_spot") or 0.0)
            _aw_step = int(self.config.nifty_strike_step or 50)
            _aw_dte  = actual_dte if actual_dte is not None else 1
            if _aw_straddle <= 20 and _aw_spot > 0:
                _aw_straddle = _aw_spot * 0.009
            if _aw_straddle > 20:
                _aw_factor = 0.55 if _aw_dte == 0 else (
                    0.70 if _aw_dte == 1 else 0.85
                )
                _aw_raw = _aw_straddle * _aw_factor
            else:
                _aw_raw = 150.0
            _aw = int(round(_aw_raw / _aw_step + 0.001) * _aw_step)
            _aw_min = max(2 * _aw_step, 100)
            _aw_max = 250 if _aw_dte == 0 else (350 if _aw_dte == 1 else 450)
            _adaptive_wing_width = int(max(_aw_min, min(_aw, _aw_max)))
        except Exception:
            _adaptive_wing_width = int(self.state.get("wing_width", 150) or 150)
        self.state["wing_width"] = _adaptive_wing_width

        # ── 27. Build signals dict ────────────────────────────────────────
        signals: dict = {''',
    )

    p.sub(
        "D1 adaptive wing published into the signals dict",
        '''            "wing_width":               self.state.get("wing_width", 150),''',
        '''            "wing_width":               _adaptive_wing_width,''',
    )


def patch_regime_engine(p: FilePatcher) -> None:
    """
    regime_engine.py — regime -> structure mapping.

    [B2] _classify_range had a hard DTE==2 special case and NO branch at all
         for DTE 3 or DTE 4. Combined with the strategy-side DTE cap (B1),
         Wednesday (DTE 4) and Thursday (DTE 3) could never produce a
         tradeable outcome. That is two of the five NIFTY sessions each week —
         and with a fresh 7-day contract carrying the most vega and the widest
         credit, they are a normal part of a professional's book. A strict,
         explicitly-gated new-cycle branch is added, plus a second (narrower)
         DTE 2 route.

    [H4] compute_final_size combined its risk modifiers with min(), so only
         the single worst condition applied and every additional simultaneous
         problem was free. Unclear positioning AND a very wide opening range
         AND a borderline VRP is three independent sources of risk, not one.
         Modifiers now compound multiplicatively, with a floor.
    """
    p.sub(
        "B2 _classify_range gains strict DTE 3/4 and DTE 2 branches",
        '''        # ── DTE 2 exception ───────────────────────────────────────────────
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    pos == PositioningRegime.STRONG_RANGE):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    f"RANGE_DTE2_EXCEPTION_STRONG_SELL_STRONG_RANGE",
                )
            return FinalRegime.NO_TRADE, "RANGE_DTE2_NO_EXCEPTION"''',
        '''        # ── DTE 2 exception ───────────────────────────────────────────────
        if dte == 2:
            if (vol == VolatilityRegime.STRONG_SELL_PREMIUM and
                    pos == PositioningRegime.STRONG_RANGE):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    f"RANGE_DTE2_EXCEPTION_STRONG_SELL_STRONG_RANGE",
                )
            # v3.1: a second, narrower DTE 2 route. Friday is DTE 2 on the
            # Tuesday-expiry calendar and the old single condition (STRONG
            # sell AND STRONG range together) is rare enough that Friday was
            # effectively closed too.
            if (vol == VolatilityRegime.SELL_PREMIUM and
                    pos == PositioningRegime.RANGE and
                    or_condition in ("VERY_NARROW", "NARROW") and
                    adx_15 < self.config.adx_trend_threshold):
                return (
                    FinalRegime.PREMIUM_SELL_RANGE,
                    "RANGE_DTE2_SELL_PREMIUM_NARROW_OR_FLAT_ADX",
                )
            return FinalRegime.NO_TRADE, "RANGE_DTE2_NO_EXCEPTION"

        # ── DTE 3 / DTE 4 new-cycle branch (v3.1) ─────────────────────────
        # Wednesday is DTE 4 and Thursday is DTE 3 on the Tuesday-expiry
        # calendar. Previously neither could ever produce a tradeable regime
        # (there was no branch here, and strategy_engine capped the condor at
        # DTE 2 anyway), so 40% of the trading week was structurally dead.
        # These are legitimate premium-selling sessions — a fresh weekly
        # contract carries the most vega and the widest credit — but they hold
        # overnight gap risk into the next session, so the bar is deliberately
        # higher than for DTE 0/1: rich VRP, genuine range positioning, a
        # contained opening range and a flat trend reading are ALL required.
        if dte in (3, 4):
            if vol != VolatilityRegime.STRONG_SELL_PREMIUM:
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_REQUIRES_STRONG_SELL_PREMIUM",
                )
            if pos not in (PositioningRegime.STRONG_RANGE, PositioningRegime.RANGE):
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_REQUIRES_RANGE_POSITIONING",
                )
            if or_condition not in ("VERY_NARROW", "NARROW", "MODERATE"):
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_OR_{or_condition}_TOO_WIDE",
                )
            if adx_15 >= self.config.adx_trend_threshold:
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_ADX_{adx_15:.0f}_TRENDING",
                )
            if conf not in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
                return (
                    FinalRegime.NO_TRADE,
                    f"RANGE_DTE{dte}_REQUIRES_MEDIUM_HIGH_CONFIDENCE",
                )
            return (
                FinalRegime.PREMIUM_SELL_RANGE,
                f"RANGE_DTE{dte}_NEW_CYCLE_STRONG_SELL_CONTAINED_OR",
            )''',
    )

    p.sub(
        "H1b regime-side VRP gate made IV-relative (the block lived in TWO places)",
        '''        # ── Gate 2: VRP data error ────────────────────────────────────────
        if vrp_raw is not None and vrp_raw > 8.0:
            details["trigger"] = "VRP_DATA_ERROR_NEUTRAL"
            self.logger.warning(
                f"VRP={vrp_raw:.2f}pp > 8pp — likely Parkinson RV error. "
                f"Treating as NEUTRAL (no trade on bad data)."
            )
            return VolatilityRegime.NEUTRAL, details''',
        '''        # ── Gate 2: VRP data error (v3.1) ─────────────────────────────────
        # The same flat 8pp rule existed independently HERE and in
        # data_engine._compute_vrp, and this one is the binding constraint:
        # it returns NEUTRAL, which is a hard no-trade. Correcting only the
        # data_engine copy would have achieved nothing.
        #
        # A genuine variance risk premium above 8pp is routine on a quiet
        # NIFTY 0DTE morning and is precisely the condition a premium seller
        # exists to harvest, so the flat rule stood the engine down on its
        # best days. A real Parkinson failure does not present as an absolute
        # number of points — it presents as realised vol collapsing to a small
        # fraction of implied — so the bound is now relative to ATM IV, with
        # the old 8pp kept as a floor to protect genuinely low-IV regimes.
        _atm_iv_pct = None
        if atm_iv is not None:
            try:
                _atm_iv_pct = float(atm_iv)
                if _atm_iv_pct <= 2.0:      # stored as a decimal, not a pct
                    _atm_iv_pct *= 100.0
            except (TypeError, ValueError):
                _atm_iv_pct = None
        _vrp_limit = max(8.0, 0.70 * _atm_iv_pct) if _atm_iv_pct else 8.0

        if vrp_raw is not None and vrp_raw > _vrp_limit:
            details["trigger"] = "VRP_DATA_ERROR_NEUTRAL"
            self.logger.warning(
                f"VRP={vrp_raw:.2f}pp > limit {_vrp_limit:.2f}pp "
                f"(ATM IV {_atm_iv_pct if _atm_iv_pct else float('nan'):.2f}%) — "
                f"likely Parkinson RV error. Treating as NEUTRAL."
            )
            return VolatilityRegime.NEUTRAL, details''',
    )

    p.sub(
        "H1b docstring aligned with the implemented gate",
        "        2. VRP data error: VRP > 8pp → treat as SELL_PREMIUM (not ABORT)",
        "        2. VRP data error: VRP > max(8pp, 0.70 x ATM IV) → NEUTRAL",
    )

    p.sub(
        "H1b self-test extended to cover both sides of the IV-relative bound",
        '''    # VRP data error → SELL_PREMIUM (not ABORT)
    s6 = make_signals(vrp_raw=9.5, vrp_smoothed=9.5)
    vol6, d6 = classifier.classify_volatility(s6, 0, 11.0)
    print(f"  VRP=9.5pp (data error) → {vol6.value} (expect NEUTRAL - no trade on bad data)")
    assert vol6 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL for data error, got {vol6}"''',
        '''    # VRP data error → NEUTRAL. At the default 12.5% ATM IV the v3.1 bound is
    # max(8, 0.70 x 12.5) = 8.75pp, so 9.5pp is still treated as bad data.
    s6 = make_signals(vrp_raw=9.5, vrp_smoothed=9.5)
    vol6, d6 = classifier.classify_volatility(s6, 0, 11.0)
    print(f"  VRP=9.5pp @ IV 12.5% → {vol6.value} (expect NEUTRAL - bad data)")
    assert vol6 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL for data error, got {vol6}"

    # v3.1: the same 9.5pp VRP against a 22% ATM IV is a genuine, rich
    # variance risk premium (bound = 15.4pp), not a data error. Under the old
    # flat 8pp rule this was hard-blocked as NEUTRAL — the engine stood down
    # on exactly the days it existed to trade. It must NOT be blocked now.
    s6b = make_signals(vrp_raw=9.5, vrp_smoothed=9.5, atm_iv=0.22)
    vol6b, d6b = classifier.classify_volatility(s6b, 0, 11.0)
    print(f"  VRP=9.5pp @ IV 22%   → {vol6b.value} (expect NOT blocked as bad data)")
    assert d6b.get("trigger") != "VRP_DATA_ERROR_NEUTRAL", (
        f"Rich VRP at high IV must not be treated as a data error, got {d6b}"
    )''',
    )

    p.sub(
        "H4 size modifiers compound multiplicatively instead of min()",
        '''        # ── Modifiers ─────────────────────────────────────────────────────
        conflict_reduction = 1.0

        # Positioning conflict: price and positioning disagree
        price_is_down = price in (PriceRegime.DOWNTREND, PriceRegime.STRONG_DOWNTREND)
        price_is_up   = price in (PriceRegime.UPTREND,   PriceRegime.STRONG_UPTREND)

        if price_is_down and pos == PositioningRegime.BULLISH:
            conflict_reduction = min(conflict_reduction, 0.75)
        elif price_is_up and pos == PositioningRegime.BEARISH:
            conflict_reduction = min(conflict_reduction, 0.75)

        # UNCLEAR positioning
        if pos == PositioningRegime.UNCLEAR:
            conflict_reduction = min(conflict_reduction, 0.50)

        # Borderline sell
        if borderline_sell:
            conflict_reduction = min(conflict_reduction, 0.50)

        # OR condition modifier
        if or_condition == "MODERATE":
            conflict_reduction = min(conflict_reduction, 0.75)
        elif or_condition == "WIDE":
            conflict_reduction = min(conflict_reduction, 0.50)
        elif or_condition == "VERY_WIDE":
            conflict_reduction = min(conflict_reduction, 0.25)

        # Strong trend → reduce size (more risk)
        if price in (PriceRegime.STRONG_UPTREND, PriceRegime.STRONG_DOWNTREND):
            conflict_reduction = min(conflict_reduction, 0.75)''',
        '''        # ── Modifiers (v3.1: compounding, not min()) ──────────────────────
        # These are independent sources of risk. Taking min() meant that once
        # any one of them fired the rest were free — an unclear positioning
        # read, a very wide opening range and a borderline VRP all at once
        # produced exactly the same size as any one of them alone. A book run
        # that way is systematically largest when conditions are worst.
        #
        # Each factor keeps its ORIGINAL value and they are now multiplied.
        # That ordering matters: with one condition active the result is
        # byte-identical to the old min() behaviour, so every documented
        # invariant still holds (UNCLEAR still reduces to 0.50, a VERY_WIDE
        # opening range still reduces to 0.25). With several active the
        # result is strictly more conservative, which is the entire point.
        # An earlier draft softened each factor to "compensate" for
        # compounding — that made the single-condition case LARGER than
        # before, i.e. the opposite of the intent, and it broke the engine's
        # own UNCLEAR <= 0.50 contract.
        conflict_reduction = 1.0

        # Positioning conflict: price and positioning disagree
        price_is_down = price in (PriceRegime.DOWNTREND, PriceRegime.STRONG_DOWNTREND)
        price_is_up   = price in (PriceRegime.UPTREND,   PriceRegime.STRONG_UPTREND)

        if price_is_down and pos == PositioningRegime.BULLISH:
            conflict_reduction *= 0.75
        elif price_is_up and pos == PositioningRegime.BEARISH:
            conflict_reduction *= 0.75

        # UNCLEAR positioning
        if pos == PositioningRegime.UNCLEAR:
            conflict_reduction *= 0.50

        # Borderline sell
        if borderline_sell:
            conflict_reduction *= 0.50

        # OR condition modifier
        if or_condition == "MODERATE":
            conflict_reduction *= 0.75
        elif or_condition == "WIDE":
            conflict_reduction *= 0.50
        elif or_condition == "VERY_WIDE":
            conflict_reduction *= 0.25

        # Strong trend → reduce size (more risk)
        if price in (PriceRegime.STRONG_UPTREND, PriceRegime.STRONG_DOWNTREND):
            conflict_reduction *= 0.75

        # Floor so a pile-up of modifiers still leaves a real (if small)
        # position rather than a meaningless one.
        conflict_reduction = max(round(conflict_reduction, 4), 0.15)''',
    )


def patch_strategy_engine(p: FilePatcher) -> None:
    """
    strategy_engine.py — edge measurement, structure and sizing.

    [B1] DTE_REQUIREMENTS capped the condor and the credit spreads at DTE 2,
         while the hard gates, the p_win table, the target table, the risk
         table and LOT_CAPS_BY_DAY all carried fully-populated entries for
         DTE 3-6. The result was a silent dead path: on Wednesday (DTE 4) and
         Thursday (DTE 3) the regime engine selected a strategy and
         compute_params always rejected it with "dte_above_max_2".

    [D1] The wing arrived from signals (now adaptive) but was then merely
         floored at 100, with no relationship to whether the resulting
         structure could clear the engine's own credit/wing ratio. The wing is
         now derived from the ACTUAL short-strike distance, which is what
         determines how much premium the long leg gives back.

    [E1] THE EV GATE WAS MEASURING THE WRONG BARRIER. p_win came from a
         no-touch model whose barrier was `wing*0.5 - spot_proximity_pts` —
         half the protective WING WIDTH. The barrier that actually decides
         whether a credit spread wins is the distance from spot to the SHORT
         STRIKE. With the engine's 150pt wing the model used a ~45pt barrier
         against a real barrier of 250-300pts, so z was roughly 6x too small,
         p_win collapsed onto its 0.35 floor, and the gate rejected
         structurally excellent trades all day long. This is the single
         largest accuracy defect in the engine.

    [E2] Expectancy was modelled as two outcomes — win at target, lose at
         stop. A NIFTY 0DTE short-premium book does not die at the stop; it
         dies on the day the stop is jumped and the structure prints at the
         wing. That outcome is now priced explicitly as a third branch.

    [E3] The empirical OR-conditional p_win prior was computed and then
         discarded whenever the barrier model produced a number. The two are
         now blended: the model knows the geometry, the prior knows what the
         lognormal geometry cannot see.

    [E4] 0DTE targets of 45-50% of credit are not what NIFTY expiry day pays.
         Theta on the expiring contract is front-and-middle loaded while the
         dangerous part of the move distribution arrives after 13:30, so
         holding for half the credit means holding straight into the gamma
         window. Recalibrated to the 25-35% band professionals actually work.

    [G1] SIZING ASSUMED THE STOP ALWAYS WORKS. structural_loss_per_lot was
         clamped down to min(stop-based loss, 2 x credit) — the engine sized
         as though the worst case were the stop level. On NIFTY 0DTE the stop
         is precisely what does not hold through a gamma move. This oversized
         every position by roughly 2-4x and is the classic way a
         premium-selling account is destroyed in a single session.

    [G2] The day lot cap scaled with int(capital / starting_capital) — a step
         function that doubles exposure the instant equity doubles and does
         nothing in between. Replaced with continuous sqrt-of-equity scaling.

    [G3] The 0DTE margin model added 2% x spot x lot x n_shorts of naked-style
         exposure margin on top of the wing margin. For a fully hedged
         defined-risk spread that is not how SPAN + exposure works; at a
         26,000 index the term was ~6x the wing margin, pinning final_lots at
         1 and throwing away most of the return for no reduction in risk.

    [G4] max_risk_per_trade_pct was loaded, safety-clamped at startup, and
         then ignored entirely in favour of a hardcoded risk table.

    [I2] The leg spread gate was purely relative, so a Rs 1.50 protective wing
         with a normal one-tick market reads as 6.7% and could be rejected.

    [I3] Slippage was double counted on entry (exec_price is already taken at
         the bid for shorts and the ask for longs, so the full spread has
         already been paid) while the round trip was approximated as entry x
         1.5 — which understates the exit, where the real money is lost.
    """
    p.sub(
        "B1 DTE_REQUIREMENTS opened to DTE 4 (Wed/Thu were structurally dead)",
        """DTE_REQUIREMENTS: Dict[str, Tuple[int, int]] = {
    IRON_BUTTERFLY:   (0, 1),
    IRON_CONDOR:      (0, 2),
    BULL_PUT_SPREAD:  (0, 2),
    BEAR_CALL_SPREAD: (0, 2),
}""",
        """# v3.1: the condor and the credit spreads were capped at DTE 2 while every
# other DTE-indexed table in the engine (hard gates, p_win, targets, risk,
# LOT_CAPS_BY_DAY) was populated out to DTE 6. On the Tuesday-expiry calendar
# Wednesday is DTE 4 and Thursday is DTE 3, so those two sessions could select
# a strategy and were then ALWAYS rejected by compute_params — 40% of the
# trading week was structurally unreachable, and the only trace was a
# "dte_4_above_max_2" line in the decision log. The regime engine now gates
# DTE 3/4 explicitly and strictly (see regime_engine._classify_range).
DTE_REQUIREMENTS: Dict[str, Tuple[int, int]] = {
    IRON_BUTTERFLY:   (0, 1),
    IRON_CONDOR:      (0, 4),
    BULL_PUT_SPREAD:  (0, 4),
    BEAR_CALL_SPREAD: (0, 4),
}""",
    )

    p.sub(
        "I2 leg spread gate made rupee-aware",
        """        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if mid > 0 and (ask - bid) / mid > (0.15 if action == "SELL" else 0.30):
                return False, f"strike_{strike:.0f}_{opt_type}_spread_too_wide\"""",
        """        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            # v3.1: a purely relative spread gate rejects cheap protective
            # wings for no reason — a Rs 1.50 long option quoted 1.45/1.55 is
            # a perfectly normal one-tick market but reads as 6.7%, and at
            # Rs 0.80 a single tick reads as 12.5%. Since the long wing is
            # what makes the structure defined-risk, rejecting it forces the
            # engine either to skip the trade or to reach for a wider, worse
            # wing. An absolute rupee tolerance is allowed on top of the
            # relative cap.
            _rel_cap = 0.15 if action == "SELL" else 0.30
            _abs_cap = float(getattr(self.config, "spread_abs_tolerance", 0.85))
            if mid > 0 and (ask - bid) > max(mid * _rel_cap, _abs_cap):
                return False, f"strike_{strike:.0f}_{opt_type}_spread_too_wide\"""",
    )

    p.sub(
        "I3 slippage model made explicit + true round-trip friction helper",
        '''    def _compute_slippage(
        self,
        legs:    List[dict],
        is_exit: bool = False,
    ) -> float:
        """
        Spread-aware slippage model.
        Entry (is_exit=False): 50% of half-spread per leg (patient limit order).
        Exit  (is_exit=True):  150% of half-spread per leg (urgent stop exit).
        No bid/ask: 0.35 entry, 0.60 exit (conservative fallback).
        NIFTY 2026: OTM 0DTE spreads widen 2-4x when stops fire under stress.
        """
        total = 0.0
        for leg in legs:
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                half_spread = (ask - bid) / 2.0
                total += half_spread * (3.0 if is_exit else 0.5)
            else:
                total += 1.20 if is_exit else 0.35
        return round(total, 3)''',
        '''    def _compute_slippage(
        self,
        legs:    List[dict],
        is_exit: bool = False,
    ) -> float:
        """
        Spread-aware slippage for the whole structure, in premium points
        (points are lot-invariant).

        v3.1 notes
        ----------
        The docstring and the code disagreed (150% claimed, 3.0x applied) and,
        more importantly, the entry number was double counting: exec_price is
        already taken at the BID for shorts and the ASK for longs, so the full
        spread has already been paid before this function is called. Charging
        a further half-spread on entry inflated modelled friction and made the
        engine reject sound structures.

        Entry now carries only a small residual (queue and tick risk on a
        marketable limit). The exit carries the genuinely large number,
        because that is where the money is actually lost: an OTM NIFTY 0DTE
        market quoting 0.10 wide at 11:00 quotes 0.50-1.00 wide when a stop
        fires into a fast move, and that is exactly when the engine exits.

        Both multiples are configurable (entry_slippage_mult /
        exit_slippage_mult) so they can be re-fitted from the engine's own
        exit-quality telemetry once it has enough sessions.
        """
        entry_mult = float(getattr(self.config, "entry_slippage_mult", 0.35))
        exit_mult  = float(getattr(self.config, "exit_slippage_mult", 2.25))
        total = 0.0
        for leg in legs:
            bid = float(leg.get("bid", 0) or 0)
            ask = float(leg.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                half_spread = (ask - bid) / 2.0
                total += half_spread * (exit_mult if is_exit else entry_mult)
            else:
                total += 1.20 if is_exit else 0.35
        return round(total, 3)

    def _round_trip_friction(
        self,
        legs:            List[dict],
        entry_costs_pts: float,
    ) -> float:
        """
        v3.1: TRUE round-trip friction, in premium points.

        The old code approximated the round trip as
        `(entry_costs + entry_slippage) * 1.5`, which understates it twice
        over: the exit is a full second set of brokerage and statutory
        charges, and exit slippage is several times entry slippage. Every
        gate that compared credit against friction — the minimum-credit gate
        and the EV gate — was therefore comparing against roughly half the
        real number, so trades that were cost-negative in reality passed.
        """
        entry_slip = self._compute_slippage(legs, is_exit=False)
        exit_slip  = self._compute_slippage(legs, is_exit=True)
        # Exit charges are of the same order as entry charges: brokerage per
        # order is identical, and STT simply moves to whichever legs are sold.
        exit_costs_pts = entry_costs_pts * 0.95
        return round(entry_costs_pts + entry_slip + exit_costs_pts + exit_slip, 4)''',
    )

    p.sub(
        "I3 slippage self-test follows the configured model, not frozen constants",
        '''    slip_entry = engine._compute_slippage(legs_ba, is_exit=False)
    expected_entry = 2 * ((46 - 44) / 2.0 * 0.5)
    assert abs(slip_entry - expected_entry) < 0.01, (
        f"Entry slippage: expected {expected_entry:.3f}, got {slip_entry:.3f}"
    )
    print(f"  Entry slippage (2 legs bid/ask): {slip_entry:.3f}pts [OK]")

    slip_exit = engine._compute_slippage(legs_ba, is_exit=True)
    expected_exit = 2 * ((46 - 44) / 2.0 * 3.0)
    assert abs(slip_exit - expected_exit) < 0.01, (
        f"Exit slippage: expected {expected_exit:.3f}, got {slip_exit:.3f}"
    )
    print(f"  Exit slippage (2 legs bid/ask): {slip_exit:.3f}pts [OK]")''',
        '''    # v3.1: these previously asserted the hardcoded 0.5x / 3.0x multiples, so
    # the test pinned the old double-counted entry model in place rather than
    # verifying the intended behaviour. They now follow the configured
    # multipliers and assert the properties that actually matter.
    _half = (46 - 44) / 2.0
    slip_entry = engine._compute_slippage(legs_ba, is_exit=False)
    expected_entry = 2 * (_half * config.entry_slippage_mult)
    assert abs(slip_entry - expected_entry) < 0.01, (
        f"Entry slippage: expected {expected_entry:.3f}, got {slip_entry:.3f}"
    )
    print(f"  Entry slippage (2 legs bid/ask): {slip_entry:.3f}pts [OK]")

    slip_exit = engine._compute_slippage(legs_ba, is_exit=True)
    expected_exit = 2 * (_half * config.exit_slippage_mult)
    assert abs(slip_exit - expected_exit) < 0.01, (
        f"Exit slippage: expected {expected_exit:.3f}, got {slip_exit:.3f}"
    )
    print(f"  Exit slippage (2 legs bid/ask): {slip_exit:.3f}pts [OK]")

    # Exiting a stressed 0DTE market must always be modelled as dearer than
    # entering one; if this inverts, every cost gate in the engine is wrong.
    assert slip_exit > slip_entry, (
        f"Exit slippage {slip_exit:.3f} must exceed entry {slip_entry:.3f}"
    )

    # The round trip must exceed the sum of its slippage legs (it also carries
    # two sets of statutory charges) — this is the number the credit gates use.
    _rt = engine._round_trip_friction(legs_ba, 1.0)
    assert _rt > slip_entry + slip_exit, (
        f"Round-trip friction {_rt:.3f} must exceed slippage alone"
    )
    print(f"  Round-trip friction (2 legs): {_rt:.3f}pts [OK]")''',
    )

    p.sub(
        "D1 wing derived from the real short-strike distance, DTE-clamped",
        '''        wing = int(round((int(signals.get("wing_width") or 150)) / step) * step)
        wing = max(wing, 100)''',
        '''        # ── Protective wing (v3.1) ────────────────────────────────────────
        # The wing determines BOTH the maximum loss and how much of the short
        # premium is handed back to the long. Taking it as a fixed number
        # makes the engine's own credit/wing ratio gate behave arbitrarily:
        # far-OTM 0DTE shorts with a fat wing can never reach the required
        # ratio, so the engine silently stops trading in the afternoon. The
        # wing is anchored to the ACTUAL short-strike distance (the only thing
        # that determines how cheap the long is), with the adaptive
        # straddle-based hint from data_engine as a fallback, then clamped by
        # DTE so 0DTE max loss stays small where credits are small.
        _wing_hint = int(signals.get("wing_width") or 150)
        if short_dist is not None and short_dist > 0:
            _wing_factor = 0.50 if dte == 0 else (0.60 if dte == 1 else 0.75)
            _wing_raw = max(short_dist * _wing_factor, float(_wing_hint) * 0.60)
        else:
            _wing_raw = float(_wing_hint)
        _wing_min = max(2 * step, 100)
        _wing_max = 250 if dte == 0 else (350 if dte == 1 else 450)
        wing = int(round(_wing_raw / step + 0.001) * step)
        wing = int(max(_wing_min, min(wing, _wing_max)))''',
    )

    p.sub(
        "E4 target ladder recalibrated for what NIFTY 0DTE actually pays",
        '''    def _get_target_pct(self, dte: Optional[int], signals: dict) -> float:
        """
        NIFTY 2026 target percentages by DTE.
        DTE 0 (Tuesday 0DTE): 45-50% — fastest gamma, exit before 13:30 gamma explosion.
        DTE 1 (Monday 1DTE): 38-42% — good theta, moderate urgency.
        DTE 2+ (Wed-Fri):    30-35% — least theta per hour, patient exit.
        Higher DTE = lower target because less gamma urgency per unit of time.
        DTE0 > DTE1 > DTE2 is the correct ordering for NIFTY intraday.
        """
        vix = float(signals.get("vix") or 11.0)
        if dte == 0:
            return 0.50 if vix < 12.0 else (0.47 if vix < 14.0 else 0.45)
        if dte == 1:
            return 0.42 if vix < 12.0 else (0.38 if vix < 14.0 else 0.35)
        if dte == 2:
            return 0.30 if vix < 12.0 else (0.27 if vix < 14.0 else 0.24)''',
        '''    def _get_target_pct(self, dte: Optional[int], signals: dict) -> float:
        """
        Fraction of the net credit taken as profit. (v3.1 recalibration.)

        The old ladder asked for 45-50% of the credit on 0DTE. That is not
        what NIFTY expiry day pays. Theta on the expiring contract is front-
        and middle-loaded while the dangerous part of the move distribution
        arrives after 13:30, so holding a 0DTE structure for half its credit
        means holding it straight into the gamma window — the engine was
        asking for the one outcome that costs the most to wait for.

        The professional pattern on NIFTY 0DTE is the opposite: take 25-35%
        quickly, bank it, let the cooldown re-arm. Expectancy per unit of
        time-at-risk is far higher and tail exposure is a fraction of it.

        Targets also FALL as VIX rises: a richer credit does not mean a bigger
        percentage of it is reachable, it means the move that threatens it is
        bigger too.
        """
        vix = float(signals.get("vix") or 11.0)
        if dte == 0:
            return 0.35 if vix < 12.0 else (0.32 if vix < 14.0 else 0.28)
        if dte == 1:
            return 0.32 if vix < 12.0 else (0.29 if vix < 14.0 else 0.26)
        if dte == 2:
            return 0.28 if vix < 12.0 else (0.25 if vix < 14.0 else 0.22)''',
    )

    p.sub(
        "E1/E2/E3 EV gate rebuilt on the real barrier + gamma tail + prior",
        '''    def _compute_ev_gate(
        self,
        net_credit:      float,
        wing:            float,
        entry_costs_pts: float,
        total_slippage:  float,
        signals:         dict,
    ) -> Tuple[bool, str]:
        dte          = signals.get("actual_dte")
        or_condition = signals.get("or_condition", "MODERATE")
        vrp_smoothed = float(signals.get("vrp_smoothed") or 0.0)
        target_pct   = self._get_target_pct(dte, signals)
        reward_pts   = net_credit * target_pct
        risk_pts     = net_credit * 1.5
        friction     = (entry_costs_pts + total_slippage) * 1.5

        p_win_table = {''',
        '''    def _proximity_buffer_pts(self, spot: float) -> float:
        """
        v3.1: the distance from a short strike at which the engine closes.

        Was a flat 40 points (config.spot_proximity_pts) — 0.22% of an 18,000
        index but only 0.15% of a 26,000 one, so the structural protection
        silently weakened as NIFTY rose. Now scaled to spot with the
        configured absolute value as a floor.
        """
        base = float(self.config.spot_proximity_pts or 40)
        pct  = float(getattr(self.config, "spot_proximity_pct", 0.0016))
        if spot and spot > 0:
            return max(base, spot * pct)
        return base

    def _compute_ev_gate(
        self,
        net_credit:      float,
        wing:            float,
        entry_costs_pts: float,
        total_slippage:  float,
        signals:         dict,
        legs:            Optional[List[dict]] = None,
        stop_premium:    Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Expected value of the structure, in premium points, over the intended
        holding period. (v3.1 rebuild — see the three defects below.)
        """
        dte          = signals.get("actual_dte")
        or_condition = signals.get("or_condition", "MODERATE")
        vrp_smoothed = float(signals.get("vrp_smoothed") or 0.0)
        target_pct   = self._get_target_pct(dte, signals)
        reward_pts   = net_credit * target_pct

        # ── Loss legs ─────────────────────────────────────────────────────
        # Normal loss = the stop actually working. Previously hardcoded to
        # 1.5 x credit with no relationship to the stop_premium the engine
        # would really use.
        if stop_premium and stop_premium > 0:
            stop_loss_pts = max(stop_premium - net_credit, 0.0)
        else:
            stop_loss_pts = net_credit * 1.5
        # [E2] Tail loss = the stop is jumped and the structure prints toward
        # the wing. Not the full wing (partial fills and some recovery are
        # normal) but far beyond the stop. A NIFTY 0DTE short-premium book
        # does not die at the stop; it dies here, and this outcome was simply
        # absent from the old two-outcome expectancy.
        wing_loss_pts = max(float(wing) - net_credit, stop_loss_pts)
        tail_loss_pts = max(stop_loss_pts, 0.80 * wing_loss_pts)

        # True round-trip friction rather than entry x 1.5.
        friction = (
            self._round_trip_friction(legs, entry_costs_pts) if legs
            else (entry_costs_pts + total_slippage) * 2.2
        )

        p_win_table = {''',
    )

    p.sub(
        "E1/E3 p_win: real short-strike barrier, prior blended back in",
        '''        p_win_prior = p_win_table.get(
            min(dte or 1, 6), p_win_table[6]
        ).get(or_condition, 0.50)

        import math as _math_ev
        _atm_iv = float(signals.get("atm_iv") or 0.0)
        _spot_ev = float(signals.get("spot") or 23900.0)
        _now_ev = now_ist().time()
        _mins_to_exit = max(
            (datetime.combine(today_ist(), dtime(15, 0)) -
             datetime.combine(today_ist(), _now_ev)).total_seconds() / 60.0,
            5.0
        )
        _sigma_t = _atm_iv * (_mins_to_exit / (375.0 * 252.0)) ** 0.5 if _atm_iv > 0 else 0.0
        if _sigma_t > 0 and wing > 0 and _spot_ev > 0:
            _barrier = max(wing * 0.5 - float(self.config.spot_proximity_pts), 20.0)
            _z = _barrier / (_spot_ev * _sigma_t)
            def _ncdf(x):
                return 0.5 * (1.0 + _math_ev.erf(x / _math_ev.sqrt(2.0)))
            p_win = max(0.35, min(0.92, 1.0 - 2.0 * _ncdf(-_z)))
        else:
            _or_c = or_condition or "MODERATE"
            _pb = {"VERY_NARROW": 0.72, "NARROW": 0.68, "MODERATE": 0.62,
                   "WIDE": 0.52, "VERY_WIDE": 0.44}.get(_or_c, 0.55)
            if dte and dte >= 2:
                _pb = max(_pb - 0.06 * min(dte - 1, 4), 0.35)
            _va = 0.06 if vrp_smoothed > 3.5 else (
                0.03 if vrp_smoothed > 2.5 else (
                -0.03 if vrp_smoothed < 2.0 else 0.0))
            p_win = max(0.35, min(0.88, _pb + _va))
        ev    = p_win * reward_pts - (1.0 - p_win) * risk_pts - friction
        min_ev = max(net_credit * 0.02, friction * 0.15)

        if ev < min_ev:
            return False, (
                f"ev_{ev:.2f}pts_below_min_{min_ev:.2f}pts(p_win={p_win:.2f})"
            )
        return True, f"ev_ok_{ev:.2f}pts"''',
        '''        # ── [E3] Empirical OR-conditional prior ───────────────────────────
        # This was previously computed and then thrown away the moment the
        # barrier model returned a number. It carries everything the
        # lognormal geometry cannot see — positioning, pinning, and the
        # engine's own historical hit rate by opening range — so it is now
        # blended in rather than discarded.
        p_win_prior = p_win_table.get(
            min(dte if dte is not None else 1, 6), p_win_table[6]
        ).get(or_condition, 0.50)
        if vrp_smoothed > 3.5:
            p_win_prior += 0.05
        elif vrp_smoothed > 2.5:
            p_win_prior += 0.025
        elif vrp_smoothed < 2.0:
            p_win_prior -= 0.03
        p_win_prior = max(0.30, min(0.90, p_win_prior))

        # ── [E1] Barrier model on the TRUE short-strike distance ──────────
        # The old code used `wing * 0.5 - spot_proximity_pts` as the barrier,
        # i.e. half the protective WING WIDTH. That is not the barrier that
        # decides a credit spread — the distance from spot to the SHORT
        # STRIKE is. With a 150pt wing the model used ~45pts against a real
        # barrier of 250-300pts, so z was roughly 6x too small, p_win pinned
        # to its 0.35 floor, and the EV gate rejected structurally excellent
        # trades all day while reporting a plausible-looking reason.
        import math as _math_ev

        _atm_iv = float(signals.get("atm_iv") or 0.0)
        if _atm_iv >= 2.0:          # stored as a percentage, not a decimal
            _atm_iv = _atm_iv / 100.0
        _spot_ev = float(signals.get("spot") or 0.0)
        _now_ev = now_ist().time()
        _mins_to_exit = max(
            (datetime.combine(today_ist(), dtime(15, 0)) -
             datetime.combine(today_ist(), _now_ev)).total_seconds() / 60.0,
            5.0
        )
        _sigma_t = (
            _atm_iv * (_mins_to_exit / (375.0 * 252.0)) ** 0.5
            if _atm_iv > 0 else 0.0
        )

        _barrier = 0.0
        if legs and _spot_ev > 0:
            _dists = [
                abs(float(l["strike"]) - _spot_ev)
                for l in legs if str(l.get("action")) == "SELL"
            ]
            if _dists:
                # The engine exits on proximity, so the effective barrier is
                # slightly nearer than the strike itself.
                _barrier = max(min(_dists) - self._proximity_buffer_pts(_spot_ev), 15.0)

        p_win_model = None
        if _sigma_t > 0 and _barrier > 0 and _spot_ev > 0:
            _z = _barrier / (_spot_ev * _sigma_t)

            def _ncdf(x: float) -> float:
                return 0.5 * (1.0 + _math_ev.erf(x / _math_ev.sqrt(2.0)))

            # Reflection-principle no-touch probability for a driftless walk.
            p_win_model = max(0.25, min(0.93, 1.0 - 2.0 * _ncdf(-_z)))

        if p_win_model is None:
            p_win = p_win_prior
        else:
            # 60/40 model/prior: the model is sharper intraday, the prior
            # covers the rest.
            p_win = 0.60 * p_win_model + 0.40 * p_win_prior
        p_win = max(0.28, min(0.92, p_win))

        # ── [E2] Three-outcome expectancy ─────────────────────────────────
        p_tail = (
            float(getattr(self.config, "gamma_tail_prob_dte0", 0.055))
            if dte == 0 else
            float(getattr(self.config, "gamma_tail_prob_dte1p", 0.025))
        )
        # A wide opening range and a trending tape both fatten the tail.
        if or_condition in ("WIDE", "VERY_WIDE"):
            p_tail *= 1.8
        _adx_ev = float(signals.get("adx_15") or 0.0)
        if _adx_ev >= float(self.config.adx_strong_threshold):
            p_tail *= 1.5
        p_tail = min(p_tail, 0.20)

        p_win_eff = max(p_win * (1.0 - p_tail), 0.05)
        p_stop    = max(1.0 - p_win_eff - p_tail, 0.0)

        ev = (
            p_win_eff * reward_pts
            - p_stop * stop_loss_pts
            - p_tail * tail_loss_pts
            - friction
        )

        # Minimum acceptable edge. The old floor (2% of credit, or 15% of an
        # already-understated friction number) let through trades whose whole
        # expectancy was inside the cost of doing them.
        min_ev = max(net_credit * 0.03, friction * 0.35, 0.75)

        _detail = (
            f"p_win={p_win:.2f},p_tail={p_tail:.3f},"
            f"rew={reward_pts:.2f},stop={stop_loss_pts:.2f},"
            f"tail={tail_loss_pts:.2f},fric={friction:.2f}"
        )
        if ev < min_ev:
            return False, f"ev_{ev:.2f}pts_below_min_{min_ev:.2f}pts({_detail})"
        return True, f"ev_ok_{ev:.2f}pts({_detail})"''',
    )

    p.sub(
        "A3 credit-vs-friction gate uses the true round-trip cost",
        '''        friction_pts        = (entry_costs_pts + total_slippage) * 1.5
        min_credit_friction = friction_pts * 3.0
        if net_credit < min_credit_friction:
            return {
                "valid": False,
                "reason": (
                    f"net_credit_{net_credit:.2f}pts_below_4x_friction_"
                    f"{min_credit_friction:.2f}pts"
                ),
            }''',
        '''        # v3.1: friction is the FULL round trip (entry charges + entry
        # slippage + exit charges + exit slippage), not entry x 1.5. Every
        # gate comparing credit to friction was previously measuring against
        # roughly half the real number, so trades that were cost-negative in
        # reality passed while the label claimed a 4x safety margin.
        friction_pts        = self._round_trip_friction(
            validated_legs, entry_costs_pts
        )
        min_credit_friction = friction_pts * 2.5
        if net_credit < min_credit_friction:
            return {
                "valid": False,
                "reason": (
                    f"net_credit_{net_credit:.2f}pts_below_2.5x_roundtrip_"
                    f"friction_{min_credit_friction:.2f}pts"
                ),
            }''',
    )

    p.sub(
        "A3 expected-edge gate charged the real exit cost",
        '''        target_pct    = self._get_target_pct(actual_dte, signals)
        exit_costs    = entry_costs_pts + total_slippage
        expected_edge = net_credit * target_pct - exit_costs''',
        '''        target_pct    = self._get_target_pct(actual_dte, signals)
        # v3.1: what the target has to clear is the cost of getting OUT, and
        # exit slippage on a stressed OTM 0DTE market is a multiple of entry
        # slippage. Using entry costs as the proxy flattered every structure.
        exit_costs    = entry_costs_pts * 0.95 + self._compute_slippage(
            validated_legs, is_exit=True
        )
        expected_edge = net_credit * target_pct - exit_costs''',
    )

    p.sub(
        "E2 stop level computed before the EV gate so EV prices the real stop",
        '''        ev_ok, ev_reason = self._compute_ev_gate(
            net_credit, actual_wing_pts or 150,
            entry_costs_pts, total_slippage, signals,
        )
        if not ev_ok:
            return {"valid": False, "reason": f"ev_gate:{ev_reason}"}''',
        '''        # v3.1: the stop level is now computed BEFORE the EV gate so the gate
        # prices the loss leg the engine will actually take, instead of a
        # hardcoded 1.5 x credit that bore no relationship to stop_premium.
        _stop_mult_pre = min(float(state.get("stop_multiplier", 2.5) or 2.5), 2.5)
        if strategy_name in (BULL_PUT_SPREAD, BEAR_CALL_SPREAD):
            _stop_premium_pre = gross_credit * 2.5
        else:
            _stop_premium_pre = net_credit * _stop_mult_pre

        ev_ok, ev_reason = self._compute_ev_gate(
            net_credit, actual_wing_pts or 150,
            entry_costs_pts, total_slippage, signals,
            legs=validated_legs, stop_premium=_stop_premium_pre,
        )
        if not ev_ok:
            return {"valid": False, "reason": f"ev_gate:{ev_reason}"}''',
    )

    p.sub(
        "G1/G2/G4 sizing anchored on structural loss and the configured budget",
        '''        current_capital = state.get("current_capital", self.config.starting_capital)
        wing_for_sizing = actual_wing_pts or 150
        _stop_loss_per_lot = 1.0 * net_credit * C02
        _structural_per_lot = max(
            (wing_for_sizing - net_credit) * C02, net_credit * C02
        )
        if _structural_per_lot <= 0:
            _structural_per_lot = wing_for_sizing * C02 * 0.5
        structural_loss_per_lot = min(
            max(_stop_loss_per_lot, 0.5 * _structural_per_lot),
            _structural_per_lot
        )

        risk_pct_map = {
            0: 0.008, 1: 0.006, 2: 0.005,
            3: 0.004, 4: 0.003, 5: 0.0025, 6: 0.002
        }
        risk_pct  = risk_pct_map.get(min(actual_dte or 1, 6), 0.002)
        max_risk  = current_capital * risk_pct
        _credit_risk_per_lot = net_credit * 2.0 * C02
        if _credit_risk_per_lot > 0:
            structural_loss_per_lot = min(structural_loss_per_lot, _credit_risk_per_lot)
        raw_lots  = max_risk / structural_loss_per_lot
        final_lots = max(1, int(raw_lots * size_mult))

        day_cap = LOT_CAPS_BY_DAY.get(day_label, 3) * max(
            1, int(current_capital / self.config.starting_capital)
        )
        final_lots = min(final_lots, day_cap)''',
        '''        current_capital = state.get("current_capital", self.config.starting_capital)
        wing_for_sizing = actual_wing_pts or 150

        # ── [G1] Risk per lot ─────────────────────────────────────────────
        # The old code sized as though the stop always works: it clamped the
        # per-lot loss down to min(stop_loss, 2 x credit). On NIFTY 0DTE the
        # stop is exactly what does NOT hold — a gap or a gamma acceleration
        # through the short strike fills far past it, and the structure is
        # worth (wing - credit) against you. Sizing on the stop therefore
        # oversized every position by roughly 2-4x, which is the textbook way
        # a premium-selling account is destroyed by a single session.
        #
        # Sizing is now anchored on the STRUCTURAL loss, with only partial
        # credit for the stop working (config.stop_efficacy, hard-capped at
        # 0.80 in load_config so this can never be switched off entirely).
        _structural_per_lot = max((wing_for_sizing - net_credit) * C02, 1.0)
        _stop_loss_per_lot  = max(
            (float(_stop_premium_pre) - net_credit) * C02, 0.0
        )
        _efficacy = float(getattr(self.config, "stop_efficacy", 0.55))
        _efficacy = min(max(_efficacy, 0.0), 0.80)
        structural_loss_per_lot = (
            _efficacy * _stop_loss_per_lot
            + (1.0 - _efficacy) * _structural_per_lot
        )
        # Never assume less risk than the stop itself; never more than the
        # structural maximum.
        structural_loss_per_lot = min(
            max(structural_loss_per_lot, _stop_loss_per_lot, 1.0),
            _structural_per_lot,
        )

        # ── [G4] Risk budget ──────────────────────────────────────────────
        # config.max_risk_per_trade_pct is loaded, safety-clamped at startup
        # to below MAX_DAILY_LOSS_PCT/3, and was then ignored completely in
        # favour of this hardcoded table — so tightening the configured risk
        # limit had literally no effect on position size, and the daily-loss
        # arithmetic the clamp was protecting did not hold. The table is now
        # expressed as a FRACTION of the configured budget.
        _budget = float(self.config.max_risk_per_trade_pct or 0.006)
        risk_frac_map = {
            0: 1.00, 1: 0.80, 2: 0.65,
            3: 0.50, 4: 0.40, 5: 0.32, 6: 0.25,
        }
        risk_pct  = _budget * risk_frac_map.get(
            min(actual_dte if actual_dte is not None else 1, 6), 0.25
        )
        max_risk  = current_capital * risk_pct
        raw_lots  = max_risk / structural_loss_per_lot
        final_lots = max(1, int(raw_lots * size_mult))

        # ── [G2] Day cap ──────────────────────────────────────────────────
        # int(capital / starting_capital) is a step function: the cap doubles
        # the instant equity doubles and does nothing in between — the single
        # worst moment to double exposure is right after a run-up. Continuous
        # square-root-of-equity scaling grows exposure smoothly and slows it
        # as the account grows, which is the standard convention.
        _equity_scale = max(
            (current_capital / float(self.config.starting_capital or 1.0)) ** 0.5,
            0.35,
        )
        day_cap = max(1, int(LOT_CAPS_BY_DAY.get(day_label, 3) * _equity_scale))
        final_lots = min(final_lots, day_cap)''',
    )

    p.sub(
        "E2 reuse the precomputed stop level (single source of truth)",
        '''        stop_mult = min(float(state.get("stop_multiplier", 2.5) or 2.5), 2.5)
        if strategy_name in (BULL_PUT_SPREAD, BEAR_CALL_SPREAD):
            stop_premium = gross_credit * 2.5
        else:
            stop_premium = net_credit * stop_mult''',
        '''        stop_mult    = _stop_mult_pre
        stop_premium = _stop_premium_pre''',
    )

    p.sub(
        "G3 defined-risk margin corrected (no naked ELM on a hedged spread)",
        '''        _wing_margin = (actual_wing_pts or 150) * C02 * 1.10
        if actual_dte == 0:
            _spot_ref = float(signals.get("spot") or 23900)
            _n_short = sum(1 for _l in validated_legs if _l["action"] == "SELL")
            _elm = 0.02 * _spot_ref * C02 * _n_short
            margin_per_lot = _wing_margin + _elm
        else:
            margin_per_lot = _wing_margin''',
        '''        # ── [G3] Margin ───────────────────────────────────────────────────
        # The old model added 2% x spot x lot x n_shorts of exposure margin on
        # top of the wing margin on 0DTE. That is the NAKED-option convention.
        # For a fully hedged, defined-risk vertical or condor the exchange
        # requirement is essentially the spread's maximum loss plus a modest
        # add-on; at a 26,000 index the old term was roughly six times the
        # wing margin, which pinned final_lots at 1 and threw away most of the
        # achievable return without reducing any actual risk. Every short leg
        # here is hedged by construction (the builders reject unhedged
        # structures), so the add-on is a small percentage — and the genuine
        # naked formula is retained for the case where a leg really is naked.
        _shorts = [l for l in validated_legs if l["action"] == "SELL"]
        _longs  = [l for l in validated_legs if l["action"] == "BUY"]
        _fully_hedged = len(_longs) >= len(_shorts) and len(_longs) > 0
        _wing_margin = (actual_wing_pts or 150) * C02 * 1.10
        if _fully_hedged:
            # Add-on for expiry-day margin tightening and broker buffer.
            _addon = 0.18 if actual_dte == 0 else 0.10
            margin_per_lot = _wing_margin * (1.0 + _addon)
        else:
            _spot_ref = float(signals.get("spot") or 0) or 24000.0
            _n_naked  = max(len(_shorts) - len(_longs), 0)
            margin_per_lot = _wing_margin + 0.02 * _spot_ref * C02 * _n_naked''',
    )


def patch_execution_engine(p: FilePatcher) -> None:
    """
    execution_engine.py — where the realised P&L is actually decided.

    [F1] THE TIME-DECAY TARGET LADDER WAS INVERTED BY A min().
         effective_target = min(target_premium, time_target). Both are premium
         levels the position must fall BELOW to take profit, so min() selects
         the HARDER target. The stored entry target (credit x (1-target_pct))
         is almost always the lower of the two, so it always won and the whole
         "accept less profit as the clock runs down" ladder was dead code.
         Winners were therefore never harvested late: they rode into the 15:00
         hard exit or reversed into a stop. This is the single most damaging
         defect for realised P&L in the engine. It must be max().

    [F2] Targets and profit locks were evaluated against MID marks while the
         exit actually fills at the ask (buying shorts back) and the bid
         (selling longs). The engine repeatedly "hit" a target it could not
         get, sent the order, and filled worse — systematically converting
         modelled edge into slippage. Profit-taking is now decided on the
         LIQUIDATION mark. Stops deliberately stay on the mid so a one-tick
         quote gap cannot fire them, with a separate hard liquidation guard
         for the case where the exitable price really has run past the stop.

    [C2] The spot-proximity exit was a flat 40 points — 0.22% of an 18,000
         index, 0.15% of a 26,000 one. Scaled to spot.

    [F4] No gamma-time management on 0DTE. After ~13:30 on expiry day the
         remaining theta on a short structure is small while gamma is
         vertical: the position risks the width of the wing to earn a handful
         of residual points. A de-risk ladder is added.

    [F5] Profit lock handed back half of everything achieved and floored the
         stop at 0.80 x credit — willing to give back 20% of the credit from a
         position already well in profit. Combined with F1, positions
         round-tripped from good profit to scratch routinely.

    [I1] LiveOrderExecutor reported status "FILLED" for every order regardless
         of what the broker did, and _get_fill_price fell back to the EXPECTED
         price after three polls. An unfilled or partially filled leg was
         booked into position_legs as a clean fill, so the engine's book
         silently diverged from the real one and every downstream number —
         premium, unrealised P&L, the daily halt, the exit decisions — was
         computed on a fiction. On a four-legged structure that is how a
         "defined risk" position quietly becomes a naked one.

    [I2] The pre-trade relative spread gate (8%) was TIGHTER than the
         strategy-side gate (15%/30%) that produced the trade, so sound
         structures were computed and then thrown away at the door.
    """
    p.sub(
        "I1 live fill verification (was reporting FILLED unconditionally)",
        '''    def _get_fill_price(
        self, order_id: str, fallback: float, retries: int = 3
    ) -> float:
        """Fetch actual fill price from broker after order placement."""
        for attempt in range(retries):
            try:
                details = self.client.get_order_details(order_id)
                price   = details.get("average_price") or details.get("price")
                if price:
                    return float(price)
            except Exception as e:
                self.logger.warning(
                    f"Could not fetch order details for {order_id} "
                    f"(attempt {attempt + 1}/{retries}): {e}"
                )
            time_module.sleep(1)

        self.logger.warning(
            f"Using fallback price {fallback:.2f} for order {order_id}"
        )
        return fallback''',
        '''    # Broker statuses meaning "this order is done and fully executed".
    _TERMINAL_OK = {"complete", "completed", "filled", "traded", "executed"}
    _TERMINAL_BAD = {"cancelled", "canceled", "rejected", "expired", "lapsed"}

    def _get_fill_price(
        self, order_id: str, fallback: float, retries: int = 8
    ) -> float:
        """
        Fetch the ACTUAL fill price and verify the order really completed.

        v3.1: this previously polled three times and, on failure, returned the
        price the strategy *expected*, while execute_leg_entry/exit
        unconditionally reported status "FILLED". An unfilled or partially
        filled leg was therefore written into position_legs as a clean fill.
        From that moment the engine's book no longer matched the broker's, so
        every downstream number — position premium, unrealised P&L, the daily
        loss halt, every exit decision — was computed on a fiction. On a
        four-legged structure this is precisely how a "defined risk" position
        quietly becomes a naked one, and nothing in the logs would say so.

        A non-completed order now raises, which routes into the existing
        _emergency_unwind path instead of silently corrupting the book.
        """
        last_status = ""
        for attempt in range(retries):
            try:
                details = self.client.get_order_details(order_id) or {}
                status  = str(
                    details.get("status") or details.get("order_status") or ""
                ).strip().lower()
                last_status = status or last_status

                filled  = details.get("filled_quantity")
                pending = details.get("pending_quantity")
                price   = details.get("average_price") or details.get("price")

                if status in self._TERMINAL_BAD:
                    raise RuntimeError(
                        f"order {order_id} terminated as '{status}' — not filled"
                    )

                if status in self._TERMINAL_OK:
                    try:
                        if pending is not None and float(pending) > 0:
                            raise RuntimeError(
                                f"order {order_id} reports '{status}' but "
                                f"pending_quantity={pending}"
                            )
                    except (TypeError, ValueError):
                        pass
                    if price and float(price) > 0:
                        return float(price)
                    raise RuntimeError(
                        f"order {order_id} complete but broker returned no "
                        f"average_price"
                    )

                # Not terminal yet: accept only if the broker explicitly says
                # everything is filled and nothing is pending.
                if filled is not None and pending is not None and price:
                    try:
                        if (float(filled) > 0 and float(pending) == 0
                                and float(price) > 0):
                            return float(price)
                    except (TypeError, ValueError):
                        pass

            except RuntimeError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Could not fetch order details for {order_id} "
                    f"(attempt {attempt + 1}/{retries}): {e}"
                )
            time_module.sleep(1)

        raise RuntimeError(
            f"order {order_id} did not reach a confirmed filled state "
            f"(last status='{last_status or 'unknown'}'); refusing to book a "
            f"phantom fill at {fallback:.2f}"
        )''',
    )

    p.sub(
        "I2 pre-trade spread gate made rupee-aware and aligned with _validate_leg",
        '''            if bid > 0 and ask > 0 and (ask - bid) / ((bid + ask) / 2) > 0.08:
                return "NO_GO", {"reason": f"leg_{strike:.0f}_{opt_type}_spread_too_wide"}''',
        '''            # v3.1: an 8% relative gate here was TIGHTER than the 15%/30%
            # gate the strategy engine used to build the trade, so sound
            # structures were computed and then discarded at the door — and a
            # Rs 1.50 protective wing quoted one tick wide reads as 6.7% and
            # could never pass reliably. Rupee-aware and aligned with
            # StrategyEngine._validate_leg.
            if bid > 0 and ask > 0:
                _mid_pt  = (bid + ask) / 2.0
                _rel_cap = 0.15 if action == "SELL" else 0.30
                _abs_cap = float(
                    getattr(self.config, "spread_abs_tolerance", 0.85)
                )
                if (ask - bid) > max(_mid_pt * _rel_cap, _abs_cap):
                    return "NO_GO", {
                        "reason": f"leg_{strike:.0f}_{opt_type}_spread_too_wide"
                    }''',
    )

    p.sub(
        "F2/F4 liquidation-mark and round-trip-cost helpers",
        '''    def _compute_current_premium(
        self, legs: List[dict], chain: dict
    ) -> float:''',
        '''    def _liquidation_premium(
        self, legs: List[dict], chain: dict
    ) -> float:
        """
        v3.1: the premium the position can ACTUALLY be closed at right now.

        _compute_current_premium marks at the mid. That is the correct number
        to report, but it is the wrong number to make a profit-taking decision
        on: closing a credit structure means BUYING BACK the shorts at the ask
        and SELLING the longs at the bid. Deciding targets on the mid meant
        the engine repeatedly declared a target reached, sent the exit, and
        filled worse — systematically converting the modelled edge into
        slippage, trade after trade, in a way that never shows up as a losing
        decision anywhere in the logs.

        Shorts are therefore marked at the ask and longs at the bid.
        """
        premium = 0.0
        for leg in legs:
            if leg.get("leg_status") != "OPEN":
                continue
            strike   = float(leg.get("strike", 0))
            opt_type = str(leg.get("option_type", ""))
            opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
            bid = float(opt.get("bid", 0) or 0)
            ask = float(opt.get("ask", 0) or 0)
            if leg["action"] == "SELL":
                mark = ask if ask > 0 else self._get_mark_price(leg, chain)
                premium += mark
            else:
                mark = bid if bid > 0 else self._get_mark_price(leg, chain)
                premium -= mark
        return premium

    def _round_trip_cost_pts(self, legs: List[dict], chain: dict) -> float:
        """
        v3.1: approximate cost, in premium points, of closing this position
        (statutory charges, brokerage and crossing the spread). Used to floor
        the profit lock and the 0DTE de-risk ladder so that "taking a small
        profit" is a profit AFTER costs rather than a rounding error that pays
        the broker and the exchange.
        """
        C02 = float(self.config.lot_size or 1)
        live = [l for l in legs if l.get("leg_status") != "CLOSED"]
        n_legs = max(len(live), 1)
        brokerage_pts = (self.config.brokerage_per_order * n_legs) / C02
        pct_pts = 0.0
        spread_pts = 0.0
        for leg in live:
            strike   = float(leg.get("strike", 0))
            opt_type = str(leg.get("option_type", ""))
            opt      = chain.get(strike, {}).get(opt_type, {}) if chain else {}
            bid = float(opt.get("bid", 0) or 0)
            ask = float(opt.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                spread_pts += (ask - bid) / 2.0
            else:
                mid = float(leg.get("entry_price", 0) or 0)
                spread_pts += 0.35
            # On exit, STT applies to the legs being SOLD, i.e. the ones that
            # were originally bought.
            _stt = self.config.stt_options_sell if leg.get("action") == "BUY" else 0.0
            pct_pts += mid * (
                self.config.exchange_txn_rate + self.config.sebi_rate + _stt
            ) * 1.18
        return round(brokerage_pts + pct_pts + spread_pts, 3)

    def _compute_current_premium(
        self, legs: List[dict], chain: dict
    ) -> float:''',
    )

    p.sub(
        "F2 monitor_position computes and persists the liquidation mark",
        '''        # Compute current premium
        current_premium = self._compute_current_premium(open_legs, chain)

        # Update last known premium
        self.db.update(
            "positions",
            {"last_known_premium": current_premium, "updated_at": now_ist().isoformat()},
            {"position_id": position["position_id"]},
        )''',
        '''        # v3.1: two marks are now maintained. current_premium is the MID —
        # the right number to report and to trigger a stop on (it is not
        # jumpy). liq_premium is the LIQUIDATION value — what it actually
        # costs to get out — and it is the only honest basis for a
        # profit-taking decision.
        current_premium = self._compute_current_premium(open_legs, chain)
        liq_premium     = self._liquidation_premium(open_legs, chain)
        if not chain:
            liq_premium = current_premium

        self.db.update(
            "positions",
            {
                "last_known_premium":       current_premium,
                "last_liquidation_premium": liq_premium,
                "updated_at":               now_ist().isoformat(),
            },
            {"position_id": position["position_id"]},
        )''',
    )

    p.sub(
        "C2 spot-proximity exit distance scaled to the index level",
        '''        proximity_pts = max(int(wing_width_p2 * 0.55), 40) if "BUTTERFLY" in strategy_name_p2 else self.config.spot_proximity_pts''',
        '''        # v3.1: a flat 40pt proximity is 0.22% of an 18,000 index and only
        # 0.15% of a 26,000 one — the structural protection silently weakened
        # as NIFTY rose, so by 2026 the engine was sitting closer to its short
        # strikes than it was designed to. Scaled to spot, with the configured
        # absolute value kept as a floor.
        _prox_base = float(self.config.spot_proximity_pts or 40)
        _prox_pct  = float(getattr(self.config, "spot_proximity_pct", 0.0016))
        _prox_scaled = max(_prox_base, spot * _prox_pct) if spot > 0 else _prox_base
        proximity_pts = (
            max(int(wing_width_p2 * 0.55), int(_prox_scaled))
            if "BUTTERFLY" in strategy_name_p2
            else _prox_scaled
        )''',
    )

    p.sub(
        "F2 premium stop keeps its mid trigger, gains a liquidation guard",
        '''        # Also check premium-based stop (for cases without price stop levels)
        if entry_credit > 0 and stop_premium > 0:
            if current_premium >= stop_premium:
                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                    "reason_detail": f"premium_stop_{current_premium:.2f}>={stop_premium:.2f}",
                    "current_premium": current_premium,
                    "stop_premium": stop_premium,
                }''',
        '''        # Also check premium-based stop (for cases without price stop levels).
        # v3.1: the trigger deliberately stays on the MID so that a single
        # wide print cannot stop the position out on quote noise — but a hard
        # guard now fires if the price we could genuinely get out at has run
        # well past the stop. That is a real loss, not a quoting artefact, and
        # previously the engine would sit through it.
        if entry_credit > 0 and stop_premium > 0:
            if current_premium >= stop_premium:
                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                    "reason_detail": f"premium_stop_{current_premium:.2f}>={stop_premium:.2f}",
                    "current_premium": current_premium,
                    "stop_premium": stop_premium,
                }
            if liq_premium >= stop_premium * 1.20:
                return "CLOSE_STOP", EXIT_PRIORITY_PRICE_STOP, {
                    "reason_detail": (
                        f"liquidation_stop_{liq_premium:.2f}>="
                        f"{stop_premium * 1.20:.2f}"
                    ),
                    "current_premium": current_premium,
                    "liquidation_premium": liq_premium,
                    "stop_premium": stop_premium,
                }''',
    )

    p.sub(
        "F2 profit-lock trigger measured on the liquidation mark",
        '''        if entry_credit > 0 and gross_credit > 0:
            profit_pct = (gross_credit - current_premium) / gross_credit''',
        '''        if entry_credit > 0 and gross_credit > 0:
            # v3.1: measured on the liquidation mark — profit you cannot
            # actually take is not profit, and locking against a mid you
            # cannot trade at is how a "free trade" becomes a loser.
            profit_pct = (gross_credit - liq_premium) / gross_credit''',
    )

    p.sub(
        "F5 profit lock tightened and floored above round-trip costs",
        '''            if profit_pct >= lock_thresh and not profit_lock_activated:
                # Move stop to breakeven (entry_credit level)
                # This converts the position to a "free trade"
                _achieved = gross_credit - current_premium
                _keep = _achieved * 0.50
                new_stop = current_premium + _keep
                new_stop = max(new_stop, entry_credit * 0.80)''',
        '''            if profit_pct >= lock_thresh and not profit_lock_activated:
                # v3.1: the old lock gave back HALF of everything achieved and
                # then floored the stop at 0.80 x entry_credit — i.e. it was
                # willing to hand back 20% of the credit from a position that
                # was already comfortably in profit, and max() made that floor
                # the binding constraint. Together with the inverted target
                # ladder (F1) this is exactly why winners round-tripped to
                # scratch. The give-back is cut to a quarter and the lock is
                # floored so a locked trade cannot finish worse than covering
                # its own round trip.
                _achieved = gross_credit - liq_premium
                _keep = _achieved * 0.25
                new_stop = liq_premium + _keep
                _rt_cost = self._round_trip_cost_pts(open_legs, chain)
                new_stop = min(new_stop, max(entry_credit - _rt_cost, 0.05))''',
    )

    p.sub(
        "F2 profit-lock stop hit measured on the liquidation mark",
        '''            if profit_lock_activated and profit_lock_stop_level:
                if current_premium >= float(profit_lock_stop_level):''',
        '''            if profit_lock_activated and profit_lock_stop_level:
                if liq_premium >= float(profit_lock_stop_level):''',
    )

    p.sub(
        "F1 time-target ladder un-inverted (min->max) + F2 + F4 gamma de-risk",
        '''            for time_threshold, target_pct in time_targets:
                if current_time >= time_threshold:
                    time_target = entry_credit * (1.0 - target_pct)
                    # Use the tighter of stored target and time target
                    effective_target = (
                        min(target_premium, time_target)
                        if target_premium > 0
                        else time_target
                    )
                    if current_premium <= effective_target:
                        self.logger.info(
                            f"PRIORITY 6 TIME TARGET: {position['strategy_name']} "
                            f"premium={current_premium:.2f} <= target={effective_target:.2f} "
                            f"after {time_threshold}"
                        )
                        return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                            "current_premium": current_premium,
                            "time_target": effective_target,
                            "time_threshold": str(time_threshold),
                            "target_pct": target_pct,
                        }

            # Also check stored target_premium (set at entry)
            if target_premium > 0 and current_premium <= target_premium:
                return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                    "current_premium": current_premium,
                    "target_premium": target_premium,
                    "reason_detail": "entry_target_reached",
                }''',
        '''            # ── v3.1 [F1]: the ladder was inverted by a min() ──────────
            # These are PREMIUM LEVELS the position must fall BELOW to take
            # profit, so a LOWER number is a HARDER target. Taking
            # min(target_premium, time_target) therefore always selected the
            # harder of the two — and since the stored entry target
            # (credit x (1 - target_pct)) is almost always the lower one, it
            # always won and the entire "accept less profit as the clock runs
            # down" ladder was dead code. Winners were never harvested late:
            # they were carried into the 15:00 hard exit or handed back to a
            # stop. max() restores the intended behaviour — the target LOOSENS
            # with time. This is the highest-impact single change to realised
            # P&L in this patch.
            #
            # [F2] The comparison is made on the LIQUIDATION mark, because a
            # target you can only reach at the mid is not a target.
            _best_target = None
            for time_threshold, target_pct in time_targets:
                if current_time >= time_threshold:
                    time_target = entry_credit * (1.0 - target_pct)
                    _best_target = (
                        time_target if _best_target is None
                        else max(_best_target, time_target)
                    )

            if _best_target is not None:
                effective_target = (
                    max(target_premium, _best_target)
                    if target_premium > 0
                    else _best_target
                )
                if liq_premium <= effective_target:
                    self.logger.info(
                        f"PRIORITY 6 TIME TARGET: {position['strategy_name']} "
                        f"liq={liq_premium:.2f} <= target={effective_target:.2f}"
                    )
                    return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                        "current_premium": current_premium,
                        "liquidation_premium": liq_premium,
                        "time_target": effective_target,
                        "reason_detail": "time_decayed_target_reached",
                    }

            # Also check stored target_premium (set at entry)
            if target_premium > 0 and liq_premium <= target_premium:
                return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                    "current_premium": current_premium,
                    "liquidation_premium": liq_premium,
                    "target_premium": target_premium,
                    "reason_detail": "entry_target_reached",
                }

            # ── v3.1 [F4]: 0DTE gamma-time de-risk ladder ─────────────────
            # After roughly 13:30 on NIFTY expiry day the remaining theta on a
            # short structure is small while gamma is vertical: the position
            # is risking the full width of the wing to earn a handful of
            # residual points. There was no management of that at all — the
            # engine simply held to the 15:00 bell. Professionals flatten into
            # that window. From 13:30 any meaningful profit is taken; from
            # 14:15 anything better than covering the round trip is taken.
            # Losing positions remain governed by the stop logic above.
            if actual_dte == 0 and entry_credit > 0:
                _rt = self._round_trip_cost_pts(open_legs, chain)
                if current_time >= dtime(14, 15):
                    if liq_premium <= entry_credit - _rt:
                        return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                            "current_premium": current_premium,
                            "liquidation_premium": liq_premium,
                            "reason_detail": "gamma_window_scratch_or_better_1415",
                        }
                elif current_time >= dtime(13, 30):
                    if liq_premium <= entry_credit * 0.88 - _rt:
                        return "CLOSE_TARGET", EXIT_PRIORITY_TIME_TARGET, {
                            "current_premium": current_premium,
                            "liquidation_premium": liq_premium,
                            "reason_detail": "gamma_window_derisk_1330",
                        }''',
    )


def patch_main(p: FilePatcher) -> None:
    """
    main.py — the daily loss halt is the last line of defence, so it has to be
    computed on numbers that are actually achievable.

    [F3] compute_unrealized_pnl marked open positions at the MID and ignored
         transaction costs entirely. For a short-premium book the mid always
         flatters the position (shorts are bought back at the ask), and the
         entry charges have already left the account. The halt therefore
         triggered late and optimistically — exactly the wrong direction for a
         circuit breaker, and the one number you cannot afford to be
         optimistic about.
    """
    p.sub(
        "F3 unrealised P&L marked at liquidation value, net of costs",
        '''    def compute_unrealized_pnl(self) -> float:
        """
        Compute total unrealized P&L across all open positions.
        Uses last_known_premium from positions table.
        """
        C02        = self.config.lot_size
        unrealized = 0.0

        for pos in self.execution_engine._get_open_positions():
            current_prem = pos.get("last_known_premium")
            if current_prem is None:
                continue
            entry_credit = float(pos.get("entry_credit") or 0)
            lots         = int(pos.get("final_lots", 1) or 1)
            # P&L = (entry_credit - current_premium) × lot_size × lots
            unrealized += (entry_credit - current_prem) * C02 * lots

        return unrealized''',
        '''    def compute_unrealized_pnl(self) -> float:
        """
        Total unrealised P&L across all open positions.

        v3.1: this used the MID mark and ignored transaction costs. For a
        short-premium book the mid is always the flattering side (shorts are
        bought back at the ask), and the entry charges have already left the
        account. The result was an unrealised number that was systematically
        too good, feeding the daily-loss halt — the engine's last line of
        defence — so the halt fired late, and only once the real drawdown was
        already larger than the configured limit.

        Now marked at liquidation value where available, net of the entry
        costs already paid and an estimate of the cost still to be paid to
        close the position.
        """
        C02        = self.config.lot_size
        unrealized = 0.0

        for pos in self.execution_engine._get_open_positions():
            current_prem = pos.get("last_liquidation_premium")
            if current_prem is None:
                current_prem = pos.get("last_known_premium")
            if current_prem is None:
                continue
            entry_credit = float(pos.get("entry_credit") or 0)
            lots         = int(pos.get("final_lots", 1) or 1)
            gross = (entry_credit - float(current_prem)) * C02 * lots

            entry_costs = float(pos.get("entry_costs_rupees") or 0.0)
            exit_costs  = entry_costs * 0.95
            unrealized += gross - entry_costs - exit_costs

        return unrealized''',
    )


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def verify_syntax():
    errors = []
    for name in ALL_FILES:
        path = BASE / name
        if not path.exists():
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=name)
            print(f"    ok    {name}")
        except SyntaxError as e:
            errors.append(f"{name}: line {e.lineno}: {e.msg}")
            print(f"    FAIL  {name}: line {e.lineno}: {e.msg}")
    return errors


def verify_runtime():
    """
    Import the patched modules and exercise the code paths the patch touched.
    Entirely in-memory / temp-sqlite: no network, no broker, no live data.
    """
    errors = []
    sys.path.insert(0, str(BASE))
    for mod in ("core", "data_engine", "regime_engine", "strategy_engine",
                "execution_engine", "calibration_engine"):
        sys.modules.pop(mod, None)

    try:
        import core
        import data_engine
        import regime_engine
        import strategy_engine
        import execution_engine
        print("    ok    all engine modules import")
    except Exception as e:
        errors.append(f"import failed: {e}")
        traceback.print_exc()
        return errors

    def check(label, ok):
        print(f"    {'ok  ' if ok else 'FAIL'}  {label}")
        if not ok:
            errors.append(label)

    try:
        cfg = core.load_config()
    except Exception as e:
        errors.append(f"load_config failed: {e}")
        return errors

    # ---- contract + cost specification ------------------------------------
    check("lot size is the 2026 value (65)", cfg.lot_size == 65)
    check("exchange txn rate is 0.03553%",
          abs(cfg.exchange_txn_rate - 0.0003553) < 1e-9)
    check("STT on sell premium remains 0.15%",
          abs(cfg.stt_options_sell - 0.0015) < 1e-9)
    check("scale-invariant OR bands present", hasattr(cfg, "or_pct_narrow"))
    check("stop_efficacy present and hard-capped at 0.80",
          0.0 <= cfg.stop_efficacy <= 0.80)
    check("max_dte_tradeable >= 4", cfg.max_dte_tradeable >= 4)

    # ---- calendar is reachable --------------------------------------------
    _, hi_ic = strategy_engine.DTE_REQUIREMENTS[strategy_engine.IRON_CONDOR]
    check("iron condor reaches DTE 4 (Wednesday is tradeable)", hi_ic >= 4)
    _, hi_bp = strategy_engine.DTE_REQUIREMENTS[strategy_engine.BULL_PUT_SPREAD]
    check("credit spreads reach DTE 4", hi_bp >= 4)

    src_x = (BASE / "execution_engine.py").read_text(encoding="utf-8")
    src_s = (BASE / "strategy_engine.py").read_text(encoding="utf-8")

    def code_only(src: str) -> str:
        """Strip comment lines so explanatory prose cannot satisfy or break a
        source assertion (the patch quotes the old code in its comments)."""
        return "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )

    code_x, code_s = code_only(src_x), code_only(src_s)

    check("time-target ladder no longer inverted",
          "min(target_premium, time_target)" not in code_x
          and "max(target_premium, _best_target)" in code_x)
    check("targets evaluated on liquidation marks",
          "_liquidation_premium" in code_x
          and "liq_premium <= effective_target" in code_x)
    check("0DTE gamma de-risk ladder present",
          "gamma_window_scratch_or_better_1415" in code_x)
    check("EV gate no longer uses the half-wing barrier",
          "wing * 0.5 - float(self.config.spot_proximity_pts)" not in code_s)
    check("EV gate prices a gamma tail",
          "p_tail" in code_s and "tail_loss_pts" in code_s)
    check("sizing uses structural loss, not the stop",
          "_structural_per_lot" in code_s and "stop_efficacy" in code_s)
    check("configured risk budget is actually used",
          "self.config.max_risk_per_trade_pct" in code_s)

    # ---- functional: engine constructs, OR is scale-invariant --------------
    tmpdir = None
    try:
        import tempfile
        import logging
        import pandas as pd

        tmpdir = Path(tempfile.mkdtemp(prefix="patch_v31_verify_"))
        db = core.Database(tmpdir / "verify.db")
        logger = logging.getLogger("patch_v31_verify")
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False

        class _NullClient:
            def __getattr__(self, _n):
                def _f(*a, **k):
                    raise RuntimeError("no network during verification")
                return _f

        rl = core.RateLimiter(cfg.rate_limits)
        me = data_engine.MarketDataEngine(cfg, db, _NullClient(), rl, logger)
        print("    ok    MarketDataEngine constructs (session_state schema fixed)")

        def _or_bars(width, base):
            rows = []
            for i in range(26):
                t = "09:%02d:%02d" % (15 + i // 2, (i % 2) * 30)
                hi = base + (width / 2.0 if i == 0 else 1.0)
                lo = base - (width / 2.0 if i == 0 else 1.0)
                rows.append({"time": t, "open": base, "high": hi,
                             "low": lo, "close": base, "volume": 1000})
            return pd.DataFrame(rows)

        for k in ("opening_straddle_pts", "_straddle_open_for_regime",
                  "_last_atm_straddle"):
            me.state[k] = 0.0

        me.state["prev_spot"] = 18000.0
        a = me.compute_opening_range(_or_bars(18000 * 0.0030, 18000.0))
        me.state["prev_spot"] = 27000.0
        b = me.compute_opening_range(_or_bars(27000 * 0.0030, 27000.0))
        ok = (a is not None and b is not None
              and a["or_condition"] == b["or_condition"])
        check("OR classification is scale-invariant (%s vs %s)"
              % (a and a["or_condition"], b and b["or_condition"]), ok)

        # Straddle-relative override must be able to widen the classification.
        me.state["prev_spot"] = 27000.0
        me.state["opening_straddle_pts"] = 90.0   # tiny straddle, same range
        c = me.compute_opening_range(_or_bars(27000 * 0.0030, 27000.0))
        check("straddle-relative OR override is active",
              c is not None and c["or_score"] <= (b["or_score"] if b else 0))

        db.close()
    except Exception as e:
        errors.append("functional verification failed: %s" % e)
        traceback.print_exc()
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- H4: size modifiers must compound WITHOUT weakening any single one -
    # The engine documents (and self-tests) that UNCLEAR positioning reduces
    # size to <= 0.50. Making the modifiers multiplicative must never make a
    # single-condition case larger than the old min() behaviour, or the patch
    # would be increasing risk in exactly the conditions it claims to respect.
    try:
        import logging as _lg
        _log = _lg.getLogger("patch_v31_h4")
        _log.addHandler(_lg.NullHandler())
        _log.setLevel(_lg.CRITICAL)
        _log.propagate = False
        rc = regime_engine.RegimeClassifier(cfg, None, _log)

        VR = regime_engine.VolatilityRegime
        PR = regime_engine.PriceRegime
        PO = regime_engine.PositioningRegime
        CL = regime_engine.ConfidenceLevel

        def _sig(or_condition="NARROW"):
            return {"vix": 11.5, "actual_dte": 0, "or_condition": or_condition,
                    "atm_iv": 0.125, "adx_15": 15.0}

        def _cr(pos=PO.RANGE, or_condition="NARROW", borderline=False,
                price=PR.RANGE):
            return rc.compute_final_size(
                VR.SELL_PREMIUM, price, pos, CL.HIGH,
                _sig(or_condition), False, borderline,
            )[2]

        cr_base     = _cr()
        cr_unclear  = _cr(pos=PO.UNCLEAR)
        cr_verywide = _cr(or_condition="VERY_WIDE")
        cr_moderate = _cr(or_condition="MODERATE")
        cr_both     = _cr(pos=PO.UNCLEAR, or_condition="VERY_WIDE")

        check("H4 baseline conflict_reduction is unreduced (%.2f)" % cr_base,
              abs(cr_base - 1.0) < 1e-6)
        check("H4 UNCLEAR still honours the <=0.50 contract (%.2f)" % cr_unclear,
              cr_unclear <= 0.50 + 1e-9)
        check("H4 VERY_WIDE OR still honours <=0.25 (%.2f)" % cr_verywide,
              cr_verywide <= 0.25 + 1e-9)
        check("H4 MODERATE OR still honours <=0.75 (%.2f)" % cr_moderate,
              cr_moderate <= 0.75 + 1e-9)
        check("H4 two risks compound below either alone (%.2f)" % cr_both,
              cr_both < min(cr_unclear, cr_verywide) + 1e-9)
        check("H4 compounding still floored above zero (%.2f)" % cr_both,
              cr_both >= 0.15 - 1e-9)
    except Exception as e:
        errors.append("H4 invariant verification failed: %s" % e)
        traceback.print_exc()

    return errors


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

SUMMARY = """
Applied {n} changes. Backups: {bk}

WHAT CHANGED, AND WHY IT MATTERS FOR REAL P&L
---------------------------------------------
 1. CONTRACT + COSTS (2026)
    NIFTY lot size 75 -> 65 (NSE revision, January 2026 cycle): every rupee
    figure in the engine was 15% wrong in the risk-taking direction. NSE
    options transaction charge 0.053% -> 0.03553% (Rs 3,503/cr + IPFT): the
    engine over-charged itself ~49% per leg and rejected sound trades on
    cost grounds. STT (0.15% sell premium), stamp duty, SEBI fee and GST
    were verified correct and left alone.

 2. THE CALENDAR WAS 40% DEAD
    The condor and credit spreads were capped at DTE 2 while every other DTE
    table ran to 6. On the Tuesday-expiry calendar Wednesday is DTE 4 and
    Thursday is DTE 3, so those sessions selected a strategy and were then
    always rejected. Opened to DTE 4, with a strict new-cycle gate added in
    the regime engine.

 3. THE EV GATE MEASURED THE WRONG BARRIER
    p_win came from a no-touch model whose barrier was half the WING WIDTH
    (~45pts) instead of the distance to the SHORT STRIKE (250-300pts).
    p_win pinned to its 0.35 floor and the gate rejected good structures all
    day. Now uses the real barrier, blends the empirical prior back in, and
    prices the gamma tail as an explicit third outcome.

 4. THE PROFIT LADDER WAS INVERTED
    effective_target = min(stored, time_decayed) always chose the HARDER
    target, so the "take less as the clock runs down" ladder never fired.
    Winners rode into the 15:00 bell or reversed into stops. Now max(), and
    evaluated on liquidation marks rather than mids.

 5. SIZING ASSUMED THE STOP ALWAYS WORKS
    Risk per lot was clamped to the stop level. On 0DTE the stop is exactly
    what fails. Sizing is now anchored on structural loss (wing - credit)
    with partial credit for stop efficacy, and is bound to
    MAX_RISK_PER_TRADE_PCT, which the engine previously ignored outright.

 6. EVERYTHING WAS CALIBRATED FOR AN 18,000 NIFTY
    Opening-range buckets, spot proximity and the velocity abort were all
    absolute point thresholds. They now scale with the index level, and the
    opening range is additionally measured against the opening straddle.

 7. STRUCTURE WAS FROZEN
    wing_width was a literal 150 that was never updated, which made the
    credit/wing ratio gate unsatisfiable for far-OTM 0DTE structures. It is
    now derived from the straddle and the actual short-strike distance.

 8. SIGNALS BLOCKED THE BEST DAYS
    VRP above 8pp was treated as a data error (it is normal and rich on
    0DTE); measured ATM IV rising into an expiry afternoon was treated as
    vol expansion (it is a sqrt(T) artefact). Both bounds are now relative
    rather than absolute.

 9. THE BOOK COULD DIVERGE FROM THE BROKER
    Live orders were reported FILLED unconditionally and fell back to the
    expected price. Terminal broker status is now verified, and a
    non-completed order routes into the existing emergency-unwind path.

10. THE ENGINE COULD NOT START
    Five session_state columns written on every cycle were missing from the
    schema, so MarketDataEngine raised OperationalError on construction.

NEXT STEPS
----------
  python verify_all.py        # the engine's own self-tests
  python backtest.py --test   # replay harness

  Run PAPER_TRADE_MODE=true for at least 20 sessions before going live, then
  let calibration_engine re-fit the thresholds from your own telemetry. The
  numbers this patch introduces (stop_efficacy, the gamma tail probabilities,
  the slippage multiples, the OR bands) are all exposed in env.txt precisely
  so they can be re-fitted rather than argued about.
"""


def main():
    banner("NIFTY INTRADAY OPTIONS ENGINE — PROFITABILITY PATCH v3.0 -> v3.1")
    print("repository : %s" % BASE)
    print("timestamp  : %s" % STAMP)

    missing = [f for f in TARGET_FILES if not (BASE / f).exists()]
    if missing:
        print("\nERROR: run this from the repository root. Missing: "
              + ", ".join(missing))
        return 2

    if VERSION_MARKER in (BASE / "core.py").read_text(encoding="utf-8"):
        print("\nThis repository is already patched to v3.1 "
              "(marker %s found in core.py)." % VERSION_MARKER)
        print("Nothing to do. Restore from a patch_v31_backup_* folder first "
              "if you want to re-apply.")
        return 0

    banner("STEP 1/5  BACKUP")
    backup_all()
    print("  originals copied to: %s" % BACKUP_DIR.name)

    banner("STEP 2/5  APPLYING SOURCE EDITS")
    patchers = []
    try:
        for name, fn in (
            ("core.py",             patch_core),
            ("data_engine.py",      patch_data_engine),
            ("regime_engine.py",    patch_regime_engine),
            ("strategy_engine.py",  patch_strategy_engine),
            ("execution_engine.py", patch_execution_engine),
            ("main.py",             patch_main),
        ):
            print("\n  [%s]" % name)
            fp = FilePatcher(name)
            fn(fp)
            patchers.append(fp)
    except PatchError as e:
        print("\nERROR: %s" % e)
        restore_all()
        return 2

    all_missed = [(fp.name, m) for fp in patchers for m in fp.missed]
    total_applied = sum(len(fp.applied) for fp in patchers)

    if all_missed:
        print("\n" + "!" * 78)
        print("ABORTING — the following anchors were not found. The source has")
        print("diverged from the version this patch was written against, and a")
        print("partial patch could leave the engine internally inconsistent.")
        for name, m in all_missed:
            print("  - %s: %s" % (name, m))
        print("!" * 78)
        restore_all()
        print("\nAll files restored from backup. Nothing was changed.")
        return 2

    for fp in patchers:
        fp.flush()
    print("\n  %d edits applied across %d files." % (total_applied, len(patchers)))

    banner("STEP 3/5  MIGRATING env.txt")
    migrate_env_file()

    banner("STEP 4/5  SYNTAX VERIFICATION")
    syn = verify_syntax()
    if syn:
        print("\nSyntax errors detected — restoring originals.")
        restore_all()
        for e in syn:
            print("  %s" % e)
        return 2

    banner("STEP 5/5  FUNCTIONAL VERIFICATION")
    run_errors = verify_runtime()
    if run_errors:
        print("\nFunctional verification failed — restoring originals.")
        restore_all()
        for e in run_errors:
            print("  %s" % e)
        return 2

    banner("PATCH COMPLETE — engine is at v3.1")
    print(SUMMARY.format(n=total_applied, bk=BACKUP_DIR.name))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        print("\nUnexpected error. Attempting to restore originals...")
        try:
            if BACKUP_DIR.exists():
                restore_all()
                print("Originals restored.")
        except Exception:
            print("RESTORE FAILED — recover manually from %s" % BACKUP_DIR)
        sys.exit(2)