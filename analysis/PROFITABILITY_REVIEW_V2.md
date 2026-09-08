# NIFTY Engine — Re-Review After v2 Patches

**Base:** `07daaa4` (my first review) → **`b6e8b57`** (your patches).
Diff: 5 files, +122 / −219. `execution_engine.py`, `main.py`, `core.py`, `backtest.py`,
`eod_report.py` were **not** touched.

Reproduce:
```
python3 analysis/patch_check.py           # do the patches do what they claim?
python3 analysis/v2_gate_consistency.py   # internal consistency of the v2 gates
```

---

## 0. Verdict

Five of the fixes are real and correctly reasoned. But **the engine still enters
zero trades**, for a different reason than before, and the patches introduced four
new problems — two of which push in the *opposite* direction from what was intended.

| v1 finding | status |
|---|---|
| §1 EV gate unsatisfiable | **partially fixed → still blocking** |
| §2 sizing blocks IC below ₹31L | **fixed** |
| §3 Parkinson bias | **over-corrected — now biased the other way** |
| §4 VRP threshold too low | **fixed** |
| §5 exit ladder / delta stop | not addressed |
| §6 margin 6.5× understated | not addressed |
| §7 no real backtest | not addressed |
| §8 calibration overfits | not addressed |

---

## 1. What was genuinely fixed

- **`risk_pts` 2.5C → 1.5C** (`strategy_engine.py:854`). Correct. This now matches the
  real loss at the `stop_premium = 2.5 × credit` exit level. The quantity error is gone.
- **Sizing** (`strategy_engine.py:1066-1092`): the hard 2-lot minimum is removed and risk
  is `min(1.5 × credit × lot, structural)`. The ₹31L capital wall is gone; a ₹10L
  account can now size 1 lot.
- **VRP thresholds** (`regime_engine.py:1085-1106`): DTE0 `×0.75` → `×1.00`, the
  VERY_NARROW/NARROW discounts deleted, absolute floor `1.0 → 2.0pp`. This was the
  single most important fix and it is right.
- **ATM IV guard made DTE-aware** (`data_engine.py:1386-1397`): 0.40–5.00 on 0DTE.
  Correct — the old 0.60–2.00 band was invalid on expiry day.
- **Parkinson correction added** (`data_engine.py:1098`). Right idea; wrong constant — see §3.

---

## 2. Still blocking: the EV gate rejects everything

`risk_pts` was fixed but the **geometry** was not. Breakeven `p_win = 1.5/(1.5+target)`:

| DTE | target | breakeven p_win | max p_win available | result |
|---|---|---|---|---|
| 0 | 0.50 | 0.750 | 0.72 + 0.08 = **0.80** | reachable |
| 1 | 0.42 | 0.781 | 0.68 + 0.08 = **0.76** | **impossible** |
| 2 | 0.30 | 0.833 | 0.64 + 0.08 = **0.72** | **impossible** |

**DTE1 (Monday) and DTE2+ are now mathematically dead at every credit size** — no
market condition can make them pass. The engine is a Tuesday-only system by arithmetic,
not by design.

For DTE0, minimum net credit required to clear `min_ev`:

| DTE | OR | VRP 2.5 | VRP 3.5 | VRP 5.0 |
|---|---|---|---|---|
| 0 | VERY_NARROW | never | 350 pts | **50 pts** |
| 0 | NARROW | never | never | never |
| 0 | MODERATE / WIDE | never | never | never |

Exactly **one cell of twelve** can ever pass: `DTE0 + VERY_NARROW OR + VRP > 4.0pp`,
needing ≥ **51.3 pts** net credit. The 150pt-wing condor the engine actually builds at
09:50 yields **49.6 pts**. It misses by 1.7 points. Sweeping the realistic grid:
**0 of 48 combinations pass** (previously 0 of 27).

The unfixed root cause is the one I flagged in §1 of the first review and it is still
there verbatim: `p_win_table` is a **terminal** (expire-OTM) probability, while the
engine exits on **touch**. Touch probability ≈ 2 × terminal probability. Until `p_win`
is computed as a first-passage probability against the *actual* barrier
(short strike − 40pts proximity), no amount of constant-tuning will make this gate
correspond to reality. Lowering `min_ev` from `0.08C` to `0.03C` is the wrong lever —
it weakens the safety margin without touching the geometry.

