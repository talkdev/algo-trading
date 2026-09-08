# NIFTY Intraday Options Engine — Profitability & Accuracy Review

**Scope:** `strategy_engine.py`, `regime_engine.py`, `main.py`, `execution_engine.py`,
`eod_report.py`, `data_engine.py`, `core.py`, `calibration_engine.py`, `backtest.py`
(21,227 lines). Reviewed **only** for edge, expectancy and measurement accuracy — not
for crashes or code defects.

**Method:** every claim below was either read directly out of the source (file:line
given) or produced by executing the engine's own formulas. Reproduce with:

```
python3 analysis/mc_engine_trade.py     # engine's trade, benign world
python3 analysis/mc_realistic.py        # + jumps / IV expansion / stress spreads
python3 analysis/sweep_fair.py          # honest VRP, realised vol held constant
python3 analysis/parkinson_bias.py      # is the VRP input even real?
python3 analysis/friction_budget.py     # cost per structure
python3 analysis/sizing_margin.py       # sizing gate + 2026 margin
```

---

## 0. Verdict

The architecture is sound and unusually disciplined: regime gating, defined risk only,
BUY-legs-first sequencing, phantom-trade logging, Bayesian shrinkage in calibration,
15:00 exit ahead of the 15:00–15:30 settlement window. Those are professional choices.

But on the question asked — *will this make money in real NIFTY intraday conditions* —
the answer is **no, and it will not even trade**. There are two independent gates that
reject 100% of the realistic parameter space, and beneath them the strategy's economics
are negative at the VRP levels the engine is configured to accept.

| # | Finding | Severity |
|---|---|---|
| 1 | EV gate is mathematically unsatisfiable — rejects every trade | Blocking |
| 2 | Position-sizing gate blocks iron condors below ~₹31L capital | Blocking |
| 3 | VRP input is biased high by ~0.8–1.5pp — the core signal is not measuring what it claims | Fatal to edge |
| 4 | Effective VRP threshold falls to ~1.1pp, where expectancy is negative | Fatal to edge |
| 5 | Exit ladder is a short-gamma "sell low" machine — 48% of exits are delta stops | Major |
| 6 | 0DTE margin understated 6.5× (SEBI expiry-day ELM ignored) | Major |
| 7 | `backtest.py` is not a backtest — no out-of-sample validation exists anywhere | Major |
| 8 | Calibration can retune thresholds on 5 days / 20 trades | Major |

Rules encoded for 2026 are **correct** and were verified: Tuesday weekly expiry
(SEBI/NSE, effective Sep 2025), lot size **65** (effective Jan 2026 series), STT on
options sell **0.15%** (Budget 2026, from 1 Apr 2026), SEBI turnover 0.0001%, stamp
0.003%. Exchange txn is set to 0.03552% vs the actual NSE 0.03503% — trivially
conservative. Spot ~23,900 and India VIX ~11 match the engine's assumed environment.

---

## 1. Blocking: the EV gate rejects everything

`strategy_engine.py:836–887`

```python
reward_pts = net_credit * target_pct     # 0.50 on DTE0
risk_pts   = net_credit * 2.5            # <-- full stop *level*, not the loss
ev = p_win * reward_pts - (1 - p_win) * risk_pts - friction
```

Reward is a **fraction** of credit; risk is **2.5× the whole credit**. Solving
`ev > 0` requires `p_win > 2.5 / (2.5 + target_pct)`:

| DTE | target | breakeven p_win | best p_win in engine's own table | result |
|---|---|---|---|---|
| 0 | 0.50 | **0.833** | 0.76 | impossible |
| 1 | 0.42 | **0.856** | 0.72 | impossible |
| 2 | 0.30 | **0.893** | 0.68 | impossible |

`p_win_table` (lines 856–865) tops out at 0.72 + 0.04 VRP bonus = 0.76. Sweeping every
DTE × OR-condition × VRP combination: **0 of 27 pass.** The engine as shipped enters no
trades — every candidate dies at `ev_gate:` and is logged as a phantom.

Two separate errors are stacked here:

1. **Wrong risk quantity.** `stop_premium = net_credit * 2.5` (line 1099) is the premium
   *level* at which you exit. The loss is `2.5C − C = 1.5C`, not `2.5C`. Risk is
   overstated 67%.
2. **Wrong probability.** Even corrected to 1.5C, breakeven p_win is 0.75 / 0.78 / 0.83 —
   still failing DTE1 and DTE2. And the p_win table is a **terminal** (expire-OTM)
   probability, while the engine exits on **touch**. For a barrier at distance *d*,
   first-passage probability ≈ 2 × terminal probability. The true probability of
   *reaching the target before being stopped* is far below the table.

