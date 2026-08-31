# NIFTY Options Trade Engine — Code Audit (Round N)
**Date:** 2026-08-31 · **Scope:** `config.py`, `data_manager.py`, `regime_engine.py`, `strategy_engine.py`, `main.py` (13,485 LOC)
**Lens:** profitability first — every finding is scored by how much it costs you in rupees, not by code aesthetics.

---

## Executive Summary

The engine is structurally sound and previous audit rounds fixed most unit-scaling bugs. **The remaining problems are no longer crashes — they are edge-destroyers.** The single most important finding of this round:

> **The two strategies that will trade most often (Iron Condor and Credit Spreads) currently have a structurally negative expected value at their configured minimum credits.** No amount of execution polish fixes a negative-EV structure. This is finding **SE-01 / CFG-01** below and should be treated as the top priority.

Second most important: **the regime engine is mathematically incapable of reaching `STRONG_SELL_VOL`** given the persistence filter and score quantization, so the engine's highest-conviction, highest-allocation state is dead code (**RE-01**).

| Severity | Count | Meaning |
|---|---|---|
| CRITICAL | 6 | Directly loses money or blocks profitable trading |
| HIGH | 12 | Materially degrades edge, sizing, or exit quality |
| MEDIUM | 14 | Signal accuracy / correctness drift |
| LOW | 9 | Hygiene, maintainability, latent risk |

---

# 1. `config.py`

### CFG-01 — Minimum credit thresholds create negative-EV structures
**Severity: CRITICAL · Impact: This is the primary reason the engine will bleed.**

`CONDOR_MIN_CREDIT = 40` on `CONDOR_WING_WIDTH = 400` is a **credit/width ratio of 0.10**. The condor is placed at 1.0σ (`CONDOR_SIGMA_MULTIPLIER = 1.0`), so P(expire inside) ≈ 68%.

```
EV/lot = 0.683 × 40  −  0.317 × (400 − 40)  =  −86.9 points/lot
       = −₹5,650 per lot, per trade, before costs
```

With the 50% profit target it is *worse* (−100.6 pts), because you cap the win at 20 pts while keeping the full 360-pt tail.

Credit spreads are worse still. `SPREAD_MIN_CREDIT = 25` at `SPREAD_DELTA_SHORT = 0.30`:
```
width ≈ 300, risk = 275, R:R = 11:1 against you
EV (one side) = 0.40 × 25 − 0.60 × 275 = −155 points
```
A 0.30-delta short needs roughly a **70% win rate just to break even at 1:11** — and 0.30 delta implies ~30% ITM probability *before* accounting for touch probability (~60%).

**Fix (profitability-critical):**
- Require credit ≥ **20–25% of wing width**, not an absolute point value. For a 400-wide condor that is **80–100 points minimum**.
- Replace absolute constants with a derived rule:
  ```python
  CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.22   # -> 88 pts on a 400 wing
  SPREAD_MIN_CREDIT_PCT_OF_WIDTH = 0.25
  ```
- If the market never offers that credit at VIX ≈ 11, **the correct action is to not trade**, not to lower the threshold. The prior audit notes show these were lowered repeatedly ("15 → achievable at VIX=11") specifically to make trades fire. That optimization was for *trade count*, not for *profit*.
- Alternatively widen the short strikes to 1.5σ and accept a smaller credit — but then the credit/width rule still must hold.

---

### CFG-02 — `MAX_RISK_PER_TRADE_PCT = 0.08` with `MAX_DAILY_LOSS_PCT = 0.03`
**Severity: CRITICAL · Impact: One max-risk loss = 2.7× the daily loss limit.**

A single trade is permitted to risk 8% of capital (₹80,000) while the daily circuit breaker fires at 3% (₹30,000). The daily CB cannot protect you — it triggers only *after* a position has already blown through it. Worse, `CB_LEVEL_1_PCT` (2%) is *below* the per-trade allowance, so CB L1 will fire on nearly every trade that moves against you, making circuit-breaks the dominant exit reason instead of the designed stop ladder.

**Fix:** `MAX_RISK_PER_TRADE_PCT = 0.015`–`0.02` (₹15–20k). Keep `MAX_DAILY_LOSS_PCT = 0.03` so you survive two full losers per day. This single change is the highest-leverage risk fix in the file.

---

### CFG-03 — `MAX_DAILY_LOSS`, `MAX_DRAWDOWN`, `STATIC_STOP_PCT`, `SL_*` band are computed but never read
**Severity: HIGH · Impact: A documented risk framework that does not exist at runtime.**

Verified unused across all four consuming modules:
`MAX_DAILY_LOSS`, `MAX_DAILY_LOSS_PCT`, `MAX_DRAWDOWN`, `MAX_DRAWDOWN_PCT`, `STATIC_STOP_PCT`, `SL_BASE_PERCENT`, `SL_REFERENCE_VIX`, `SL_MIN_PERCENT`, `SL_MAX_PERCENT`, `TRANSACTION_COST_PCT`, `MAX_RISK_PER_TRADE_PCT`, `MAX_COMBINED_RISK_PCT`.

The `SL_*` group in particular describes a **VIX-scaled stop-loss** (`SL_BASE_PERCENT = 0.30` at `SL_REFERENCE_VIX = 14`, clamped 0.18–0.40). This is a genuinely good idea for profitability — stops should be wider when vol is high — and it is entirely absent. Every strategy instead uses a flat `2.0 × credit`.

**Fix:** Implement VIX-scaled stops:
```python
sl_pct = clamp(SL_BASE_PERCENT * (vix / SL_REFERENCE_VIX), SL_MIN_PERCENT, SL_MAX_PERCENT)
```
At VIX 11 this gives a *tighter* 0.236 stop; at VIX 20 a wider 0.40. Flat 2× credit stops out good trades in calm markets and holds losers too long in volatile ones.

---

### CFG-04 — 75 unused constants; several represent dead strategy logic
**Severity: HIGH · Impact: Deltas/thresholds you believe are active are not.**