Related dead code: `p_win` is now capped at **0.88**, but the table maxes at 0.72 and the
largest `vrp_adj` is +0.08, so 0.80 is the true ceiling. The 0.88 cap implies headroom
that does not exist.

---

## 3. New problem: the Parkinson correction over-corrects

`rv = sqrt(...) * 1.15`. Measured against a known 10.50% ground truth:

| ticks/min | raw RV | ×1.15 | residual bias | resulting VRP error |
|---|---|---|---|---|
| 5 | 7.96% | 9.16% | −1.34pp | **+1.34pp** (still too rich) |
| 20 | 9.05% | 10.45% | −0.05pp | +0.05pp ✅ |
| **60** (≈1/sec index feed) | 9.66% | **11.08%** | **+0.58pp** | **−0.58pp** |
| 200 | 10.00% | 11.51% | +1.01pp | −1.01pp |

1.15 is only correct at ~20 ticks/min. NIFTY spot is disseminated roughly **once per
second**, so at ~60 effective observations per bar the estimator now reads **high**,
which pushes measured VRP **down** by ~0.6pp. Sampling s.d. is unchanged at 0.49pp
because the window is still 60 bars.

This compounds badly with §2: the EV gate needs a **measured** VRP > 4.0pp, which after
a −0.6pp measurement bias requires a **true VRP of ~4.6pp**. At India VIX ~11 that is
a rare-days-per-year event, and the engine has no way to know how rare because there
is still no backtest.

The fix is not a better constant. Calibrate the factor against your own feed
(replay one day of ticks, build 1-min bars, solve for the multiplier that recovers
5-second realised variance), or drop Parkinson and compute realised variance directly
from 5-second returns.

---

## 4. New problem: the exit-slippage patch has no effect

`_compute_slippage` exit multiplier went 1.5 → 3.0. But:

```
strategy_engine.py:954   total_slippage = self._compute_slippage(validated_legs)   # is_exit=False
strategy_engine.py:1728  engine._compute_slippage(legs_ba, is_exit=True)           # SELF-TEST ONLY
strategy_engine.py:1743  engine._compute_slippage(legs_no_ba, is_exit=True)        # SELF-TEST ONLY
```

`is_exit=True` is **never called on a production path**. The patch changed a self-test
assertion and nothing else.

Worse, it made the gate internally inconsistent. `_compute_ev_gate` approximates round
trip as `(entry_costs + entry_slippage) × 2.0` — which assumes exit slippage equals
entry slippage. The v2 model now says exit slippage is **6×** entry (3.0 vs 0.5
half-spreads), so the correct multiplier is ~3.5, not 2.0:

| | pts |
|---|---|
| gate friction, as coded | 3.59 |
| true round trip under the v2 model | 4.57 |
| **understated by** | **0.98 pts (27%)** |

Feed the honest number back in and the one surviving cell needs **65.3 pts** of credit
instead of 51.3 — further out of reach.

---

## 5. New problem: the two gates now disagree silently

- `regime_engine` emits `SELL_PREMIUM` at **2.0pp** and `STRONG_SELL_PREMIUM` at 2.6pp.
- `strategy_engine`'s EV gate needs `vrp_adj = +0.08`, i.e. **VRP > 4.0pp**.

Every cycle with VRP between 2.0 and 4.0pp is greenlit by the regime engine, passes
strategy selection, builds legs, prices them — and is then killed by the EV gate. The
operator sees `NO_TRADE` with an `ev_gate:` string while the dashboard says
`SELL_PREMIUM`. All of it lands in the phantom log. Pick one threshold and derive the
other from it.

---

## 6. New risk: sizing now assumes the stop always holds

`structural_loss_per_lot = min(1.5 × credit × lot, structural)` — ₹3,218 vs a true
max loss of ₹7,605 (**2.4×**). Sizing off the stop rather than the wing is standard
professional practice, but only when the stop is reliable. This one is not: it is
evaluated on a **45-second** monitoring cycle (`REGIME_CALC_INTERVAL_SEC=45`, unchanged),
and the position is short gamma on expiry day.

| capital | lots | intended worst case | actual if the stop gaps |
|---|---|---|---|
| ₹10L | 1 | ₹3,218 (0.32%) | ₹7,605 (0.76%) |
| ₹20L | 3 | ₹9,652 (0.48%) | ₹22,815 (1.14%) |

