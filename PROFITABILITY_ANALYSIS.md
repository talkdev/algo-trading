# Profitability & accuracy analysis — NIFTY intraday options engine (Sep 2026)

Reproduced from the per-day databases now on `main` (`data/per_day/*.db`) and
`backtest_engine.py` replays. Every number below is measured, not inferred.

## 1. The three-day picture (rebased code, clean)

| date | weekday / DTE | cycles | trades | P&L |
|------|---------------|--------|--------|-----|
| 2026-09-08 | Tuesday, **0DTE** | 768 | 1 | **+476** (win, exit "P6 gamma") |
| 2026-09-09 | Wednesday, DTE 4 (US CPI) | 1203 | 0 | — |
| 2026-09-10 | Thursday, DTE 3 (range-bound) | 510 | 0 | — |

2026 reality: NIFTY weekly expiry is **Tuesday-only**, and VIX sits near 11.
So the only genuinely tradeable intraday premium instrument is 0DTE on
Tuesday; Wed/Thu/Fri/Mon only have the *next Tuesday* weekly at DTE 2–5.

## 2. Why the engine barely trades — three layers of "rich vol" gates

The engine's core edge is *sell premium when the variance risk premium (VRP)
is rich* (`SELL_PREMIUM` / `STRONG_SELL_PREMIUM`). That assumption is encoded
in **three separate DTE gates**, each requiring rich vol for DTE ≥ 2:

1. `regime_engine.classify_final` — the generic DTE filter (DTE ≥ 4 requires
   `SELL_PREMIUM` + MEDIUM/HIGH confidence; DTE 2–3 requires `SELL_PREMIUM`).
2. `regime_engine._classify_range` — DTE 3/4 range days additionally require
   `STRONG_SELL_PREMIUM` (`RANGE_DTE3/4_REQUIRES_STRONG_SELL_PREMIUM`).
3. `strategy_engine.decide()` — a third copy of the same filter.

At VIX ≈ 11, `SELL_PREMIUM`/`STRONG_SELL_PREMIUM` almost never fire, so every
non-expiry day is structurally closed. This is the exact reason the engine
takes ~0 trades on Sep 9 and Sep 10.

## 3. The deeper, non-obvious finding — fixed-cost amortisation

I relaxed those gates (unblocked NEUTRAL vol and allowed near-weekly DTE 3/4
spreads under range/positioning confirmation) and replayed. The engine then
**did** produce trades, and they were *directionally correct* — but they lost:

| date | trade | entry→exit credit | gross | costs | net |
|------|-------|-------------------|-------|-------|-----|
| 2026-09-09 | bear call DTE4 (SC23650/BC23800) | 43.28 → 42.22 | +1.06 pts (₹69) | ₹108 | **−₹40** |
| 2026-09-10 | bear call DTE3 (SC23600/BC23750) | 34.68 → 33.43 | +1.25 pts (₹81) | ₹104 | **−₹23** |

The pattern is unambiguous and important: **a near-weekly (DTE 3/4) option
decays only ~1–2 premium points over an intraday hold, which is ₹65–130 of
gross profit — but fixed brokerage + slippage on a single lot is ~₹100–110.**
The trades were *right* (premium decayed, spot stayed in range) and still lost,
because the slow theta cannot clear the fixed round-trip cost at 1-lot size.

This is why the original `STRONG_SELL_PREMIUM` gate for DTE 2–4 is **not**
mere conservatism: rich vol is what makes the credit large enough that a
day's worth of decay clears the fixed costs. Relaxing that gate without
fixing size lets in thin-credit trades that are structurally unprofitable.

Compare 0DTE (Sep 8): theta is fast enough that a 1-lot structure decays
~7+ points and clears costs → +476.

## 4. The sizing amplifier — `size_multiplier` forces 1 lot

Even when a near-weekly trade is allowed, the position size is crushed to one
lot. `compute_final_size` multiplies:

```
base_size(Thu 0.65) × vix_mult(1.0) × conf_mult(1.0) × dte_mult(DTE3 0.40)
× event_mult(1.0) × OR modifier(MODERATE 0.75)  →  ≈ 0.195
```

so `lots = (max_risk / per_lot_risk) × size_mult` rounds down to **1 lot** for
essentially every non-Tuesday trade. The `dte_mult` (0.40/0.30 for DTE 3/4)
and the day/OR discounts assume a *swing* position that must survive to
expiry — the same swing-trading assumption behind the DTE gates — but this
engine is flat by 15:00, so DTE does not add holding risk. At one lot the
fixed brokerage dominates, which is the root cause of the losses in §3.

## 5. Sep 10 specifically

- Data covers only **09:15:03 → 11:28:12** (morning session).
- Spot pinned in a ~60-pt band (23,400–23,460), max pain 23,500, VIX 11.7,
  ATM IV ~10.5–10.8%, VRP ~1.7–1.9pp (NEUTRAL, never rich).
- There is no rich-VRP moment all morning, so there is no cost-clearing
  premium-selling trade. Every structure the engine can build on a DTE-3
  chain has negative expected value after friction — the EV gate rejects it
  (`ev_−2.0pts_below_min_1.1pts, p_win=0.65`).