Notable dead parameters that change strategy behaviour:
- `BUTTERFLY_DELTA_A/B/C` (0.30/0.20/0.10) — the butterfly builder ignores these entirely and hardcodes `wing_width = NIFTY_STRIKE_STEP * 1` (100 pts). You are trading a 100-wide butterfly, not the delta-selected structure documented.
- `SPREAD_ROLL_DELTA_TRIGGER = 0.35` — no roll logic exists. Credit spreads never roll; they only hard-stop or time-exit.
- `CONDOR_ADJUSTMENT_DELTA = 0.35` — no condor adjustment exists.
- `RATIO_DELTA_EXIT_TRIGGER = 0.35` — ratio spread has no delta-based exit.
- `BACKSPREAD_MIN_MOVE_MULTIPLE`, `BACKSPREAD_HEDGE_QTY`, `BACKSPREAD_SHORT_QTY` — hardcoded in the builder.
- `LOW_VIX_DELTA` / `MID_VIX_DELTA` / `HIGH_VIX_DELTA` — the documented VIX-adaptive delta selection **does not exist**. Strike selection is VIX-blind.
- `MIN_PREMIUM_PCT` / `MAX_PREMIUM_PCT` — note these are also **inverted** (`MIN = 0.008 > MAX = 0.006`), which would have failed loudly had they ever been used.
- `EDGE_PERCENTILE_HIGH/LOW`, `SKEW_ZSCORE_FEAR/COMPLACENT`, `STRONG_SELL_THRESHOLD` and all regime thresholds — shadowed by hardcoded duplicates in `regime_engine.py` (see RE-02).

**Fix:** Either wire them up or delete them. The VIX-adaptive delta selection (`*_VIX_DELTA`) is the one with real profit impact — selling 0.25-delta at VIX 11 and 0.15-delta at VIX 20 is materially better than a fixed 0.30.

---

### CFG-05 — Duplicate `WEIGHT_FLOW` assignment (lines 187–188)
**Severity: LOW · Impact: Cosmetic, but a symptom of copy-paste drift in the weight block.**
```python
WEIGHT_FLOW  = 0.15   # reference: 0.15
WEIGHT_FLOW  = 0.15   # <- duplicate
```
The `assert` on line 189 passes only by coincidence. Also note all four weights are unused (RE-02).

---

### CFG-06 — Holiday calendar is 2026-only and unvalidated
**Severity: MEDIUM · Impact: Trading on a closed day, or blocking a trading day.**

`NSE_MARKET_HOLIDAYS` contains only 2026 dates. On 1-Jan-2027 the engine will treat every 2027 holiday as a trading day. `HOLIDAY_CALENDAR_REVIEWED_ON = 2026-08-31` and the preflight warns only after 180 days — by which point you have already traded through holidays.

**Fix:** Hard-fail preflight if `max(holiday_year) < current_year`, not just warn.

---

### CFG-07 — Cost model unverified by own admission; `COST_NSE_IPFT_PCT` is likely wrong
**Severity: MEDIUM · Impact: Understated costs inflate backtest/paper P&L.**

The file itself flags every `COST_*` rate as "supplied externally, NOT independently confirmed." `COST_NSE_IPFT_PCT = 1e-9` (₹0.01/crore) is implausibly small — the actual NSE IPFT for F&O is ₹10/crore (1e-6), which the comment explicitly *reverted*. The reverted value was probably the correct one.

`COST_BROKERAGE_PER_ORDER = 20.0` is also flagged as an assumption. At current sizing a 4-leg condor round trip costs ₹189 in brokerage+GST alone = **2.9 points/lot** — meaningful against a 40-point credit (7% of gross).

**Fix:** Verify IPFT against a live contract note before going live. Budget ~6–8 points/lot round-trip friction for a condor and require credit thresholds well above it.

---

### CFG-08 — `LOT_SIZE = 65` flagged as unverified
**Severity: MEDIUM · Impact: Every rupee figure in the engine is wrong if this is wrong.**

Config's own comment says the NSE circular is "user-supplied verification, NOT independently confirmed." A wrong lot size silently mis-scales P&L, margin, costs, and every risk gate.

**Fix:** Read lot size from the Upstox instrument master at startup and assert it matches the constant. Fail loudly on mismatch.

---

### CFG-09 — `PERSISTENCE_READINGS = 3` unused; regime engine hardcodes 3 anyway
**Severity: LOW.** Docstring claims it was patched to 2 for faster confirmation, value is 3, and `regime_engine._persist()` ignores it entirely (hardcoded `if len(buf) > 3` / `len(buf) == 3`).

---

# 2. `data_manager.py`

### DM-01 — Bid/ask are **never updated by the WebSocket** — only by the 60s REST poll
**Severity: CRITICAL · Impact: Every stop, target, trailing stop, and paper fill runs on up-to-60-second-stale quotes.**

Confirmed: `_update_instrument_ltp()`, `_update_instrument_greeks()`, `_update_instrument_oi()` write `ltp`, greeks, `iv`, and `oi`. **No code path anywhere writes `bid` or `ask` outside `fetch_option_chain()`** (the 60s REST refresh). Grep for assignments to `["bid"]` returns zero hits in the WS handlers.

This is severe because recent (correct) patches switched `_get_position_value()`, `_get_position_current_premium()`, `_update_all_pnls()`, and `_simulate_fill()` to **prefer bid/ask midpoint over LTP**. The intent was good — avoid stale single prints — but the effect is the opposite: the engine now preferentially uses the *stalest* data source and only falls back to the live-ticking LTP when bid/ask are zero.

Consequences:
- Stop-losses fire up to 60s late. In a gap move that is the difference between a −1× and a −3× credit exit.
- Profit targets are missed — price touches target intra-minute, midpoint never sees it.
- Paper-mode fills are priced off a stale mid, making paper results systematically better than live.

**Fix (highest-value data change in this file):**
1. Subscribe options in `full_d5` mode (`config.WS_MODE_FULL` — defined but unused) instead of `option_greeks`, and parse the depth block to update bid/ask on every tick.
2. Until then, add a staleness guard: if `_rest_ts` is older than ~15s, prefer LTP over midpoint rather than the reverse.
3. Track `_quote_ts` per leg and refuse to act on a stop/target decision from a quote older than N seconds.

---

### DM-02 — `_append_daily_return()` computes a *tick-to-tick* return, labels it *daily*
**Severity: CRITICAL · Impact: The Edge module — 30% of the regime score — runs on a garbage RV.**

```python
def _append_daily_return(self):
    if self._last_return_date == today: return
    self.log_returns.append(np.log(self.spot / self.prev_spot))
```
`self.prev_spot` is the spot from the **immediately preceding poll (~60 seconds earlier)**, not yesterday's close. The first poll of each day therefore appends a 60-second return into a series that `compute_realized_vol()` annualizes with `√252`.

Result: RV is understated by roughly `√(60s / 1 day)` ≈ **an order of magnitude too low**. A too-low RV makes `IV − RV` look large and positive → `edge_score = +1` ("RICH, seller edge") **permanently**, in all conditions.

This is a persistent, one-directional bias pushing the engine toward selling premium regardless of whether premium is actually rich. It is exactly the failure mode that produces a good-looking equity curve followed by one catastrophic loss.

Mitigating factor: `compute_realized_vol()` prefers `candles_daily` when ≥20 daily candles exist, so this path is a fallback. But it *is* the path used during the first ~20 sessions and any time the daily candle fetch fails — and `get_estimated_rv()`'s fallback (`VIX × 0.70`) has the same directional bias.