The honest reward:risk actually configured is **1 : 3.0 (DTE0), 1 : 3.6 (DTE1),
1 : 5.0 (DTE2)** — win 0.30–0.50C, lose 1.50C. That geometry needs a 75–83% hit rate.
A 0.29-delta short strangle does not have one.

## 2. Blocking: sizing makes the primary structure unreachable

`strategy_engine.py:1063–1098`. `risk_pct` is 0.5% of capital on DTE0;
`structural_loss_per_lot = (wing − credit) × 65 = (150 − 33) × 65 = ₹7,605`.
Iron condors are hard-rejected unless `raw_lots × size_mult ≥ 1.0`, then forced to 2 lots.

| capital | DTE | max_risk | raw_lots | outcome |
|---|---|---|---|---|
| **₹10,00,000** (shipped default) | 0 | ₹5,000 | 0.66 | **blocked** |
| ₹10,00,000 | 1 | ₹4,000 | 0.53 | **blocked** |
| ₹31,20,000 | 0 | ₹15,600 | 2.05 | 2 lots |

And `size_mult` enters at `max(signals.size_multiplier, 0.10)` with a 0.50 default
(line 1329), so in practice you need roughly **₹60L+** for the flagship structure to
clear its own sizing gate. The shipped configuration cannot trade its main strategy.

## 3. Fatal to edge: the VRP signal does not measure VRP

Everything gates on `VRP = ATM_IV − Parkinson_RV` (`data_engine.py:1163–1220`). Three
independent problems make that number unusable at the precision it is being used to.

**(a) Parkinson on 1-minute bars is biased low.** The estimator assumes a continuously
observed high–low range. A 1-minute bar is built from finitely many ticks, so the
observed range is always ≤ the true range. Measured against a known 10.50% ground truth
using the engine's exact formula over 60 bars:

| ticks/min | mean RV | bias | s.d. |
|---|---|---|---|
| 5 | 7.96% | **−2.54pp** | 0.46 |
| 20 | 9.06% | **−1.44pp** | 0.44 |
| 60 (≈1/sec index dissemination) | 9.66% | **−0.84pp** | 0.44 |
| 1000 | 10.26% | −0.24pp | 0.44 |

NIFTY spot is a 50-stock weighted average disseminated roughly per second, so the
realistic bias is **−0.8 to −1.5pp**. RV low ⇒ **VRP high** ⇒ the engine systematically
sees premium richness that is not there.

**(b) Sampling noise alone (±0.44pp, 1 s.d.) is a third of the decision threshold.**
The 60-bar window is one hour of data.

**(c) The IV leg goes stale exactly when it matters.** `compute_atm_iv`
(`data_engine.py:1380–1393`) returns `None` whenever ATM IV / VIX falls outside
0.60–2.00. On Tuesday 0DTE the expiring contract's annualised ATM IV routinely runs well
above 2× India VIX — that is normal expiry behaviour, not staleness. The guard fires,
IV returns `None`, and `_compute_vrp_smoothed` falls back to `self._vrp_buffer[-1]`
(line 1181) — a stale reading — during the engine's primary trading window.

Net: the sole gate that is supposed to establish edge is biased in the direction that
manufactures false edge.

## 4. Fatal to edge: the threshold is set below where money is made

`regime_engine.py:1080–1108` multiplies the base threshold down:

```
DTE0        × 0.75
VERY_NARROW × 0.75
→ 2.0 × 0.75 × 0.75 = 1.125pp   (floor 1.0)
```

So the engine will call `SELL_PREMIUM` at a **measured** 1.13pp — which, after the
−0.8 to −1.5pp measurement bias, is a **true VRP of roughly zero or negative**.

Monte Carlo, realised vol pinned at 10.5% in every row, IV set honestly to
`realised + VRP`, engine's own strike and exit logic, 2026 costs, per lot:

| true VRP | path | win% | **EV/lot** | worst |
|---|---|---|---|---|
| +1pp | smooth | 37.7% | **−₹129** | −₹1,358 |
| +1pp | jumpy | 48.2% | **−₹168** | −₹4,219 |
| +2pp | smooth | 39.7% | **+₹8** | −₹1,089 |
| +2pp | jumpy | 48.0% | **−₹41** | −₹3,724 |
| +4pp | smooth | 47.3% | **+₹194** | −₹880 |
| +4pp | jumpy | 51.4% | **+₹169** | −₹2,833 |

**Breakeven is ~+2pp true VRP on a smooth path and ~+2.5pp with realistic jumps.**
The engine's effective gate of 1.1–1.9pp *measured* sits well inside the losing region.

Note the jump column: same realised vol, only the path shape differs, and it costs
₹50–170/lot and roughly **triples the worst case**. Short gamma is not compensated for
path shape anywhere in this engine.

## 5. Major: the exit ladder sells at the worst moment