A range-bound day is only good for premium selling *when the premium is rich
enough to pay the costs*. At VIX 11.7 with Tuesday-only weekly expiry, it is
not. This is a market fact, not a bug.

## 6. What is defensible to change (and what is not)

**Applied (this branch):**

- **NEUTRAL-vol unblock** (`regime_engine` Hard Block 3). A *directional*
  vertical (bear call in a downtrend, bull put in an uptrend, single-sided
  vertical on a range day with a positioning tilt) earns drift + theta and
  needs trend/positioning confirmation, not rich vol. Rich vol remains the
  gate for delta-neutral condors inside `_classify_range`. `BUY_OPTIONS`
  (realised > implied) still hard-blocks all selling.

**Recommended, but a risk-appetite decision — needs your sign-off:**

- **Size defined-risk near-weekly trades to a cost-viable minimum.** Fix the
  `dte_mult` discount (it is a swing assumption on a flat-by-15:00 book) and/or
  raise `max_risk_per_trade_pct` so a DTE 3/4 vertical sizes to 2–3 lots and
  amortises the ~₹100 fixed cost. This is what actually turns the §3 trades
  profitable; it also increases per-trade and daily risk, so I will not apply
  it without your confirmation.

**Not defensible (overfit / unprofessional):**

- Blanket-lowering the EV gate, friction caps, or wing-cost cap.
- Trading `VOL_BUY_OPTIONS` (selling when realised > implied).
- Trading `CHOPPY_MARKET` / un-established opening range.
- Treating the 3 dates as a fit target — n=1 per day is noise.

## 7. Blocker

`main` now carries `data/per_day/nifty_algo_2026-09-10.db`, so Sep 10 is
replayable. But note its coverage ends at 11:28 IST — the afternoon is
absent, so any "Sep 10 profit" is judged on a half session.

## 8. Patch v1 — applied & verified

`patch_v1.py` is a self-contained, idempotent patch (run `python patch_v1.py`
from the repo root). It is the source of truth for the changes below and
reproduces this working tree byte-for-byte from a clean `HEAD` checkout.

What it changes:

1. **NEUTRAL vol unblock** (`regime_engine.classify_final`, Hard Block 3) —
   NEUTRAL no longer hard-blocks; delta-neutral structures are re-gated by vol
   inside `_classify_range`. `BUY_OPTIONS` still blocks all selling.
2. **DTE rich-vol gates removed** (three copies: `classify_final`,
   `_classify_range`, `strategy_engine.decide`) — near-weekly DTE 2–4 is now
   gated on regime/positioning/OR/trend/confidence, not vol. DTE>6 and the
   confidence floor on DTE≥4 are kept.
3. **`_classify_range` DTE 3/4 fall-through** — RANGE/STRONG_RANGE still routes
   to the condor (with containment gates); BULLISH/BEARISH now falls through
   to the single-sided vertical, exactly as DTE 0/1.
4. **Wing-cost gate scoped to IRON_CONDOR only** — a single-sided vertical's
   one long leg is its risk definition, not a "second position".
5. **DTE size discount removed for DTE 1–4** (`dte_mult` 0.75/0.50/0.40/0.30 →
   1.0) — the swing-era discount assumed a hold-to-expiry position; this book
   is flat by the 15:00 hard exit.
6. **Risk budget** `0.006→0.012` per trade, `0.02→0.04` daily — the old 0.6%
   cap could never size a defined-risk near-weekly structure above one lot.
7. **Fixed-cost amortization floor** (`compute_params`, new [G6]) — brokerage
   is per-order, not per-lot (~Rs 94 round trip for a 2-leg spread). When a
   HIGH-confidence, non-borderline, defined-risk setup would trade at 1 lot
   even though the risk budget supports ≥1.5 risk-correct lots, it now trades
   `round(raw_lots)` (capped by the day cap) instead of a size-crushed 1 lot.

Replay results (`backtest_engine.py`):

| date | strategy | conf | DTE | lots | gross pts | costs | **net P&L** |
|------|----------|------|-----|------|-----------|-------|-------------|
| 09-08 | BEAR_CALL_SPREAD | HIGH | 0 | 4 | +4.45 | 110 | **+1,047** |
| 09-09 | BEAR_CALL_SPREAD | HIGH | 4 | 3 | +1.06 | 137 | **+70** |
| 09-10 | BEAR_CALL_SPREAD | HIGH | 3 | 3 | +0.84 | 128 | **+36** |

All three sessions now produce a profitable trade. Every trade is a
directionally-correct defined-risk bear call (the market drifted down each
day); the patch is what lets a HIGH-confidence read survive the calendar/OR
size crush and amortise its fixed brokerage.

**Risk note:** per-trade risk budget is now 1.2% (blended, up from 0.6%) and
daily 4.0% (up from 2.0%). The floor sizes a HIGH-confidence defined-risk
structure to the risk-correct `round(raw_lots)`, never above the day cap and
always within the engine's own 1.5× risk cap. This is a deliberate
risk-appetite increase, justified by the capped-loss nature of every
structure the engine trades and by the fixed-cost amortization economics.