Within the 2% daily halt for a single trade, but `MAX_ENTRIES_PER_DAY=3` means three
gap-throughs is ~3.4%. Either keep the structural basis for sizing, or tighten the
monitoring loop to a few seconds so the stop is actually enforceable.

---

## 7. New behaviour: `is_0dte = is_tue`

`data_engine.py:1882` — previously 0DTE was selected only between 12:30 and 14:00 on
Tuesday; now the whole Tuesday session resolves to the expiring contract.

Combined with §2 killing DTE1/DTE2, the engine is now **Tuesday-only, 0DTE-only**.
Two consequences:

1. Entries can start at 09:45 on expiry day. At 09:50 the strike selector places shorts
   at spot ±100pts ≈ **0.29 delta** — that is maximum gamma for the least theta, held
   against a 0.40-delta stop. It is the exact configuration my first review measured at
   **−₹41/lot** on a realistic path at +2pp VRP.
2. The margin omission (§8) now applies to **100%** of trades rather than some of them.

---

## 8. Unfixed from the first review — and now more expensive

- **Margin still `wing × 65 × 1.10`** (`strategy_engine.py:1102`) = ₹10,725/lot. SEBI's
  expiry-day additional 2% ELM on short options is still absent: 2% × 23,900 × 65 × 2
  short legs = **₹62,140/lot**. Realistic 0DTE margin ≈ ₹69,745 — **6.5× understated**.
  Since the engine is now Tuesday-0DTE-only, this is no longer an edge case.
- **Exit ladder untouched**: 40pt proximity stop and 0.40 delta stop on a 0.29-delta
  entry. `DELTA_CLOSE_THRESHOLD=0.40`, `SPOT_PROXIMITY_PTS=40` unchanged.
- **Stale VRP fallback untouched** (`data_engine.py:1179-1183`): when `atm_iv` is `None`,
  it still returns `self._vrp_buffer[-1]`. No VRP should mean no trade, not a stale one.
- **`backtest.py` untouched** — still a DB replay of trades already taken. There remains
  no way to validate any of this before funding it.
- **`MIN_SAMPLES_VRP = 3`** (`calibration_engine.py:255`). Three trades in a bucket can
  move `vrp_sell_threshold`. This is worse than I flagged the first time, and it is now
  the mechanism most likely to quietly undo the good §1 threshold fix.
- **45s cycle** unchanged — see §6.

---

## Recommended next patch, in order

1. **Replace `p_win` with a touch probability.** `p ≈ 1 − 2·N(−d/(σ√T))` where `d` is the
   distance to the *effective* barrier (short strike − 40pts) and `σ√T` comes from live
   ATM IV and time to 15:00. Delete `p_win_table`. This is the one change that makes the
   gate mean something; everything else in §2 follows from it.
2. **Fix the reward:risk geometry.** 1:3 needs 75%+. Either raise DTE0 target toward
   0.65–0.70 of credit, or cut `stop_premium` to ~1.5× credit (loss 0.5C, breakeven 43%).
   Do this *before* touching `min_ev` again.
3. **Use the real round-trip friction in the gate**: `entry_costs + exit_costs +
   entry_slip + exit_slip`, calling `_compute_slippage(legs, is_exit=True)` for the exit
   leg. Delete the `× 2.0` proxy.
4. **Calibrate the RV factor against your own feed** instead of the 1.15 constant, and
   widen the window to 90–120 bars.
5. **Reconcile the VRP thresholds** — one source of truth; have `regime_engine` and the
   EV gate read the same number.
6. **Add the expiry-day ELM** to `margin_per_lot` and gate on real available margin.
7. **Raise `MIN_SAMPLES_VRP`** to 30+ and never let calibration lower `vrp_sell_threshold`
   below the 2.0pp floor you just established.

---

## Bottom line

The threshold fix (§1 of v1) was the right call and it is correctly implemented. But the
EV gate still blocks every trade because you fixed the *risk quantity* and left the
*probability model* and the *reward:risk geometry* untouched — and the two patches that
were meant to make costs more honest either do nothing (§4) or push the core signal the
wrong way (§3). Net position: the engine went from "rejects everything for one reason"
to "rejects everything for a subtler reason, and is now Tuesday-only by accident."

The single highest-value change remains the same as last time: make `p_win` a
first-passage probability. Everything downstream is currently being tuned against a
number that does not describe the trade being placed.