`execution_engine.py:1053–1097`. Entry is at ~**0.29 delta** (the engine's own 09:50
pick is spot ±100pts after the `sqrt(time-remaining)` shrink and 50-pt rounding), and
Priority-1 closes the position at **0.40 delta**. That is an 0.11-delta band. Priority-2
closes when spot comes within **40pts** of a short strike — on a ~157pt daily sigma that
barrier is ~0.38 SD away, which is touched most days.

Realistic-path exit distribution: **DELTA 48%**, PRICE_STOP 22%, TARGET 23%,
PROXIMITY 7%. Half of all trades are closed by a delta stop — and delta rises both from
spot moving *and* from IV expanding, so the stop fires precisely when buying the short
back is most expensive.

Critically, **retuning the exits does not fix it.** Full sweep at +2pp VRP
(`sweep_fair.py`), jumpy path:

| configuration | win% | EV/lot |
|---|---|---|
| engine: 1.0× dist, 40pt prox, 0.40 delta, 50% target | 48.0% | −₹41 |
| proximity stop removed | 48.0% | −₹41 |
| delta stop widened to 0.60 | 48.0% | −₹41 |
| strikes 1.5× out (~0.15 delta) | 66.6% | −₹47 |
| 1.5× out, premium stop only, 35% target | 77.0% | −₹89 |
| 1.5× out, premium stop 1.75×, 30% target | 77.6% | −₹107 |

Win rate moves from 48% to 78%; **expectancy does not improve** — moving strikes out
buys hit-rate with credit you no longer collect. This is the key strategic result:
**the entry edge (VRP) is the only lever that matters. Exit tuning is redistribution.**

Friction is *not* the villain, incidentally — round-trip cost + slippage is only
**7.7–16.7%** of gross credit depending on strike distance (`friction_budget.py`). The
loss is coming from the risk geometry, not the brokerage.

## 6. Major: 0DTE margin understated 6.5×

`strategy_engine.py:1106`: `margin_per_lot = wing × 65 × 1.10` = **₹10,725**.

SEBI's index-derivatives framework levies an **additional 2% ELM on short options on
expiry day** (in force since Nov 2024), charged on contract notional, applying to
positions held at start of day *and* opened intraday for that day's expiry:

| component | ₹/lot |
|---|---|
| defined-risk SPAN component | 7,605 |
| additional ELM, 2% × (23,900 × 65) × 2 short legs | 62,140 |
| **realistic 0DTE margin** | **69,745** |
| engine estimate | 10,725 |
| **understatement** | **6.5×** |

Consequences: return on margin at the 50% target is **1.5%, not 10%**; and with
`MAX_CONCURRENT_POSITIONS=1`, 2 lots on expiry day need ₹1.39L — a ₹10L account is
nowhere near the ₹31L the sizing gate already demands. This is the single most
expensive real-world omission, because 0DTE Tuesday is where the engine is designed to
concentrate.

## 7. Major: there is no validation

`backtest.py:207 replay_day()` reads `cycle_log`, `trade_entries`, `trade_exits`,
`positions` **out of the engine's own database**. It reports on trades already taken. It
is a P&L attribution tool, not a backtest — there is no historical option-chain replay,
no walk-forward, no out-of-sample split anywhere in 21,227 lines. Nothing in this
repository can answer "is this profitable" before real money is at risk. The file name
implies otherwise, which is how a losing strategy gets funded.

## 8. Major: calibration will overfit

`MIN_TRADING_DAYS_FOR_CALIBRATION=5`; `_apply_dte_feedback` acts on `len(pnls) >= 5`
trades; `_run_vrp_calibration` picks a threshold from the first VRP bucket clearing 55%
shrunk win rate. At 1–3 trades/day, a 5-day sample is ~10 trades. Bayesian shrinkage is
present and correct in principle, but with a 78% win-rate strategy you need **300+**
trades to distinguish a 78% edge from a 70% one. Retuning `vrp_sell_threshold`
downward off a lucky fortnight will push the gate further into the losing region
identified in §4 — the feedback loop runs the wrong way.

---

## Prioritised changes

### Tier 1 — required before the engine can trade at all

1. **Rewrite the EV gate.** Use actual loss, not stop level: `risk = (stop_mult − 1) × C`.
   Replace terminal `p_win` with touch-adjusted probability
   `p_touch ≈ 2·N(−d/(σ√T))` computed from live IV and the actual barrier
   (short strike − proximity), not a hard-coded table.
2. **Fix the reward:risk geometry.** 1:3 with a 60–70% hit rate is negative. Either
   raise the target toward 60–70% of credit *or* cut the stop to ~1.5× credit, and
   verify `p_win > risk/(risk+reward)` holds with the corrected probability.
