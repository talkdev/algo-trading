#!/usr/bin/env python3
"""
Variant A/B harness for etf-momentum-screener.py (TSMOM on Nifty ETF universe).

Runs the repo's own event-driven Backtester against the REAL cached price data
in stock-data/tsmom.db, monkeypatching module constants per variant.
Every variant is evaluated:
  - on the FULL period available,
  - as IN-SAMPLE (first 80% of calendar) vs OUT-OF-SAMPLE (last 20%).

No network access required.  Output: experiments/results_<tag>.json + stdout table.
"""

import importlib.util
import json
import sys
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("tsmom", ROOT / "etf-momentum-screener.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

PARAMS = dict(m.BASELINE_PARAMS)   # {lookback:252, skip:21, entry:0.02, exit:0.00}

# --------------------------------------------------------------- load data ---
db = m.Db()
with redirect_stdout(StringIO()):
    prices, meta = m.load_prices_from_db(db)
all_dates = sorted({d for s in prices.values() for (d, *_x) in s})
n = len(all_dates)
IS_END = all_dates[int(n * 0.8)]          # in-sample end / OOS start
FULL = (all_dates[0], all_dates[-1])
print(f"calendar: {FULL[0]} -> {FULL[1]} ({n} sessions) | IS ends {IS_END}")

# ------------------------------------------------------------- variant defs --
# name -> (config overrides, param overrides)
VARIANTS = {
    "V0_baseline":           ({}, {}),
    "V1_rank_riskadj":       ({"RANK_BY": "risk_adjusted"}, {}),
    "V2_ensemble_lbs":       ({"ENSEMBLE_LOOKBACKS": (126, 189, 252)}, {}),
    "V3_debt_rankable":      ({"CASH_PROXY_SYMBOLS": {"LIQUIDCASE"}}, {}),
    "V4_corr_070":           ({"PAIRWISE_CORR_CAP": 0.70}, {}),
    "V5_rebal_quarterly":    ({"REBALANCE_FREQ": "Q"}, {}),
    "V6_no_200dma_overlay":  ({"EXIT_OVERLAY_200DMA": False}, {}),
    "V7_rotation_rule":      ({"ROTATION_RULE": True}, {}),
    "V8_lb189":              ({}, {"lookback": 189}),
    "V9_lb126":              ({}, {"lookback": 126}),
    # --- round 2: explore around winner V4 ---------------------------------
    "V10_corr_060":          ({"PAIRWISE_CORR_CAP": 0.60}, {}),
    "V11_corr_075":          ({"PAIRWISE_CORR_CAP": 0.75}, {}),
    "V12_corr70_ensemble":   ({"PAIRWISE_CORR_CAP": 0.70,
                               "ENSEMBLE_LOOKBACKS": (126, 189, 252)}, {}),
    "V13_corr70_rotation":   ({"PAIRWISE_CORR_CAP": 0.70,
                               "ROTATION_RULE": True}, {}),
    "V14_corr70_hold6":      ({"PAIRWISE_CORR_CAP": 0.70, "MAX_HOLDINGS": 6}, {}),
    "V15_corr70_hold4":      ({"PAIRWISE_CORR_CAP": 0.70, "MAX_HOLDINGS": 4}, {}),
    "V16_corr70_ens_rot":    ({"PAIRWISE_CORR_CAP": 0.70, "ROTATION_RULE": True,
                               "ENSEMBLE_LOOKBACKS": (126, 189, 252)}, {}),
    "V17_corr_065":          ({"PAIRWISE_CORR_CAP": 0.65}, {}),
    "V18_corr70_debt":       ({"PAIRWISE_CORR_CAP": 0.70,
                               "CASH_PROXY_SYMBOLS": {"LIQUIDCASE"}}, {}),
    # --- round 3 (2026-10-27, second audit): profitability knobs, measured vs
    #     the NEW adopted baseline (stock config now = corr 0.70 + rotation) ---
    "W1_voltgt_008":         ({"VOL_TARGET": 0.08}, {}),
    "W2_voltgt_010":         ({"VOL_TARGET": 0.10}, {}),
    "W3_voltgt_020":         ({"VOL_TARGET": 0.20}, {}),
    "W4_execlag_5":          ({}, {"exec_lag": 5}),
    "W5_execlag_10":         ({}, {"exec_lag": 10}),
    "W6_execlag_20":         ({}, {"exec_lag": 20}),
    "W7_entry_000":          ({}, {"entry": 0.00}),
    "W8_entry_003":          ({}, {"entry": 0.03}),
    "W9_entry_005":          ({}, {"entry": 0.05}),
    "W10_exit_pos002":       ({}, {"exit": 0.02}),
    "W11_exit_neg002":       ({}, {"exit": -0.02}),
    "W12_notradeband_010":   ({"NO_TRADE_BAND": 0.10}, {}),
    "W13_notradeband_030":   ({"NO_TRADE_BAND": 0.30}, {}),
    "W14_incumbency_0":      ({"INCUMBENCY_BONUS": 0}, {}),
    "W15_incumbency_6":      ({"INCUMBENCY_BONUS": 6}, {}),
    "W16_no_regime_scaling": ({"REGIME_SCALING": False}, {}),
    # --- round 4: mid-month guard-rail exits (2026-10-27 live audit: holdings
    #     breaching exit rules were stuck until month-end) -------------------
    "G1_guard_wk_both":      ({}, {"guard_freq": 5,  "guard_mode": "both"}),
    "G2_guard_daily_both":   ({}, {"guard_freq": 1,  "guard_mode": "both"}),
    "G3_guard_wk_dma":       ({}, {"guard_freq": 5,  "guard_mode": "dma"}),
    "G4_guard_wk_mom":       ({}, {"guard_freq": 5,  "guard_mode": "mom"}),
    "G5_guard_2wk_both":     ({}, {"guard_freq": 10, "guard_mode": "both"}),
    # --- round 5: structural constraints ------------------------------------
    "F1_family_rule_off":    ({"ONE_PER_INDEX_FAMILY": False}, {}),
    "F2_liq_min_005":        ({"MIN_LIQ_WEIGHT": 0.05}, {}),
    "F3_liq_min_020":        ({"MIN_LIQ_WEIGHT": 0.20}, {}),
    "F4_poscap_033":         ({"POS_CAP_ABS": 0.33}, {}),
    "F5_poscap_020":         ({"POS_CAP_ABS": 0.20}, {}),
    "F1F4_family_off_cap033": ({"ONE_PER_INDEX_FAMILY": False,
                                "POS_CAP_ABS": 0.33}, {}),
    # --- round 6: exec-date trend-guard capital policies (user run
    #     2026-10-27 #2: 3 of 5 basket names below their exit rules at exec
    #     time; ~Rs 6.4L of the plan idled in cash mid-month) ----------------
    "R6P_execguard_park":     ({}, {"exec_guard": "park"}),
    "R6R_execguard_redist":   ({}, {"exec_guard": "redistribute"}),
    "R6S_execguard_subst":    ({}, {"exec_guard": "substitute"}),
    # --- round 7: plausibility-gate exclusion at selection (live flags
    #     2026-10-27: MONQ50 vol 121% p.a. / +101% momentum print, MASPTOP50
    #     vol 65% p.a. — spliced/unadjusted series remain selectable today) --
    "P1_plaus_vol":           ({}, {"plaus_excl": "vol"}),
    "P2_plaus_mom":           ({}, {"plaus_excl": "mom"}),
    "P3_plaus_both":          ({}, {"plaus_excl": "both"}),
    # --- round 8: external second-opinion audit proposals (2026-10-27) ------
    "R8A_lag20_refresh5":     ({}, {"exec_lag": 20, "refresh_late": 5}),
    "R8B_renorm_excess":      ({}, {"renorm_excess": True}),
    "R8C_maxhold_6":          ({"MAX_HOLDINGS": 6}, {}),
    "R8D_maxhold_7":          ({"MAX_HOLDINGS": 7}, {}),
    "R8E_rank_riskadj":       ({"RANK_BY": "risk_adjusted"}, {}),
    # --- round 9: external comprehensive audit (2026-10-27, 15-section F-set)
    #     A-D isolate the COST of each newly-adopted honesty fix (variant turns
    #     the fix OFF -> legacy accounting); E/F test tighter data-integrity
    #     gates; G/H test the two remaining trade-rule proposals.  I = all four
    #     honesty fixes OFF together (pre-audit reporting stack).
    "R9A_tax_legacy_buckets": ({"TAX_BUCKET_DETAIL": False}, {}),
    "R9B_cash_untaxed":       ({"TAX_ON_CASH_ACCRUAL": False}, {}),
    "R9C_settle_annual":      ({"SETTLE_MODE": "annual_first"}, {}),
    "R9D_spread_legacy":      ({"SPREAD_RECALIBRATED": False}, {}),
    "R9E_integ_tight":        ({"INTEG_MAX_VOL": 1.00, "INTEG_MAX_MOM": 1.50}, {}),
    "R9F_integ_mid":          ({"INTEG_MAX_VOL": 1.20, "INTEG_MAX_MOM": 2.00}, {}),
    "R9G_rotation_rankfirst": ({"ROTATION_RANKFIRST": True}, {}),
    "R9H_mintrade_frac":      ({}, {"min_trade_frac": 0.0002}),
    "R9I_pre_audit_stack":    ({"TAX_BUCKET_DETAIL": False,
                                "TAX_ON_CASH_ACCRUAL": False,
                                "SETTLE_MODE": "annual_first",
                                "SPREAD_RECALIBRATED": False}, {}),
    # --- round 10: transcript audit 2026-10-27 §11.6 — stale-signal discipline.
    #     A/B inside a late-exec harness (exec_lag=20 reproduces the user's
    #     16-session-late fresh-journal scenario): deploy late with the trend
    #     guard only (production) vs SUPPRESS new entries when the signal is
    #     >10 sessions old and wait for the next month-end.
    "R10A_lag20_base":        ({}, {"exec_lag": 20}),
    "R10B_lag20_staleskip10": ({}, {"exec_lag": 20, "stale_skip_days": 10}),
    # --- round 11 (audit v3 F10): sector concentration cap.  The 2026-09-30
    #     basket was 4/5 sectoral-thematic names; the family+correlation caps
    #     did not stop it.  Variants cap the COMBINED sector weight; no re-norm.
    "R11A_sector40":          ({"SECTOR_CAP_FRAC": 0.40}, {}),
    "R11B_sector60":          ({"SECTOR_CAP_FRAC": 0.60}, {}),
    # --- round 12 (user ask 2026-10-28): immediate exit of holdings whose
    #     rules fire mid-month, instead of waiting for the month-end.  The
    #     default is set from THESE numbers, not from the ask.
    "R12A_daily_exit":        ({}, {"daily_exit": True}),
    # --- round 13 (audit v5, 2026-10-28): Bug #6 entry-overlay momentum floor,
    #     Bug #8 vol blend vs LW saturation, + re-checks of corr cap and regime
    #     scaling after the regime shift (all broad indices below 200-DMA).
    #     V0_baseline is re-run too for same-binary comparability.
    "R13A_entry_dma_mom5":    ({"ENTRY_OVERLAY_MOM_FLOOR": 0.05}, {}),
    "R13B_entry_dma_mom10":   ({"ENTRY_OVERLAY_MOM_FLOOR": 0.10}, {}),
    "R13C_volblend_30":       ({"VOL_BLEND_252": 0.30}, {}),
    "R13D_volblend_50":       ({"VOL_BLEND_252": 0.50}, {}),
    "R13E_corr_060":          ({"PAIRWISE_CORR_CAP": 0.60}, {}),
    "R13F_corr_075":          ({"PAIRWISE_CORR_CAP": 0.75}, {}),
    "R13G_regime_off":        ({"REGIME_SCALING": False}, {}),
    # --- round 18 (28-Oct-2026, audit v7): the six India-listed international
    #     ETFs trade at REAL premiums to NAV (SEBI/RBI overseas-unit freeze;
    #     web-verified 28-Oct: VRO/DSIJ/Mint/BSE).  nav_data has no stored
    #     history, so the live premium guard cannot be replayed in backtest —
    #     this hard blacklist bounds the historical drag of banning the six
    #     outright (the strictest possible premium policy).
    "R18A_globals_banned":    ({"_EXCLUDE_SYMBOLS": ("MAFANG", "MAHKTECH", "MASPTOP50",
                                                     "MONQ50", "MON100", "HNGSNGBEES")}, {}),
    # --- round 20 (external audit, 28-Oct-2026 night).  Every claim re-measured
    #     on the CURRENT binary (V0 re-run included).  Adoption rule (auditor):
    #     a gain must beat the bootstrap band, not be a 1pp point estimate.
    # premium-cap decision data: ban the 4 blow-offs only, vs all-but-BeES
    "R20A_ban4hot":           ({"_EXCLUDE_SYMBOLS": ("MAFANG", "MAHKTECH", "MASPTOP50",
                                                     "MONQ50")}, {}),
    "R20B_ban5_keepbees":     ({"_EXCLUDE_SYMBOLS": ("MAFANG", "MAHKTECH", "MASPTOP50",
                                                     "MONQ50", "MON100")}, {}),
    # auditor lever 2: re-measure ranking variants on the current binary
    "R20C_rank_riskadj":      ({"RANK_BY": "risk_adjusted"}, {}),
    "R20D_ensemble":          ({"ENSEMBLE_LOOKBACKS": (63, 126, 252)}, {}),
    "R20E_sector40":          ({"SECTOR_CAP_FRAC": 0.40}, {}),
    "R20F_sector60":          ({"SECTOR_CAP_FRAC": 0.60}, {}),
    # auditor lever 5: tax-aware exit deferral (explicit rule, never backtested)
    "R20G_taxdefer":          ({"TAX_DEFER_EXITS": True}, {}),
    # auditor lever 1a: renorm the capped-out excess instead of holding cash
    "R20H_renorm":            ({}, {"renorm_excess": True}),
    # auditor lever 1b/1c/6: the three NEW knobs (engine code added this round)
    "R20I_seatfill":          ({}, {"seat_fill": True}),
    "R20J_parkbench":         ({}, {"park_bench": True}),
    "R20K_ddsleeve":          ({}, {"dd_sleeve_scale": True}),
    # --- audit v12 (2026-10-29 live audit, leak-3): live AUTOFILL pacing is
    # gap/sessions_to_replan — a ~21-session linear ramp; the backtest
    # executes next-session (ramp=1).  Sweep the ramp cap for the proposed
    # AUTOFILL_MAX_SESSIONS=3 (vs 5, 21-as-is).
    "R22A_ramp1":            ({}, {"deploy_ramp": 1}),
    "R22B_ramp3":            ({}, {"deploy_ramp": 3}),
    "R22C_ramp5":            ({}, {"deploy_ramp": 5}),
    "R22D_ramp21":           ({}, {"deploy_ramp": 21}),

}

# pseudo-key handled by run_one (filtered price map — not a module constant)
PSEUDO_KEYS = {"_EXCLUDE_SYMBOLS"}

# window-shift robustness windows (in addition to FULL + 80/20 split)
SHIFT_WINDOWS = [
    ("2015-10-20", "2024-10-31"),   # truncated recent bull
    ("2018-01-01", "2023-12-31"),   # mid window incl. 2020 crash + 2022 chop
    ("2020-01-01", "2026-10-16"),   # post-COVID only
]

# ----------------------------------------------------------------- runner ----
def run_one(cfg_over, par_over, start=None, end=None):
    real_over = {k: v for k, v in cfg_over.items() if k not in PSEUDO_KEYS}
    saved = {k: getattr(m, k) for k in real_over}
    for k, v in real_over.items():
        setattr(m, k, v)
    excl = set(cfg_over.get("_EXCLUDE_SYMBOLS", ()))
    px = prices if not excl else {k: v for k, v in prices.items() if k not in excl}
    try:
        bt = m.Backtester(px, meta, dict(PARAMS, **par_over),
                          cost_case="base", strategy="B",
                          apply_tax=True, risk_on=True)
        with redirect_stdout(StringIO()):
            res = bt.run(start_date=start, end_date=end)
    finally:
        for k, v in saved.items():
            setattr(m, k, v)
    return res


M_KEYS = ["start", "end", "years", "cagr_after_cost_tax", "cagr_after_cost",
          "volatility", "sharpe", "max_drawdown", "avg_invested",
          "avg_turnover", "trades", "skipped", "tax_accrued",
          "final_equity", "recovery_days"]


def metrics(res):
    if res.get("error"):
        return {"error": res["error"]}
    return {k: res.get(k) for k in M_KEYS}


results = {}
only = [a for a in sys.argv[1:] if not a.startswith("--")]
do_windows = "--windows" in sys.argv
for name in VARIANTS:
    if only and name not in only:
        continue
    cfg_over, par_over = VARIANTS[name]
    t0 = time.time()
    r_full = run_one(cfg_over, par_over)
    r_is = run_one(cfg_over, par_over, start=FULL[0], end=IS_END)
    r_oos = run_one(cfg_over, par_over, start=IS_END, end=FULL[1])
    entry = {"full": metrics(r_full), "is": metrics(r_is),
             "oos": metrics(r_oos)}
    if do_windows:
        for i, (ws, we) in enumerate(SHIFT_WINDOWS):
            entry[f"win{i}"] = metrics(run_one(cfg_over, par_over, start=ws, end=we))
    results[name] = entry
    el = time.time() - t0
    f = results[name]["full"]
    if "error" in f:
        print(f"{name:22s} ERROR: {f['error']}")
    else:
        print(f"{name:22s} full: CAGR {f['cagr_after_cost_tax']*100:6.2f}%  "
              f"Sharpe {f['sharpe']:5.2f}  MaxDD {-f['max_drawdown']*100:6.2f}%  "
              f"turn {f['avg_turnover']:4.2f}x  inv {f['avg_invested']*100:5.1f}%  "
              f"| OOS CAGR {results[name]['oos'].get('cagr_after_cost_tax', 0)*100:6.2f}% "
              f"({el:.0f}s)")
        if do_windows:
            for i, (ws, we) in enumerate(SHIFT_WINDOWS):
                w = results[name][f"win{i}"]
                print(f"{'':22s} win{i}: {ws[:4]}-{we[:4]}  CAGR "
                      f"{w.get('cagr_after_cost_tax', 0)*100:6.2f}%  "
                      f"Sharpe {w.get('sharpe', 0):5.2f}  "
                      f"MaxDD {-w.get('max_drawdown', 0)*100:6.2f}%")

# buy & hold reference (NIFTYBEES)
ref = [r for r in prices.get("NIFTYBEES", []) if r[0] >= FULL[0]]
if ref:
    bh = ref[-1][1] / ref[0][1]
    yrs = len(ref) / 252
    print(f"\nBUY&HOLD NIFTYBEES reference: CAGR {(bh ** (1/yrs) - 1) * 100:.2f}% "
          f"(gross, {FULL[0]} -> {FULL[1]})")

out = ROOT / "experiments" / f"results_variants.json"
# keep the full audit trail: filtered runs update their rows instead of
# clobbering earlier rounds (round-11 fix — R10-only runs had wiped the file)
old = {}
if out.exists():
    try:
        old = json.loads(out.read_text())
    except Exception:
        old = {}
old.update(results)
out.write_text(json.dumps(old, indent=2, default=str))
print(f"\nwrote {out}")