**Fix:** Compute daily returns from `candles_daily` closes only. Delete `log_returns`/`_append_daily_return` entirely, or store the previous *session close* rather than the previous poll.

---

### DM-03 — `compute_net_flow()` sums the entire snapshot buffer, not a delta
**Severity: HIGH · Impact: Flow score (15% weight) is noise; `dm.net_flow` is unused anyway.**

```python
for snapshot in list(self.oi_snapshots):      # ALL snapshots
    net_flow += data["call_delta_oi"] + data["put_delta_oi"]
```
Each snapshot already contains a per-cycle OI *change*. Summing all of them cumulates every change since the buffer filled — producing a slow-drifting integral, not a 15-minute flow reading. Same problem in `compute_spread_ratio()`, which compares current spread to an average that includes the current value.

Also: `dm.net_flow` and `dm.spread_ratio` are computed every cycle in `main.py` and **consumed by nothing** — `regime_engine._module_flow()` maintains its own independent, correctly-differenced snapshot logic. This is pure wasted API budget and CPU.

**Fix:** Delete `compute_net_flow`, `compute_spread_ratio`, `fetch_oi_snapshot`, and their `main.py` call sites. The regime engine's implementation is the correct one.

---

### DM-04 — `compute_iv_rank()` returns a hardcoded `55.0` when history is short
**Severity: HIGH · Impact: Silently opens the NEUTRAL-regime gate for the first ~10 sessions.**

`_should_enter_new_position()` blocks NEUTRAL entries when `iv_rank <= 50`. A cold-start default of `55.0` is *just above* that gate, so a brand-new deployment with zero IV history will happily enter iron condors in NEUTRAL regime on fabricated data. `_select_strategy()` uses the same `iv_rank > 50` test.

**Fix:** Return `None` on insufficient history and treat `None` as "block entry." Never let a placeholder satisfy a risk gate.

---

### DM-05 — In-memory rolling windows are not persisted across restarts
**Severity: HIGH · Impact: Every restart resets the engine's statistical memory.**

`log_returns` (252), `vix_history_20d` (20), `iv_atm_history` (22,500), `iv_rv_spread_history` (60), `skew_history` (60), `bid_ask_spread_3otm` — all `deque`s built at `__init__`, never loaded from SQLite. Only `iv_rank_history` (via `_load_iv_rank_history`) and the regime engine's `_skew_history`/`_flow_snapshots` survive.

After a restart mid-week: `vix_history_20d` is empty → `_build_long_straddle`'s VIX-spike filter is skipped entirely → `_build_defensive_hedge`'s VIX-spike gate is skipped → both strategies fire on no evidence.

**Fix:** Persist and reload these deques alongside the existing regime state.

---

### DM-06 — `_save_daily_iv_close()` only writes after 14:45
**Severity: MEDIUM · Impact: No IV-rank history accumulates if the engine is stopped before 14:45.**

The patch that made this a genuine "close" also made it conditional on `(hour, minute) >= (14, 45)`. Combined with DM-04's `55.0` default, an engine run only in the morning session will never build IV-rank history and will permanently trade on the fabricated default.

**Fix:** Write an intraday value continuously (overwriting), so the last write of any session is the best available close.

---

### DM-07 — Spot spike-rejection can freeze the engine on a genuine gap
**Severity: MEDIUM · Impact: Blind during exactly the move you must react to.**

Both `fetch_spot_and_vix()` and `_update_instrument_ltp()` reject any spot move >5% versus the last value, permanently reverting to the stale value. On a genuine 5%+ gap (Budget day, global shock) the engine keeps a stale spot **forever** — every subsequent real tick is also >5% from the frozen value, so nothing can ever update it. Meanwhile short gamma is exploding.

**Fix:** Reject once, then accept on the second consecutive confirming tick (two-strike rule), and log CRITICAL + halt new entries rather than silently substituting stale data.

---

### DM-08 — `check_margin()` returns `(True, 0.0)` on any exception
**Severity: MEDIUM · Impact: In live mode, an API failure is interpreted as "margin approved."**

Fail-open on a risk check. If the margin endpoint is down or rate-limited, `_pre_trade_checks()` sees `margin_ok = True` and places the order.

**Fix:** Fail closed — return `(False, 0.0)`.

---

### DM-09 — WS instrument-key fallback assigns ticks to the wrong expiry
**Severity: MEDIUM · Impact: Cross-expiry price contamination.**

When `_instrument_map` lookup misses, both `_update_instrument_ltp` and `_update_instrument_greeks` parse the key and assume `expiry = self._active_expiry`. If the tick is for a *later* expiry (tranche 2 positions target expiry+7d), its price is written into the active expiry's chain at the same strike.

**Fix:** Parse the expiry from the instrument key instead of defaulting to active; drop the tick if it cannot be resolved.

---

### DM-10 — `_do_get` retries non-retryable 4xx errors 5 times with exponential backoff
**Severity: MEDIUM · Impact: Up to ~31s stalled per bad request, inside the data-refresh critical path.**

A `400 Invalid Endpoint` or `404` is retried identically to a transient failure. With `RETRY_MAX_ATTEMPTS = 5` and base 1.0s, a single permanent error burns 1+2+4+8+16 = 31 seconds — half a data-refresh cycle.

**Fix:** Only retry 429/5xx/network errors. Fail fast on 4xx.

---

### DM-11 — `MIN_OI_LOTS = 50` filter drops strikes silently, breaking builders
**Severity: MEDIUM.** `fetch_option_chain` skips any strike where both sides have OI < 50. Condor/butterfly builders then hit `if strike not in chain: return (None, {})` and abort with a generic message. The build-failure cooldown (300s) then blocks retries. Net effect: long dead periods with no diagnosis.

**Fix:** Log which specific strike/OI failed, and consider relaxing the filter for strikes within ±5 steps of ATM.

---

### DM-12 — `candles_15m` is an alias to `candles_30m`
**Severity: LOW.** `self.candles_15m = self.candles_30m` — harmless today but a trap for future code that assumes 15m bars.

---

# 3. `regime_engine.py`

### RE-01 — `STRONG_SELL_VOL` is mathematically unreachable
**Severity: CRITICAL · Impact: The engine's highest-conviction state, with the largest capital allocation (20%) and lot cap (8), can never trigger.**

Chain of reasoning:

1. `_persist()` does `raw_int = int(round(raw))` and confirms only when three consecutive readings are *identical integers*.
2. `_module_vol()` returns `0.5 * term_score + 0.5 * skew_score` ∈ `{−1, −0.5, 0, 0.5, 1}`.
3. `int(round(0.5))` = **0** in Python (banker's rounding — verified). So a vol reading of +0.5 (contango, neutral skew — the *normal* premium-selling condition) quantizes to **0**.
4. Composite = `0.30·vol + 0.30·edge + 0.25·trend + 0.15·flow`, where each confirmed term ∈ {−1, 0, 1}.
5. `STRONG_SELL` requires composite **> 0.45**. Reachable combinations: `vol+edge = 0.60`, `vol+edge+trend = 0.85`, `edge+trend = 0.55`, etc.

So it *is* reachable in principle — but it requires **vol=+1 AND edge=+1 simultaneously confirmed for 3 consecutive cycles**, and vol=+1 requires `term_score = +1 AND skew_score = +1` (contango *and* complacent skew z < −1.0).

The blocker: `skew_score` requires `_skew_zscore()` which needs ≥3 *distinct calendar days* of history (`SKEW_MIN_DAYS = 3`), and `_record_skew` stores **one value per day**. Meanwhile `forward_iv` almost always falls back to `VIX/100` (`_compute_forward_iv`), making `t_spread = VIX − VIX = 0` → `term_score = 0` → `vol_score = 0.5·0 + 0.5·skew` ≤ 0.5 → **rounds to 0**.

**Net: `vol` is pinned at 0 essentially permanently**, capping composite at `0.30·edge + 0.25·trend + 0.15·flow = 0.70` in theory but requiring all three to be +1 — and with edge biased to +1 by DM-02, the realistic ceiling is `0.30 + 0.25 = 0.55` only when a trend also confirms. Since `STRONG_SELL` also wants a *range-bound* market for the straddle branch, trend=+1 and straddle-selection are mutually contradictory.

**Fix:**
1. Do **not** round module scores to integers. Keep `vol_score` as a float and change `_persist` to confirm on sign-stability rather than exact-integer equality.
2. Fix `forward_iv` so `term_score` can actually be non-zero (see RE-04 / main.py's `_ensure_term_structure_expiry` — it exists but only runs every 6h and often finds nothing).
3. Recalibrate `STRONG_SELL_THRESHOLD` empirically from `regime_history` in SQLite — you already log every composite. Pick the 85th percentile of observed values.

---

### RE-02 — Every threshold is duplicated between `config.py` and module-level constants; the config values are dead
**Severity: HIGH · Impact: Tuning `config.py` has no effect. Silent misconfiguration.**

| config.py | regime_engine.py (actually used) |
|---|---|
| `TERM_SPREAD_CONTANGO = 0.5` | `TERM_THRESHOLD = 0.5` |
| `SKEW_ZSCORE_FEAR = 1.5` | `SKEW_Z_STEEP = 1.5` |
| `SKEW_ZSCORE_COMPLACENT = −1.0` | `SKEW_Z_FLAT = −1.0` |
| `EDGE_RICH = 5.0` / `EDGE_CHEAP = 0.0` | `EDGE_RICH` / `EDGE_CHEAP` (local) |
| `ADX_TREND_THRESHOLD = 20` | `ADX_TREND = 20.0` |
| `EMA_SLOPE_THRESHOLD = 0.0005` | `EMA_SLOPE_PCT = 0.05` |
| `WEIGHT_VOL/EDGE/TREND/FLOW` | `WEIGHTS = {...}` |
| `STRONG_SELL_THRESHOLD` etc. | hardcoded in `_map_regime()` |
| `PERSISTENCE_READINGS = 3` | hardcoded `3` in `_persist()` |
| `RV_LOOKBACK_DAYS = 20` | `RV_WINDOW = 20` |
| `SKEW_LOOKBACK_DAYS = 60` | `SKEW_HISTORY_DAYS = 30` ← **mismatched** |

The config file's own comment on `ADX_TREND_THRESHOLD` admits this ("regime_engine.py has its own hardcoded ADX_TREND constant, patched separately"). That is a workaround, not a fix — and `SKEW_LOOKBACK_DAYS` (60) vs `SKEW_HISTORY_DAYS` (30) is an actual live divergence.

**Fix:** Delete all module-level duplicates; read from `config` exclusively.

---

### RE-03 — `EMA_SLOPE_PCT` unit mismatch between config and engine
**Severity: HIGH · Impact: The trend filter is ~100× looser than config implies.**

`_module_trend()` computes `slope_pct = slope / spot * 100.0` (a percentage, e.g. `0.35` for 0.35%) and compares against `EMA_SLOPE_PCT = 0.05` (0.05%). Config's `EMA_SLOPE_THRESHOLD = 0.0005` is the *decimal* form of the same 0.05%. The engine is internally consistent, but the config constant is both unused and in different units — a future "fix" that wires config in would loosen the filter 100×.

Separately: `0.05%` of spot over 20 bars of 30-min data is an extremely low bar. At spot 24,000 that is a 12-point EMA drift over 10 hours. **Essentially any market passes this filter.** The trend module reduces to a pure `ADX > 20` test.

**Fix:** Raise to ~0.25–0.40% for 30-min bars, and unify units.

---

### RE-04 — `forward_iv` falls back to `VIX/100`, forcing `term_score = 0`
**Severity: HIGH · Impact: Kills half the vol module (see RE-01).**

`_compute_forward_iv()` needs a loaded expiry in the 30–45 DTE window. `main.py` fetches only `expiries[:3]` each cycle (all weeklies, ≤ ~21 DTE). `_ensure_term_structure_expiry()` exists and scans 28–42 DTE but runs only every 6 hours and returns early on the first success — and if the monthly is not exactly in that window it gives up until the next 6h check.

When it fails: `forward_iv = vix / 100`, so `v_fwd_pct − v_spot_pct = VIX − VIX = 0` → `term_score = 0` always.

**Fix:** Widen the window to 25–50 DTE, run the check every cycle (it's cheap once cached), and — importantly — **do not silently substitute VIX**. Return `None` and let `term_txt` report "n/a" so the degradation is visible rather than masked as a genuine FLAT reading.

---

### RE-05 — `_persist()` holds the last confirmed value indefinitely when data is unavailable
**Severity: HIGH · Impact: A stale bullish/bearish score can persist for hours or across days.**

```python
if raw is None:
    return self._conf[name]     # hold previous confirmed value
```
`self._conf` is persisted to SQLite and reloaded on restart. If the flow module is unavailable at market close (it usually is — it needs 20+ min of intraday snapshots), yesterday's `flow = +1` is reloaded at 09:15 the next morning and contributes `0.15` to the composite until flow warms up ~30 minutes later.

**Fix:** Decay unavailable modules toward 0 over N cycles rather than holding indefinitely. Clear confirmed scores on a new trading day.

---

### RE-06 — `int(round(...))` uses banker's rounding
**Severity: MEDIUM.** Verified: `round(0.5) == 0`, `round(-0.5) == 0`. Any `±0.5` module score (which `_module_vol` produces in the most common market state) silently becomes 0. Use `math.copysign(math.ceil(abs(x) - 0.5 + 1e-9), x)` or keep floats (preferred, see RE-01).

---

### RE-07 — Warmup gate is only 1 cycle
**Severity: MEDIUM.** `self._warmup_required = 1` — after a single 60s refresh the engine will act on a regime derived from unconfirmed buffers and (post-restart) reloaded stale `_conf` values. Given the persistence filter needs 3 readings, warmup should be ≥3 cycles.

---

### RE-08 — Macro override window is far too narrow
**Severity: MEDIUM · Impact: You are short gamma into Budget/RBI announcements.**

`EVENT_PRE_HOURS = 6` / `EVENT_POST_HOURS = 2`, measured from **09:15 on the event date**. So `EVENT_HEDGE` activates at 03:15 on event day (pre-market) and expires at **11:15 the same morning**. An RBI policy statement at 10:00 or a Budget speech running to 13:00 is covered; a 14:00 press conference is not. More importantly, positions opened on the *preceding* days are never flattened — the override starts 6 hours before, not 2 days.

**Fix:** Extend to `EVENT_PRE_DAYS = 2` for short-vol de-risking, and anchor the post-window to the actual event time, not 09:15.

---

### RE-09 — Skew z-score requires 3 distinct calendar days, one sample per day
**Severity: MEDIUM.** `_record_skew` stores one value per date; `_skew_zscore` needs `SKEW_MIN_DAYS = 3` *excluding today*. So skew contributes nothing for the first 4 trading days of any fresh deployment, and a `stdev < 1e-9` guard silently returns `None` in low-variance regimes.

---

### RE-10 — `leg_delta` risk-free rate hardcoded to 0.065
**Severity: LOW.** `6.5%` passed as a literal at four call sites. Minor for weekly options but should be a config constant.

---

# 4. `strategy_engine.py`

### SE-01 — Short straddle `max_risk` is set to 1× credit; the true risk is unbounded
**Severity: CRITICAL · Impact: Sizing is built on a fiction. This is the single largest tail-risk exposure in the engine.**

```python
# _build_short_straddle, line 2880
max_risk = total_premium * 1.0 * config.LOT_SIZE
```
A naked ATM short straddle has **theoretically unlimited** loss. Setting `max_risk = 1× credit` means:

- `_calculate_lot_size()` divides `MAX_RISK_PER_TRADE (₹80,000)` by `max_risk` → at 250-pt premium that is `₹16,250/lot` → **4 lots**.
- Actual exposure at 4 lots: a 3% adverse gap (`STRADDLE_SPOT_STOP_PCT`, ~720 pts) leaves the short leg ~470 pts ITM = `470 × 65 × 4` = **₹122,200**, versus a declared `max_risk` of ₹65,000.
- The capital-deployment gate (`deployed = sum(p.max_risk)`) and the combined-risk check (`MAX_COMBINED_RISK`) both read this understated number, so the portfolio can hold **~2× the risk it believes it holds**.
- `realized_pnl_percent` divides by `max_risk`, so reported returns are inflated ~2×.

The `_estimate_margin_requirement()` patch partly compensates (naked margin ≈ 11% of notional = ₹171,600/lot, capping at 4 lots), but that is a coincidental collision, not a risk control.

**Fix:** Define `max_risk` for undefined-risk structures as a **stop-based** figure:
```python
max_risk = (stop_multiple × credit) × LOT_SIZE   # e.g. 2.0 × credit, matching STRADDLE_STOP_MULT
```
plus a gap allowance. Better: refuse to trade naked straddles at all and require the condor/iron-fly variant with defined wings.

---

### SE-02 — Condor/spread structures are negative-EV as configured
**Severity: CRITICAL.** See **CFG-01** for the arithmetic. The builder-side manifestation:
- `_build_iron_condor` accepts any `net_credit >= 40` on a 400-wide structure.
- `_build_credit_spreads` accepts `total_credit >= 25` across two spreads whose widths come from delta-selected strikes (typically 200–400 pts each).

Neither builder checks credit **relative to width**. This is the fix that matters most:
```python
put_width  = short_put_strike - long_put_strike
call_width = long_call_strike - short_call_strike
min_credit = 0.25 * max(put_width, call_width)
if total_credit < min_credit: return (None, {})
```

---

### SE-03 — Condor uses **calendar** days in the expected-move formula
**Severity: HIGH · Impact: Short strikes are placed ~22% too close to spot. Directly raises loss frequency.**

```python
expected_move = spot * (vix / 100) * ((dte / 365) ** 0.5)
```
`dte` is calendar days (`(expiry - today).days`) but VIX is an **annualized** vol quoted on a 252-trading-day convention. Mixing conventions understates the move.

At spot 24,000, VIX 12, DTE 6 (calendar):
- As coded: `24000 × 0.12 × √(6/365)` = **369 pts**
- Trading-day correct: 6 calendar days ≈ 4 trading days → `24000 × 0.12 × √(4/252)` = **363 pts** — close here, but the error grows with weekends/holidays. Over a Monday→next-Tuesday window (8 calendar, 6 trading): as-coded 426 vs correct 444.

More importantly the formula also ignores that **weekend theta decays but weekend vol does not accrue**. The systematic direction is short strikes too close → more tested sides → more `_close_one_side` stop-outs at the worst price.

**Fix:** Convert to trading days via the holiday calendar (already in config), and add a small buffer:
```python
trading_days = count_trading_days(today, expiry)
expected_move = spot * (vix/100) * sqrt(trading_days / 252)
```

---

### SE-04 — Condor short strikes at 1.0σ is far too aggressive for premium selling
**Severity: HIGH · Impact: ~32% of condors are tested. Structurally guarantees a low win rate.**

`CONDOR_SIGMA_MULTIPLIER = 1.0` (the comment says "1.0σ not 1.5σ" as though it were a fix). At 1.0σ, P(inside) = 68.3%. Combined with a 10% credit/width ratio, that is the negative EV in CFG-01.

Standard premium-selling practice is 1.5σ (P ≈ 86.6%) or delta-based selection at 0.10–0.16 delta.

**Fix:** `CONDOR_SIGMA_MULTIPLIER = 1.5`, and *then* demand credit ≥ 15% of width. Test both against `regime_history` before going live.

---

### SE-05 — `_close_one_side()` returns and the caller returns `False`, so the *other* exits are skipped
**Severity: HIGH · Impact: A tested condor loses its remaining protection for a full cycle.**

In `_check_stop_loss`:
```python
await self._close_one_side(position, "call", STOP_LOSS)
return False          # <- signals "no stop hit"
```
Returning `False` is intentional (the position is still partly open), but `_monitor_all_positions()` then continues to `_check_trailing_stop` → `_check_profit_target` → `_check_dte_exit` on a position whose leg quantities were just mutated to 0 on one side. `_get_position_current_premium()` now sums only the surviving side, so `profit_pct` jumps discontinuously and the trailing stop can fire immediately on a phantom "profit."

**Fix:** After a one-sided close, `continue` to the next position for that cycle and let the next cycle re-evaluate cleanly.

---

### SE-06 — CB Level 1 fires per-position on **unrealized** P&L, pre-empting every designed stop
**Severity: HIGH · Impact: Circuit-breaks become the dominant exit; you exit at the worst intraday prices.**

`cb_l1_threshold = max(2% × capital, total_credit × LOT_SIZE)` = `max(₹20,000, credit_pts × 65)`. For a 4-lot straddle at 250 pts, `total_credit` (points × lots) = 1,000 → `× 65` = ₹65,000. But for a condor at 45 pts × 3 lots = 135 → `× 65` = ₹8,775, so the ₹20,000 flat floor binds — and ₹20,000 on a `max_risk` of ₹69,225 is a **29% stop**, tighter than the designed `2 × credit`.

The condor's own stop (`net_credit × 2.0`, in premium points) would trigger at a much larger loss. So CB L1 almost always fires first, and CB L1 exits with `use_market=True`.

**Fix:** CB L1 should be a *portfolio-level* backstop, not a per-position one. Set its threshold well above any individual strategy's designed stop, or remove the per-position variant entirely and rely on the stop ladder.

---

### SE-07 — Circuit breakers run on `realized_pnl` which is actually *unrealized* MTM
**Severity: HIGH · Impact: Naming collision that makes CB behaviour hard to reason about; drives false triggers.**

`_update_all_pnls()` writes MTM into `position.realized_pnl` for **open** positions. `_check_circuit_breakers()` then reads `position.realized_pnl` for CB L1. So a momentary bid/ask dislocation (which, per DM-01, is up to 60s stale) can trigger a market-order liquidation.

**Fix:** Rename to `unrealized_pnl`; require CB L1 to persist for 2 consecutive cycles before acting.

---

### SE-08 — Both `MAX_TRANCHES_PER_STRATEGY` tranches can occupy the same expiry
**Severity: HIGH · Impact: Concentration risk presented as diversification.**

`_build_iron_condor(tranche=2)` sets `target_dte += 7` and calls `get_expiry_by_dte(target_dte, tolerance=5)`. With `tolerance=5`, a target of 13 DTE will happily match an 8-DTE expiry — the same one tranche 1 used. The duplicate guard in `_enter_new_position` catches identical `(strategy, expiry)` pairs and aborts, so you *don't* double up — but you also **never get the second tranche**, and the abort consumes the entry opportunity for that cycle without setting a build-failure cooldown.

**Fix:** Pass an explicit `min_dte` floor to `get_expiry_by_dte` for tranche > 1, or exclude already-used expiries from the search.

---

### SE-09 — `_calculate_transaction_costs()` double-counts on repeated calls
**Severity: MEDIUM · Impact: Understated live P&L; noisy CB thresholds.**

The function iterates legs and counts an order for entry (`entry_price > 0`) *and* exit (`exit_price > 0`). It is called from `_estimate_costs()` inside `_check_circuit_breakers()` **on every cycle for open positions** — where `exit_price == 0`, so only entry costs count. Then at close it is called again with both. That part is fine.

The real issue: `_position_to_dict()` calls it for open positions on every `_save_all_positions_to_sqlite()` (every cycle), and `_check_circuit_breakers` calls it twice per cycle per position. It is a pure function so results are consistent, but it opens no DB and is cheap — the concern is that **CB L2's `daily_pnl_net_estimate` subtracts open-position closing costs that are only entry-side**, systematically understating the true cost of exiting. Minor, but it makes the CB threshold ~2× less conservative than intended.

**Fix:** Add an explicit `include_exit_estimate=True` mode that doubles the per-order count for open positions.

---

### SE-10 — `_estimate_max_loss` for RATIO_SPREAD uses `debit × 2` — the wrong risk model
**Severity: MEDIUM · Impact: Wildly wrong sizing for the one strategy with genuine unbounded-ish risk.**

The 1×2 ratio spread built here is **long 2 / short 1** on each side — that is actually a *backspread* (net long options), so max loss is bounded by the net debit. But the builder requires `total_credit > 0` (a net credit), which for a 2:1 long-heavy structure means the ATM short is worth more than 2× the wing longs — an unusual condition. Meanwhile `meta["max_risk"] = total_credit * 2 * LOT_SIZE` treats it as credit-based.

The structure and the risk model disagree. `_check_stop_loss` was also recently patched to use `position.stop_loss` (`credit × 2`) as a premium stop, which only makes sense for a net-short structure.

**Fix:** Decide what this strategy is. If it's a backspread, `max_risk = net_debit × LOT_SIZE` and the credit gate is wrong. Given `RATIO_MAX_CAPITAL_PCT = 0.01` caps it at ₹10,000 anyway, consider removing the strategy entirely — it adds complexity for ~1% of capital.

---

### SE-11 — Butterfly ignores its own delta config and uses a 100-point wing
**Severity: MEDIUM · Impact: Max profit is capped at ~50 pts; commissions eat a large share.**

`wing_width = config.NIFTY_STRIKE_STEP * 1` = 100 pts. With `BUTTERFLY_MIN_RR_RATIO = 2.0`, net debit must be ≤ 33 pts and max profit ≤ 67 pts. Round-trip costs on 4 legs (with the ×2 on the body) ≈ 6 points/lot in slippage alone. Meanwhile `BUTTERFLY_DELTA_A/B/C` (0.30/0.20/0.10) are unused.

Also: `_get_upper_wing_strike()` and `_get_lower_wing_strike()` both filter on `option_type == "put" and action == "BUY"` and return `max`/`min` respectively. Correct for this all-put butterfly, but they will silently return wrong values for any other structure.

---

### SE-12 — `_check_dte_exit` triggers at `dte <= exit_dte` before the profit target is evaluated
**Severity: MEDIUM · Impact: Forced exits at DTE=1 regardless of P&L, forfeiting the best theta day.**

Order in `_monitor_all_positions`: stop → trailing → **profit target** → DTE → max hold. Profit target *is* checked first, good. But `STRADDLE_EXIT_DTE = 1` / `CONDOR_EXIT_DTE = 1` means every credit position is force-closed one day before expiry at market. For a condor sitting at 80% profit with strikes far OTM, that final day is nearly pure profit and the exit costs 2–6 points of slippage.

**Fix:** Skip the DTE exit when the position is comfortably profitable AND both short strikes are >1.5σ away; let the `_expiry_day_close_all` + worthless-leg-skip logic (which already exists and is good) handle it.

---

### SE-13 — Entry window closes at 14:00 but positions must be flat by 15:15
**Severity: MEDIUM · Impact: 75-minute minimum hold on an intraday basis; heavy churn.**

`EXEC_END_TIME = 14:00`, `TIME_EXIT_NORMAL = 15:15`. Positions entered at 13:55 that don't qualify for overnight hold (`_end_of_day` requires `dte > 5`, regime ∈ SELL, and specific strategies) are closed 80 minutes later — paying full round-trip friction (~6–12 pts/lot) for 80 minutes of theta (~3–5 pts on a weekly).

**Fix:** Either move `EXEC_END_TIME` to ~11:30 so intraday positions have real duration, or require entries after 13:00 to satisfy the overnight-hold criteria before being allowed.

---

### SE-14 — `_move_stop_to_breakeven` sets condor/spread `stop_loss = 0.0`, disabling the stop
**Severity: MEDIUM.**
```python
elif strategy in [IRON_CONDOR, CREDIT_SPREADS]:
    position.stop_loss = 0.0
```
`_check_stop_loss` for these strategies uses spot-vs-short-strike logic and never reads `position.stop_loss`, so this is currently inert. But `_check_profit_target` uses `position.profit_target`, and any future code reading `stop_loss` will see "no stop." Meanwhile the straddle branch was correctly patched to use `total_credit`.

---

### SE-15 — `_hedge_delta` hedges with `NSE_FO|NIFTY`, which is not a tradeable instrument key
**Severity: MEDIUM · Impact: Live delta hedge will fail with an API error.**

`INSTRUMENT_NIFTY_FUT = "NSE_FO|NIFTY"` is a series prefix, not a specific contract (a real key looks like `NSE_FO|53215`). In paper mode this is logged and skipped; in live mode `_api_post` will 400 and retry 5 times (~31s) before raising, inside the strategy cycle.

**Fix:** Resolve the current-month futures instrument key from the instrument master at startup.

---

### SE-16 — `_reduce_position_50pct` uses `max(1, floor(qty × 0.5))` — reduces a 1-lot position to 0
**Severity: MEDIUM.** For `leg.qty == 1`, `max(1, 0) = 1` → the entire leg is closed, not 50%. The position becomes a naked single-sided remnant (for a straddle: one leg closed, one leg naked) rather than being fully closed. `_reduce_position_pct(0.75)` has the same behaviour.

**Fix:** If `floor(qty × pct) == 0`, either close the whole position or skip the reduction — do not leave an unbalanced remnant.

---

### SE-17 — Trailing stop peak is stored in `position.meta` but `meta` is not persisted
**Severity: MEDIUM.** `_peak_profit_pct` lives in `position.meta`, which is `copy.deepcopy(meta)` at creation and never written to SQLite (`save_position` stores no meta column). The docstring acknowledges the reset-on-restart behaviour, but combined with DM-05 the engine loses meaningful state on every restart.

---

### SE-18 — `_pre_trade_checks` runs before lot sizing, so its risk check is off by a factor of `lots`
**Severity: MEDIUM.** Acknowledged and partially patched — `_enter_new_position` adds an authoritative post-sizing check (NEW-2). But `_pre_trade_checks` *also* does the delta-limit check pre-sizing, comparing a 1-lot delta against the limit. A 4-lot position's true portfolio delta is never gate-checked before execution; only `_check_greeks_limits()` catches it afterwards, reactively, via `_hedge_delta` (which is broken per SE-15).

---

### SE-19 — Paper slippage is optimistic and asymmetric in the wrong direction
**Severity: MEDIUM · Impact: Paper results will not replicate live.**

`PAPER_SLIPPAGE_SHORT_TICKS = 20` (1.0 pt), `PAPER_SLIPPAGE_HEDGE_TICKS = 40` (2.0 pts), applied from the **midpoint**. But real fills for a market/aggressive-limit order cross the spread — you pay roughly half-spread *plus* impact. With `MAX_SPREAD_ATM_PTS = 3` and `MAX_SPREAD_OTM_PTS = 5`, half-spread alone is 1.5–2.5 pts, so short-leg slippage of 1.0 pt from mid is optimistic.

Round-trip for a 4-leg condor: modelled 12 pts vs realistic ~16–20 pts. Against a 40-pt credit that is the difference between marginal and clearly negative.

**Fix:** Model slippage as `half_spread + impact_ticks`, derived from the live bid/ask, not a fixed tick count.

---

### SE-20 — `total_credit` is in points×lots but compared against rupee thresholds in places
**Severity: LOW (already patched in CB L1, flagging for consistency).** `_create_position_record` computes `total_credit = Σ(entry_price × qty)` — points × lots, no `LOT_SIZE`. CB L1 correctly multiplies by `LOT_SIZE`. `_check_profit_target` and `_check_trailing_stop` correctly compare against `_get_position_current_premium()` (same units). Keep an eye on any new code.

---

### SE-21 — No per-strategy performance attribution / kill logic
**Severity: HIGH (profitability opportunity, not a bug)**

The engine writes a rich `trade_analysis.csv` (composite score, sub-scores, DTE, regime at entry/exit) but **nothing reads it back**. There is no mechanism to:
- Disable a strategy whose trailing-20-trade expectancy is negative.
- Weight lot sizing toward historically profitable `(regime, strategy)` pairs.
- Detect that, say, all `MILD_SELL → CREDIT_SPREADS` trades lose money.

**This is the highest-value *addition* you can make.** Suggested implementation:
```python
def _strategy_health(self, strategy_name, lookback=20):
    trades = last N closed trades for strategy
    if len(trades) >= 10 and mean(net_pnl) < 0:
        return "DISABLED"
    return "OK"
```
Gate `_select_strategy()` on it. A negative-expectancy strategy that keeps trading is how accounts die slowly.

---

# 5. `main.py`

### MN-01 — `_display_console()` calls `se._get_portfolio_greeks()` every 30s from the main loop
**Severity: LOW · Impact: Minor, but it iterates all positions and legs synchronously.** Cached correctly via `cached_greeks`; noted only for completeness.

### MN-02 — EOD `should_close` default is `True`, and the overnight-hold condition is very restrictive
**Severity: HIGH · Impact: Near-total daily churn; theta is never actually harvested.**

Overnight hold requires **all** of: regime ∈ {STRONG_SELL, MILD_SELL} **and** `dte > 5` **and** strategy ∈ {straddle, condor, spreads} **and** tomorrow ≠ this position's expiry **and** `vix < 22`.

Given `CONDOR_DTE_MIN + 2 = 6` is the target DTE and `get_expiry_by_dte` often returns 6–8 DTE, `dte > 5` is satisfiable — but only on the entry day. By day 2, `dte = 5` and the position is force-closed at EOD. **Every credit position has an effective maximum life of about one overnight.**

Selling weekly premium and closing after ~1 day captures roughly 1/6 of the theta while paying 100% of the round-trip friction. This is a structural profitability leak that compounds with CFG-01.

**Fix:** Relax to `dte >= 2` (i.e. hold until the DTE exit rule fires) for defined-risk structures, keeping the VIX and regime gates. For naked straddles, keep the conservative rule.

### MN-03 — `_is_expiry_day()` falls back to `weekday == 1` unconditionally
**Severity: MEDIUM.** If `get_available_expiries()` is empty or stale, every Tuesday is treated as expiry day, triggering `_expiry_day_close_all` at 15:10 and setting `eod_done_today = True` — halting the engine for the rest of the day even when nothing expires.

### MN-04 — `_run_preflight_checks` exits the process on a holiday/weekend
**Severity: MEDIUM.** `sys.exit(1)` on a non-trading day means any supervisor/systemd restart loop will thrash. Preferable: sleep until the next trading day.

### MN-05 — `_find_nearest_expiry()` can issue up to 60 sequential API calls at startup
**Severity: MEDIUM.** `for days in range(1, 60)` with `fetch_option_chain` + `sleep(0.2)` = up to ~12s and 60 requests against a 50/s bucket, at the moment the engine most needs its rate-limit headroom. `_ensure_future_expiry_coverage` and `_ensure_term_structure_expiry` each do the same (60 and 15 iterations).

**Fix:** Derive candidate expiries from the Upstox instrument master (one call) instead of brute-force probing.

### MN-06 — Regime refresh and `se.run_cycle()` are coupled to the same 60s timer
**Severity: MEDIUM · Impact: Stop-losses are only evaluated once per minute.**

`se.run_cycle()` — which contains `_monitor_all_positions()` and therefore every stop-loss and profit-target check — runs **only inside the regime-refresh block**, gated on `regime_elapsed >= REGIME_REFRESH_SECONDS (60)` *and* `data_refresh_complete` *and* `data_age <= 120s`. If a data refresh errors, position monitoring is skipped entirely for that cycle.

Combined with DM-01 (60s stale bid/ask), the effective stop-loss latency is **60–120 seconds**. On a 3% gap that is the whole move.

**Fix:** Decouple. Run `_monitor_all_positions()` on a fast 5s loop using WS prices; run regime refresh on the 60s loop.

### MN-07 — `data_refresh_complete` gate can permanently block trading
**Severity: MEDIUM.** Set `False` at the start of the refresh block and `True` in a `finally` — safe. But `data_age > REGIME_REFRESH_SECONDS * 2` skips the regime *and* the strategy cycle. If the API is slow (e.g. DM-10's 31s retry stall), `data_age` exceeds 120s and **no position monitoring runs at all** during the outage.

**Fix:** Always run `_monitor_all_positions()`; only gate *new entries* on data freshness.

### MN-08 — EOD branch triggers at `TIME_EXIT_NORMAL` (15:15) and then `continue`s for the rest of the day
**Severity: LOW (already patched with `eod_done_today`).** Behaviour is now correct; noting that the 60s sleep in that branch means a signal-based shutdown takes up to 60s to be noticed.

### MN-09 — Signal handler uses `loop.call_soon_threadsafe` from a `signal.signal` handler
**Severity: LOW.** Works, but `loop.add_signal_handler()` is the correct asyncio idiom on POSIX. Current approach is fine on Windows (where `add_signal_handler` is unsupported).

---

# Prioritized Action Plan

## Tier 1 — Do before any live capital (profitability-critical)
1. **CFG-01 / SE-02** — Replace absolute min-credit with credit ≥ 20–25% of wing width. *Without this, everything else is polish on a negative-EV engine.*
2. **SE-04** — Move condor shorts to 1.5σ.
3. **SE-01** — Fix short-straddle `max_risk` to a stop-based figure (or drop naked straddles).
4. **CFG-02** — Cut `MAX_RISK_PER_TRADE_PCT` to 1.5–2%.
5. **DM-02** — Delete the tick-based `log_returns` path; RV from daily candles only.
6. **DM-01** — Stop preferring stale bid/ask over live LTP; add quote-staleness guards.

## Tier 2 — Fix within the first week
7. **RE-01 / RE-06** — Stop integer-rounding module scores; recalibrate thresholds from logged `regime_history`.
8. **MN-02** — Relax the overnight-hold rule so theta is actually harvested.
9. **MN-06 / MN-07** — Decouple position monitoring from the 60s regime timer.
10. **SE-06 / SE-07** — Make CB L1 a portfolio backstop; require 2-cycle persistence.
11. **SE-03** — Trading-day expected-move calculation.
12. **RE-02** — Delete duplicated thresholds; single source of truth in `config.py`.

## Tier 3 — Structural improvements for sustained profitability
13. **SE-21** — Per-strategy expectancy tracking with auto-disable. *Highest-value addition.*
14. **CFG-03** — Implement VIX-scaled stop-losses (`SL_*` constants).
15. **CFG-04** — Implement VIX-adaptive delta selection (`*_VIX_DELTA`).
16. **SE-19** — Spread-derived slippage model so paper ≈ live.
17. **DM-05** — Persist all rolling windows across restarts.
18. **RE-08** — Widen the event-hedge window to 2 days pre-event.

## Tier 4 — Hygiene
19. Delete the 75 unused constants and the dead `compute_net_flow`/`compute_spread_ratio`/`fetch_oi_snapshot` path (DM-03).
20. **DM-08** fail-closed margin, **DM-10** no-retry-on-4xx, **SE-15** real futures key, **CFG-06** multi-year holiday calendar.

---

## Closing note on methodology

Reading the accumulated `PATCH:` / `LIVE FIX:` comments, a pattern stands out: several past "fixes" lowered a threshold specifically because a gate was blocking trades (`CONDOR_MIN_CREDIT` 100 → 15, `SPREAD_MIN_CREDIT` 50 → 10, `EXEC_END_TIME` 11:00 → 14:00, `ADX_TREND` 25 → 20, DTE tolerance 2 → 5). Each individually looks reasonable; collectively they optimized the engine for **trade frequency** rather than **trade quality**, which is how a system arrives at a structurally negative expected value while every individual component appears to work.

The credit thresholds in CFG-01 are the clearest example, and reversing that specific direction of drift is the core recommendation of this audit. **A gate that blocks every trade is telling you the market isn't paying enough — that is information, not a bug.**