3. **Fix sizing.** Size on `min(defined risk, realistic stop loss)` rather than full
   structural width, and drop the hard 2-lot minimum for iron condors — or state a
   realistic minimum capital (~₹35–40L) in the config.

### Tier 2 — required for the edge to be real

4. **Debias the RV estimator.** Apply a scaling correction calibrated against a
   known-vol simulation (≈1.10–1.35× for 1-min NIFTY bars), or switch to a
   Rogers–Satchell / Garman–Klass blend, or compute realised variance from 5-second
   returns. Widen the window from 60 bars to 90–120 to halve sampling noise.
5. **Fix the ATM IV guard.** The 0.60–2.00 IV/VIX band is invalid on 0DTE. Make the band
   DTE-aware, or compare the 0DTE ATM IV against the *previous session's* 0DTE ATM IV.
   Never silently fall back to a stale `_vrp_buffer[-1]` inside the trading window —
   no VRP should mean no trade.
6. **Raise the VRP floor to where money is actually made.** Post-debias, require
   **≥ 2.5pp true VRP** for DTE0 and 3.0pp for DTE1, and delete the `× 0.75` DTE0 and
   `× 0.75` VERY_NARROW discounts — they push the gate straight into the loss zone.
   The absolute floor of 1.0pp should be 2.0pp.
7. **Add a term-structure / IV-percentile filter.** VRP alone is one number. Professional
   desks additionally require IV rank/percentile above a floor and a non-inverted
   front-end term structure before selling. Both are computable from the chain already
   being fetched.

### Tier 3 — risk shape

8. **Replace the delta stop with a spot-based structural stop.** A 0.40-delta stop on a
   0.29-delta entry is noise-triggered. Use a fixed spot barrier expressed in
   sigma units (e.g. 0.8 × expected remaining move) and let the wings define the tail.
9. **Model stress liquidity.** `_compute_slippage` uses 1.5× half-spread on exit; NIFTY
   0DTE OTM spreads widen 2.5–4× when a stop fires. Use 3× for stop exits and keep 1.5×
   for target exits — this alone changes which trades pass the gate.
10. **Add the expiry-day ELM to the margin model** and gate entries on real available
    margin, not `wing × 65 × 1.10`.
11. **Prefer 2-leg credit spreads over 4-leg condors when directional confidence exists.**
    Half the legs, half the brokerage/slippage, half the ELM. The condor's second short
    leg only pays when the range genuinely holds.
12. **Tighten the monitoring loop on 0DTE.** 45s between cycles (`REGIME_CALC_INTERVAL_SEC`)
    is ~7.4pts of 1-s.d. spot drift per cycle, and far more in a fast tape — that is
    pure slippage on every stop.

### Tier 4 — validation

13. **Build a real backtester.** Store the full option chain per cycle (the schema is
    already close), then replay entry/exit logic against historical chains with a fill
    model. Rename the current file to `performance_report.py`.
14. **Raise calibration minimums** to ~60 trading days / 150+ trades before any threshold
    moves, and never allow calibration to *lower* the VRP gate below the Tier-2 floor.
15. **Keep the phantom-trade log — it is the best asset here.** Once the EV gate is
    fixed, phantom outcomes are the cheapest way to measure whether the gate is
    admitting the right trades.

---

## What a professional NIFTY 0DTE desk does differently

- **Sells the expiry-day theta cliff, not the morning.** The engine enters 09:50–13:00.
  On Tuesday the reliable decay is 12:30–15:00; before noon you are paying gamma for
  theta you have not earned yet.
- **Requires vol richness in *percentile* terms, not absolute points.** VIX 11 with
  IV rank 15 is not a sell; VIX 11 with IV rank 70 is.
- **Sizes off real margin and treats expiry-day ELM as a first-class cost of capital.**
  ROC at 1.5% per trade with a −₹4,000 tail is not a business.
- **Never uses a delta stop inside 0.15 delta of entry.** Stops go where the thesis is
  wrong, not where the greeks wobble.
- **Runs the sold structure to a defined exit time with wings as the stop**, accepting
  a lower hit rate and a much better average loss — which is the opposite of the
  1:3 geometry configured here.
- **Validates on ≥2 years of chain data before funding.** With ~1 trade/day, statistical
  significance takes a year of live trading; nobody discovers a 78%-vs-70% difference
  from 10 trades.

---

## Bottom line

This is well-built infrastructure wrapped around an edge that has not been established.
Fix the two blocking gates (§1, §2) and the engine will start trading — but it will
trade a negative-expectancy strategy, because the signal that authorises every trade
(§3) is biased in the direction that invents edge, and the threshold (§4) sits below
breakeven. The measurement fix and the VRP floor are worth more than every exit-rule
change combined: at true VRP +4pp the same structure and the same exits earn **+₹169/lot**;
at true VRP 0pp they lose **−₹539/lot**. Everything else is detail.
