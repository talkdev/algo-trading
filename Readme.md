# NIFTY Intraday Algorithmic Options Trading Engine & Backtest Framework

## Architecture Overview & Technical Reference Manual

**Current build:** `v65m7q-cycle-alive-watchdog`  
**Hard invariant:** never open credit into a labelled threatened trend  
**Live entry point:** `python main.py`  
**Replay:** `python backtest_engine.py` (same `MainEngine.run_one_cycle()`)

---

### Table of Contents

1. [Executive Summary & Core Design Philosophy](#1-executive-summary--core-design-philosophy)
   - [1.1 Current build (v65m7q)](#11-current-build-v65m7q--backtest-is-a-data-pump-into-mainengine)
   - [1.2 Live-hardening SOP](#12-live-hardening-sop)
2. [Module Architecture & Import Dependency Graph](#2-module-architecture--import-dependency-graph)
3. [Engine Initialization, Data Structures & State Mechanics](#3-engine-initialization-data-structures--state-mechanics)
4. [Master Pipeline Lifecycle: `decide()`](#4-master-pipeline-lifecycle-decide)
5. [Hard Gate Validation Engine (`_check_hard_gates`)](#5-hard-gate-validation-engine-_check_hard_gates)
6. [Regime Classification & Regime-to-Strategy Mapping](#6-regime-classification--regime-to-strategy-mapping)
7. [Range Strategy Resolution & Two-Way Extreme Fading Logic](#7-range-strategy-resolution--two-way-extreme-fading-logic)
8. [Strategy Entry Rule Validation (`_validate_entry_rules`)](#8-strategy-entry-rule-validation-_validate_entry_rules)
9. [Mathematical Parameter Computation & Leg Construction (`compute_params`)](#9-mathematical-parameter-computation--leg-construction-compute_params)
10. [Cost, Slippage, and Margin Architecture](#10-cost-slippage-and-margin-architecture)
11. [Expected Value (EV) Gate Model (`_compute_ev_gate`)](#11-expected-value-ev-gate-model-_compute_ev_gate)
12. [DTE Mechanics & Continuous Sqrt-Life Interpolation (`by_dte`)](#12-dte-mechanics--continuous-sqrt-life-interpolation-by_dte)
13. [Long-Premium Momentum Alternative Route](#13-long-premium-momentum-alternative-route-_momentum_decision--compute_momentum_params)
14. [Exit Ladder, Profit Lock, and Debit HWM](#14-exit-ladder-profit-lock-and-debit-hwm)
15. [Dominant Structures, Position Sizing, and Risk Allocation](#15-dominant-structures-position-sizing-and-risk-allocation)
16. [Live Operations, Telegram, and Durability](#16-live-operations-telegram-and-durability)
17. [Event-Driven Backtest Engine CLI & Replay Architecture](#17-event-driven-backtest-engine-cli--replay-architecture)
18. [Decision & Rejection Census Dictionary](#18-decision--rejection-census-dictionary)

---

### 1. Executive Summary & Core Design Philosophy

The system is a cost-aware, intraday NIFTY 50 options engine for NSE weekly / near-dated contracts. One decision path serves both live trading and historical replay.

#### Core Tenets

- **Cost-aware structural edge.** Every candidate structure must clear realistic round-trip friction (STT, exchange, SEBI, stamp, GST, brokerage, bid-ask slippage). Edge is never assumed.
- **Strict intraday demarcation.** Positions open after the session window (DTE-dependent) and flatten before the hard exit (15:15 IST weekly, 15:00 on 0DTE) — five minutes before the broker 15:20 F&O RMS sweep.
- **Asymmetric book.** The primary book is a premium seller (Iron Condor, Iron Butterfly, Bull Put Spread, Bear Call Spread). Long Call / Long Put exist only as a **substitute** when the sell-side is refused (or as an aligned second-slot debit when that env is enabled).
- **Two-way auction preservation.** Expanding sessions reject mid-range pins and fade confirmed extremes instead.
- **Hard credit-into-trend invariant.** `_hard_credit_into_trend_refusal()` is the single source of truth: no BCS into labelled UPTREND / no BPS into labelled DOWNTREND once ADX is at the trend threshold. Waivers are day-structure bearish or a pinned high-extreme fade — never a self-set circular exemption.

---

### 1.1 Current build (v65m7q) — backtest is a data pump into MainEngine

**Architecture rule:** do **not** maintain a second trading loop. Replay feeds historical DB cycles into the **same** live path:

```text
HistoricalStore → SimClock.set → ReplayClient.point → MainEngine.run_one_cycle()
```

| Layer | Live | Replay |
|---|---|---|
| Orchestration | `MainEngine.run_one_cycle()` | **identical** (`MainEngine.for_replay`) |
| Indicators / regime / decide / monitor / entry / exit / state / P&L | live engines | **identical** |
| Broker I/O | `UpstoxClient` | `ReplayClient` (DB snapshots) |
| Fills | `PaperOrderExecutor` or live | `PaperOrderExecutor` + `FillModel` |
| Clock | wall `now_ist` | `SimClock` (patches `core` / `main` / engines) |
| Telegram | `TelegramReporter` (daemon thread) | stub |

Startup banner must show:

```text
Engine Build = v65m7q-cycle-alive-watchdog
Hard Invariant = no credit into labelled threatened trend
```

#### What this build owns (v65h → v65m7q)

| Area | Behaviour |
|---|---|
| Cycle-alive watchdog | A slow but **running** cycle must not look like a dead feed. `MainEngine` marks `_cycle_in_progress` and phase-times `run_one_cycle()` (Sep25 live printed 80–170s iterations). |
| Construct-fail latch | Latch after **2** identical economics rejects (`construct_fail_latch_after=2`). Single-reject sticky over-suppressed re-probes. Deep-EV latch only when EV ≤ `construct_fail_deep_ev_pts` (−10 pts). |
| Broker history truth | History API failure must **not** look like “no order” (v65m7p). |
| Hard invariant → debit | `hard_invariant` / `no_calls_into` / `no_puts_into` / unfinished-extreme / sticky tokens are momentum-block markers so a refused credit can still consult LONG_CALL / LONG_PUT. |
| Extreme fade pin | Only the **matching** extreme is pinned (a low-location BCS must not inherit a high-fade flag). |
| Fade flags on FLAT | Location-derived fade flags must not permanently latch when the tape is FLAT. |
| Sticky max-pain | When IV expand clears, drop the sticky max-pain latch so Intent can re-evaluate. Soft/warmup BCS still needs a prior refuse + ≥10 pt spot move before max-pain is waived. |
| Open-spike wait | “Unresolved” is not wall-clock alone — a lower-high / pullback can resolve the wick before 12:15. |
| Range-origin map | `PREMIUM_SELL_BULL` / `PREMIUM_SELL_BEAR` with `price=RANGE` defer to `_resolve_range_strategy` so location+tape evidence applies (and cannot flip to the opposite vertical). |
| Sticky trend refuse | After a labelled UPTREND BCS refuse (or DOWNTREND BPS refuse), a one-cycle RANGE flicker does not re-open that credit. |
| Unfinished extremes | BCS at loc ≥ 0.90 / BPS at loc ≤ 0.10 refused unless day-structure / extreme-fade waiver applies. |
| Mid-range fade | Mid-location fade into a with-grind EMA/VWAP is refused (Sep18-class). |

#### Still true from v65–v65h (do not regress)

- Replay is a MainEngine pump (not a second `decide()` / `_open` / `_close`).
- Debit HWM: Phase A after `momentum_lock_trigger` is free-trade only; Phase B trail only once HWM open gain ≥ `momentum_hwm_large_frac` (default **1.0** = 2× entry), giving back `momentum_hwm_giveback_frac` (0.20).
- Replay does **not** force `allow_correlated_debit_beside_credit` (default **false**).
- Immature ORB breakout needs same-side VWAP (not NEUTRAL/UNKNOWN).
- Immature directional credit may underwrite on strong ADX preview (≥ `adx_strong_threshold`).
- Profit-lock **arming** on DTE1+ uses the weekly 22% bar (not a ~29% blend). Armed lock-hit = `CLOSE_TARGET`.
- DTE1 lock-anchored P6 ladder; near-expiry give-back capped at `profit_lock_max_giveback_pts_dte0/dte1` (2.0 pts). IC/fly arm at `profit_lock_pct_symmetric` (0.10).
- Location-edge credits: `credit_risk_ratio_away_side` (0.04); EV may clear down to −friction; DTE0 wing `credit_ratio` eased to 0.09 on that path only.
- Soft directional always requires `_soft_tape_agrees`. Soft lean at loc ≥ 0.70 / ≤ 0.30 can book without VWAP lag veto.

#### Prior (v50–v64) — superseded, do not treat as current

Those passes closed live book-truth (PENDING_ENTRY, orphan flatten, Intent, fill qty), tape/lean visibility, and the first MainEngine pump. The long per-version hunt tables (v50–v64) live in git history. **Do not** follow “restart on v61/v62/v65h” banners or the old `v65h-invariant` stamp.

Replay P&L tables printed in older README revisions are **not** a live guarantee. Hunt the first decision divergence, not end-of-day ₹.

---

### 1.2 Live-hardening SOP

1. **Hard invariants first.** `_hard_credit_into_trend_refusal()` is the single source of truth (map + counter-trend). No fade/two-way carve-out may reopen a labelled UPTREND BCS / DOWNTREND BPS except the documented waivers inside that function.
2. **No circular waiver.** Do not set an exemption in the same function that checks it.
3. **Hunt first divergence of decision**, not EOD P&L. Replay blotter wins are not proof live will refuse the next bad entry.
4. **Deploy proof.** Restart the live process; banner must show `v65m7q-cycle-alive-watchdog`. Confirm PID start time ≥ fix time.
5. **Inherited bad opens.** Entry invariants do not close positions opened under old code; manage/flatten under the exit ladder.
6. **Sanity before live:** `python verify_all.py` (module self-tests + static pre-flight). There is no `preflight_live.py` or `tests/test_live_invariants.py` in this tree.

---

### 2. Module Architecture & Import Dependency Graph

```
                  ┌───────────────────────────────┐
                  │           main.py             │
                  │   Live / replay orchestrator  │
                  │     MainEngine.run_one_cycle  │
                  └──────────────┬────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
   ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
   │ bot_controller  │ │ backtest_engine │ │ telegram_reporter│
   │ Telegram /start │ │ SimClock +      │ │ daemon sender   │
   │ /stop → algo.stop│ │ ReplayClient    │ │ (live only)     │
   └─────────────────┘ └────────┬────────┘ └─────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────────────┐
        │              strategy_engine.py               │
        │   Hard gates, Intent, map, EV, momentum       │
        └───────┬───────────────────────────────┬───────┘
                │                               │
                ▼                               ▼
    ┌───────────────────────┐       ┌───────────────────────┐
    │   regime_engine.py    │       │  calibration_engine.py│
    │  4-tier classifier    │       │  rolling 20-session   │
    │  size_multiplier      │       │  Bayesian shrinkage   │
    └───────────┬───────────┘       └───────────┬───────────┘
                │                               │
                ▼                               ▼
    ┌───────────────────────┐       ┌───────────────────────┐
    │  execution_engine.py  │       │    data_engine.py     │
    │  PENDING→OPEN, 7-pri  │       │  1m/5m/15m bars,      │
    │  exit, profit lock,   │       │  chain, Greeks, OR    │
    │  flatten / orphans    │       │                       │
    └───────────┬───────────┘       └───────────┬───────────┘
                │                               │
                └───────────────┬───────────────┘
                                ▼
                    ┌───────────────────────┐
                    │        core.py        │
                    │ Config, Database,     │
                    │ ExpiryCalendar,       │
                    │ UpstoxClient, by_dte  │
                    └───────────────────────┘
```

#### Key module roles

1. **`main.py` — production driver.** Instantiates every engine, runs the cycle (`regime_calc_interval_sec` default **15**), watchdog, `/stop` flatten, daily halt, Telegram lifecycle, trade console report. `MainEngine.for_replay()` is the backtest entry. Paper is the default (`PAPER_TRADE_MODE`); live requires `LIVE_RATES_VERIFIED`.
2. **`bot_controller.py` — operator remote.** Telegram `/start` `/stop` (writes `logs/algo.stop`). It is **not** the trading loop.
3. **`core.py`.** `Config` (env.txt + defaults), SQLite `Database` (WAL + `synchronous=FULL` + `quick_check` on open), `ExpiryCalendar` (NIFTY **weekly Tuesday** expiry; Monday if Tuesday is a holiday), `UpstoxClient`, `now_ist` / `today_ist` / `by_dte` / `dte_blend`.
4. **`data_engine.py`.** Spot / VIX / chain ingest, 1-minute bars, 5-minute fast ADX (`adx_fast_resample=300s`), 15-minute MTF (EMA 9/21, ADX 14, VWAP, Parkinson RV). **No Supertrend; no synthetic Black-76/BS Greeks** — deltas/IV come from the broker chain. Drops the **forming 1-minute bar** before MTF. Session windows from **DTE**, not weekday.
5. **`calibration_engine.py`.** Live `CalibrationEngine` used by `MainEngine` and injected into `StrategyEngine` / `ExecutionEngine`. Rolling ~20-session shrinkage on VIX / VRP / OI / PCR / skew / day-range. `regime_engine.py` still contains a second `CalibrationEngine` class that `RegimeEngine` constructs internally — live threshold updates are pushed from the `main.py` instance.
6. **`regime_engine.py`.** Four-tier classification → `final_regime` + `size_multiplier`. Does **not** emit `MOMENTUM_BUY_*` — those are strategy-layer substitutes.
7. **`strategy_engine.py`.** Hard gates, Intent, map, range resolver, hard invariant, entry rules, strike/EV/sizing, momentum substitute.
8. **`execution_engine.py`.** Place / reconcile / monitor / flatten. 7-priority exit ladder, debit HWM, PENDING_ENTRY, orphan Exit-All.
9. **`backtest_engine.py`.** Historical pump + `FillModel` + blotter / audit / `--test`.
10. **`telegram_reporter.py`.** Start / heartbeat / order / close / stop. Own daemon thread — never sync-send on the trading thread.
11. **`split_db_per_day.py`.** Live primary → `data/per_day/*.db` shards for replay.
12. **`verify_all.py`.** Module self-tests + static pre-flight (does not start the live loop).
13. **`upstox_token.py`.** OAuth token refresh.

`restore_primary_db.py` is **not** in this tree. A corrupt/empty primary is handled by `Database` refusing a failed `quick_check`, and by `backtest_engine.py` falling back to `data/per_day/` when `--db` is omitted.

---

### 3. Engine Initialization, Data Structures & State Mechanics

#### `StrategyEngine.__init__`

```python
class StrategyEngine:
    def __init__(self, config: Config, database: Database,
                 market_engine: MarketDataEngine,
                 cal_engine: CalibrationEngine, logger):
        ...
        self._construct_fail_counts: Dict[str, int] = {}
        # session price memory for late momentum lives on the instance
        # as _v13_price_hist (filled by _note_price)
```

`CalibrationEngine` is injected (it is **not** constructed inside `StrategyEngine`). `ExpiryCalendar` is used via `core` helpers, not stored as `self.calendar`.

#### Persistent session state (`market_engine.state`)

- `entry_count`, `entry_start` / `entry_end` / `hard_exit_time` (DTE-derived)
- `consecutive_stops` — **exit streak from `trade_exits` blotter** (resync via `_sync_stop_streak`; memory latch alone cannot halt)
- `last_stop_time`, `last_stop_reason`, `last_stop_signal_combo`
- `last_exit_time`, `last_exit_spot`, `last_exit_strategy`, `last_exit_strategy_side`, `last_exit_is_regime_rotation`, `last_exit_is_failed_break_scalp`
- `last_entry_time`
- `session_high` / `session_low`, `session_mean_reversion_book`
- `day_mode` / `event_day` (events file reloaded on mtime)
- `session_no_calls_after_uptrend_refuse` / `session_no_puts_after_downtrend_refuse`
- `construct_fail` sticky key + `_max_pain_block_spot`
- `displaced_tape` latch
- `daily_halted`

#### Open-slot accounting

`PENDING_ENTRY` **counts** toward `max_concurrent_positions`. Successful `OPEN`+`CLOSED` count toward `max_entries_per_day`. Aborted/pending drafts do not burn the daily entry cap.

#### Database

- WAL + `PRAGMA synchronous=FULL`, 60s busy timeout, WAL checkpoint on close and after chain-snapshot bursts.
- Startup `PRAGMA quick_check`; a failed primary is refused (do not trade on a corrupt file).
- `strategy_decisions` stores every cycle (`action`, `strategy_name`, `reason`, `params_json`, `signals_json`).
- After regime merge, `MainEngine` patches the latest `cycle_log` **and** `market_snapshots` row (snapshots are written *before* enrichment).

---

### 4. Master Pipeline Lifecycle: `decide()`

Called from `MainEngine.run_one_cycle()` whenever the clock is inside the entry cut (09:30 … late-momentum end 14:57) and the flatten lock is held. ABORT / feed_stale / None regime are **hard-gate** refusals inside `decide()`, so momentum markers still fire.

```
                         [ signals ]
                              │
                              ▼
                       _note_price()
                              │
                              ▼
                    _check_hard_gates()
                 ┌────────────┴────────────┐
              fail                      pass
                 │                         │
                 ▼                         ▼
        _momentum_decision()     _map_regime_to_strategy()
                 │                         │
                 │              _build_decision_intent()
                 │                         │
                 │              _same_side_chase_refusal()
                 │                         │
                 │              _counter_trend_entry_refusal()
                 │                         │
                 │              _slot_conflict()
                 │                         │
                 │              _validate_entry_rules()
                 │                    │
                 │         IC pin fail → demote to evidenced vertical
                 │                         │
                 │              _sticky_construct_reason()
                 │                         │
                 │              compute_params()
                 │                    │
                 │         IC econ fail → one demotion
                 │                         │
                 └──────────┬──────────────┘
                            ▼
                     ENTER / NO_TRADE
                    (persist + cycle_log)
```

Every NO_TRADE path consults `_momentum_decision` before returning. Intent is rebuilt on IC demotion.

#### `MainEngine.run_one_cycle()` (live = replay)

1. Day reset  
2. `MarketDataEngine.run_cycle()`  
3. `RegimeEngine.process_signals` + `merge_regime_into_signals`  
4. Patch `cycle_log` / `market_snapshots`  
5. `monitor_all_positions` (always — ABORT never skips exits)  
6. `perform_hard_exit_sweep`  
7. `check_daily_loss_halt`  
8. `decide()` → `process_entry_decision`  
9. Daily P&L + console trade report  

Cycle interval default: `regime_calc_interval_sec = 15` (`REGIME_CALC_INTERVAL_SEC`). The outer loop also allows decide() from **09:30** through the late-momentum end (**14:57**); sell-side clocks inside `decide()` still use the DTE window.

---

### 5. Hard Gate Validation Engine (`_check_hard_gates`)

Evaluated in order. Session clocks come from `state` (DTE-set), not the static README 09:20/14:15 of older builds.

| # | Token / family | Trigger | Notes |
|---|---|---|---|
| 1 | `feed_stale_entries_blocked` | `signals["_feed_stale"]` | Watchdog / degrade |
| 2 | `ABORT:…` / `regime_unavailable` / `regime_engine_no_trade` | `block_new_entries`, `final_regime` None / `NO_TRADE` / `ABORT` | Upstream halt |
| 3 | `daily_loss_limit_reached_or_halted` | `daily_halted` | Default action **flatten** (`daily_halt_action`) |
| 4 | `circuit_breaker_suspected` | flag | Exchange halt / tick explosion |
| 5 | `vix_spike_detected` | flag | India VIX spike (abort_vix_spike_pct default 15%) |
| 6 | `iv_expanding_never_sell_into_rising_iv` / `iv_spiking` | `iv_behavior` EXPANDING/SPIKING | Waived for afternoon fade **or** `_away_side_intent` |
| 7 | `before_entry_window_*` / `past_entry_window_*` | clock vs `entry_start`/`entry_end` | **0DTE: 10:30–13:00.** Weekly: **09:45–14:15** |
| 8 | `max_concurrent_positions_reached` | OPEN+PENDING ≥ `max_concurrent_positions` (**2**) | Second ticket must be a different trade |
| 9 | `max_entries_per_day_N_reached` | OPEN+CLOSED ≥ **3** | +1 extra (cap 4) on live two-way fade |
| 10 | `second_slot_cooldown_*` | open book + < 10 min since last **entry** | Opposite extreme fade exempt |
| 11 | `entry_cooldown_*` | < 10 min since last **act** (entry or exit) | Opposite rotation: `reentry_opposite_cooldown_min` (3) |
| 12 | `no_material_change_since_exit_*` | spot move < max(0.12% , 15 pts) | Opposite rotation uses 0.25× |
| 13 | `2_consecutive_stops_halt` | blotter streak ≥ 2 | Banked `CLOSE_TARGET` is **not** a stop |
| 14 | `same_signal_combo_caused_last_stop` | same regime/signal fingerprint | |
| 15 | `stop_cooldown_*` | `STOP_COOLDOWN_MAP` | CLOSE_STOP 30m, CLOSE_ADX 45m, CLOSE_VWAP 20m, CLOSE_DELTA 30m |
| 16 | `spot_velocity_too_fast` | 5-min displacement vs `spot_velocity_pct` (0.14%) | Two-way + matching fade exempt |
| 17 | `straddle_expanding_no_sell_into_rising_iv` | ATM straddle up >6% vs ~5-min lookback (`[-1]`) | **No IV confirm** — do not qualify on EXPANDING/SPIKING (live morning blocks fired with DECLINING IV on ATM roll) |
| 18 | `opening_range_not_yet_computed` / `opening_range_pending` | `or_computed` false / `price_regime==OBSERVING` | Separate tokens |
| 19 | `open_spike_wait_unresolved_lower_high` | open-HIGH wick, before resolve / 12:15 | Pullback 30 pts or LH loc ≥ 0.70 after 10:45 |
| 20 | `chain_stale_cannot_validate_strikes` | chain age > 120s | |
| 21 | `expiry_day_waiting_for_0dte` | Tuesday morning, 0DTE chain not loaded | |
| 22 | `confidence_*_insufficient_*` | LOW/NONE | Extreme fade / away-side intent can waive |
| 23 | `dte_X_above_max_4_*` / `dte_requires_confidence` | DTE > 4, or DTE ≥ 4 needs HIGH | |
| 24 | `day_move_used_*_no_edge` | range / straddle ≥ `day_move_used_block_pct` | `day_move_range_factor` converts range→displacement |
| 25 | `only_Xmin_before_hard_exit_need_Y` | < 90 min (morning) / < 50 min after 13:00 | `afternoon_credit_after_hhmm=13:00` |
| 26 | `wide_or_*_dangerous_to_sell_premium` | WIDE / VERY_WIDE without confirmed trend | |

Intent exemptions are **post-selection** (v60). Hard gates must not call `_intent_exempt`.

---

### 6. Regime Classification & Regime-to-Strategy Mapping

#### Actual enum values (README previously listed obsolete names)

**Volatility:** `STRONG_SELL_PREMIUM`, `SELL_PREMIUM`, `BORDERLINE_SELL`, `NEUTRAL`, `BUY_OPTIONS`, `ABORT`  
**Price:** `STRONG_UPTREND`, `UPTREND`, `RANGE`, `DOWNTREND`, `STRONG_DOWNTREND`, `CHOPPY`, `OBSERVING`  
**Positioning:** `STRONG_RANGE`, `RANGE`, `BULLISH`, `BEARISH`, `UNCLEAR`  
**Final:** `PREMIUM_SELL_RANGE`, `PREMIUM_SELL_BULL`, `PREMIUM_SELL_BEAR`, `NO_TRADE`, `ABORT`  
**Confidence:** `HIGH`, `MEDIUM`, `LOW`, `NONE`

There is **no** `MOMENTUM_BUY_CALL` / `MOMENTUM_BUY_PUT` final regime. Long premium is a strategy-layer substitute.

ADX defaults: trend **20**, strong **28**. Immature ADX: directional credit needs a **strong preview** or wait (`NO_TRADE:ADX_IMMATURE_NO_DIRECTIONAL_CREDIT`). Immature ORB UPTREND/DOWNTREND requires same-side VWAP.

Sell-regime cutoff: **14:30** (`sell_regime_cutoff_hhmm` = `momentum_late_window_start`). After that the classifier is NO_TRADE and only the late-momentum route can buy.

#### `_map_regime_to_strategy`

```
PREMIUM_SELL_RANGE  →  _resolve_range_strategy()
PREMIUM_SELL_BULL   →  hard invariant → (if price=RANGE: defer to range)
                       → day-structure bearish veto (unless ADX ≥ strong UPTREND)
                       → two_way_wait_no_puts_at_high
                       → BULL_PUT_SPREAD
PREMIUM_SELL_BEAR   →  hard invariant → mid-range fade-into-grind
                       → (if price=RANGE: defer to range)
                       → two_way_wait_no_calls_at_low
                       → BEAR_CALL_SPREAD
```

Two-way overlay (range ≥ 85 pts, no fade flag yet): loc ≥ 0.80 → force BEAR / loc ≤ 0.20 → force BULL.

---

### 7. Range Strategy Resolution & Two-Way Extreme Fading Logic

`_resolve_range_strategy` is DTE-agnostic. A condor is a **pin**, never the default.

#### Ladder (first match wins)

1. **Structural bearish lean** (`_range_day_bearish_lean`) → `BEAR_CALL_SPREAD` (unfilled gap-down under a call wall). Friday / weekend-risk stays delta-neutral.
2. **Confirmed two-way auction** (range ≥ 85 pts): loc ≥ 0.85 → BCS, loc ≤ 0.15 → BPS; otherwise `two_way_auction_wait_for_extreme`.
3. **Location lean** (range ≥ 50 pts, not event day):
   - Mature ADX ≥ trend (20) + non-UNCLEAR positioning: loc ≥ **0.62** → BPS, loc ≤ **0.38** → BCS.
   - Soft **0.58 / 0.42** with `_soft_location_evidence` (EMA/VWAP tape agree), **or loc ≥ 0.70 / ≤ 0.30 alone** (warmup uses RANGE/STRONG_RANGE positioning).
4. **True pin only** — NARROW / VERY_NARROW OR, mature ADX in **[12, 20)**, loc (0.40, 0.60), session range < 100 pts, sell-premium vol, **before 12:00**, no grind-away: butterfly if `dte_blend ≥ 0.35` and ADX < 18 and |spot−ATM| < 50; else condor.
5. **Else `range_wait_no_pin_no_lean`.** Never `range_default_condor`.

Location:

```text
loc = (spot - eff_lo) / (eff_hi - eff_lo)
```

`eff_hi` / `eff_lo` strip open-spike wicks when the spike gap ≥ 15 pts.

---

### 8. Strategy Entry Rule Validation (`_validate_entry_rules`)

#### Iron Butterfly

- `dte_blend ≥ 0.35`; after 12:00 forbidden (any DTE); |spot−ATM| ≤ 50; ADX ≤ 22 **and mature**; OR NARROW / VERY_NARROW.

#### Iron Condor

- Runway ≥ `by_dte(dte, 75, 90)` minutes to hard exit.
- ADX mature and in [12, `CONDOR_PIN_ADX_MAX` 20); ADX < `adx_strong_threshold` (28).
- OR NARROW / VERY_NARROW only; banned on two-way; banned on unresolved open-spike wick; banned if session range ≥ 100 pts; banned if loc at soft-lean extremes; loc must be mid (0.40–0.60).

Pin-gate failure **demotes** to an evidenced vertical before momentum.

#### Bull Put Spread

- Afternoon low fade / away-side BULL Intent / `or_mid` / `early_pass` → pass.
- Else spot ≥ OR mid − `by_dte(30, 15)`; spot not > 30 pts below VWAP (failed-break reclaim exempt).

#### Bear Call Spread

- Afternoon high fade → pass.
- Away-side BEAR Intent waives **OR-mid only**, not max-pain.
- OR-mid veto applies on DOWNTREND/STRONG_DOWNTREND (not RANGE).
- Max pain: veto if |spot − max_pain| < 25 unless trend-through downtrend or Intent `max_pain`. Soft/warmup Intent requires a prior refuse latch **and** ≥ 10 pt spot move.

---

### 9. Mathematical Parameter Computation & Leg Construction (`compute_params`)

```
strategy + signals → _select_strikes → _build_validated_legs
  → net credit after costs/slippage → wing / credit-risk / friction
  → target & stop (by_dte) → _compute_ev_gate → lot sizing
```

#### Strike deltas (actual Config)

| Book | Flat | Trend | Strong |
|---|---|---|---|
| Near-dated | 0.32 | 0.30 | 0.28 |
| Weekly DTE ≥ 2 | 0.24 | 0.22 | 0.18 |

EM band: near-dated 0.80–1.35 × EM; weekly 0.55–2.10 × EM (condor weekly hi 1.35 × weekly ATM straddle). Fade shorts sit ≥ 80 pts outside the tested extreme.

Wing factor / max width via `by_dte` (factor 0.50→0.62, max 250→450 pts). Weekly wing-cost cap **0.58** (`wing_cost_frac_max_weekly`); near-dated **0.50**.

#### Construction prices (`_get_exec_price`)

Live/paper **construction** is conservative quote-edge, not a Config `fill_edge`:

```text
SELL = bid (else LTP, else ask)
BUY  = ask (else LTP, else bid)
```

`fill_edge` exists only on the **replay** `FillModel` (`--fill-edge`, default 0.25). Spread gate uses `spread_abs_tolerance` (0.85) so cheap wings are not rejected on a 0.10 tick.

#### Target & stop

- Target: `target_pct_for_dte` = `by_dte(0.70, 0.40)` (VIX shave on elevated VIX).
- Stop: `stop_mult_for_dte` = `by_dte(1.60, 1.70)` of net credit, capped at wing loss.

---

### 10. Cost, Slippage, and Margin Architecture

#### Statutory & brokerage (`_compute_costs`) — **Budget 2026 rates**

| Item | Default | Env |
|---|---|---|
| Brokerage | ₹20 / order | `BROKERAGE_PER_ORDER` |
| STT (options sell / exercise) | **0.15%** of premium | `STT_OPTIONS_SELL` (was 0.10% pre-Apr 2026; override for old-tape replay) |
| Exchange txn | **0.03553%** | `EXCHANGE_TXN_RATE` |
| SEBI | ₹10 / crore (`1e-6`) | `SEBI_RATE` |
| Stamp (buy) | 0.003% | `STAMP_DUTY_BUY_OPTIONS` |
| GST | 18% on (brokerage + exchange + SEBI) | hardcoded |

Older README 0.0625% STT / 0.05% exchange is **wrong**.

#### Slippage

```text
entry = Σ half-spread × entry_slippage_mult (0.35)
exit (stress) = Σ half-spread × exit_slippage_mult (2.25)
```

#### Economic checks (actual hurdles)

1. Net credit > 0.  
2. Round-trip friction ≤ **28%** of net credit (`max_friction_frac_of_credit`).  
3. Brokerage ≤ **15%** of net credit (`max_brokerage_frac_of_credit`).  
4. Wing cost ≤ 50% near-dated / 58% weekly of short premium (condors).  
5. Credit / wing ≥ `MIN_CREDIT_RATIO[_DTE0]`; location-edge may use `credit_risk_ratio_away_side` **0.04**; DTE0 time ladder 0.16 / 0.13 / 0.10.  
6. Target pts ≥ **1.25×** round-trip friction (`min_target_over_friction`).

---

### 11. Expected Value (EV) Gate Model (`_compute_ev_gate`)

Three-way blend (weights 0.40 model / 0.30 prior / 0.30 market delta; reclaim verticals tilt toward market).

```text
p_win = w_m · p_win_model + w_p · p_win_prior + w_k · p_mkt
EV = p_win_eff·(Reward − friction_calm) − p_stop·(Stop + friction_stress)
     − p_tail·(Tail + friction_stress)
```

- Tail = stop + 0.30 × max(wing − stop, 0).  
- `p_tail` = `gamma_tail_prob_for_dte` = `by_dte(0.055, 0.025)` (scaled on wide OR / strong ADX).  
- `p_win_prior` is **life-continuous**: OR-width anchors blended with `by_dte` (expiry 0.72…0.44, weekly 0.64…0.38). **No 7×5 DTE×OR table.** VRP / STRONG_SELL bonuses apply; clamp [0.30, 0.90].  
- `p_mkt` = 1 − max(|short Δ|).  
- Barrier σ = min(σ_iv, σ_straddle × 1.15); 0DTE max-pain within 0.5σ cuts p_touch 15%.  
- Min EV = max(net credit × 0.03, friction × 0.35). Location-edge credits may clear down to −friction.  
- DTE ≥ 2 carry uses `ev_carry_discount_dte2p` (0.62).

---

### 12. DTE Mechanics & Continuous Sqrt-Life Interpolation (`by_dte`)

```text
life = DTE + 0.5
w = (√2.5 − √life) / (√2.5 − √0.5)     # clamp [0, 1]
by_dte(DTE, V_expiry, V_weekly) = V_weekly + w · (V_expiry − V_weekly)
```

| | 0DTE | 1DTE | ≥ 2DTE |
|---|---|---|---|
| `dte_blend` w | 1.00 | ≈ 0.41 | 0.00 |
| Session window | 10:30–13:00, hard exit **15:00** | 09:45–14:15, hard exit **15:15** | same as 1DTE |
| Tradeable | Fly, IC, BPS, BCS, **momentum** (half risk, 2-lot cap) | IC, BPS, BCS, momentum | same; DTE 3–4 needs HIGH confidence |
| Target % | 0.70 | ≈ 0.52 | 0.40 (VIX shave DTE ≥ 3) |
| Stop × credit | 1.60 | ≈ 1.66 | 1.70 |
| Δ close | 0.45 | blend | **0.30** (`delta_close_dte1p`) |
| Lock arm | 40% | **22% weekly bar** (not blended) | 22%; after 13:30 → 15%; after 14:15 → 10%; IC/fly 10% |
| Lock keep | 0.65 | blend | 0.35 |
| Give-back cap | 2.0 pts | 2.0 pts | uncapped |
| Credit/wing min | IC 0.13, Fly 0.18, Vert 0.11 | IC 0.10, Fly 0.15, Vert 0.08 | same |
| p_tail | 0.055 | blend | 0.025 |
| Momentum | allowed (`momentum_min_dte=0`) | allowed | allowed |

NIFTY weekly expiry is **Tuesday** (holiday → Monday). `ExpiryCalendar.get_dte` is actual trading-day DTE, not weekday arithmetic.

---

### 13. Long-Premium Momentum Alternative Route (`_momentum_decision` & `compute_momentum_params`)

Momentum **never** runs unprompted. It answers sell-side refusals whose reason matches `momentum_block_markers` (economic fails, IV, event, time, post-stop, slot_conflict, hard_invariant, open_spike_wait, construct_fail, …).

#### Gate (current, not the old “0DTE forbidden” rule)

- Enabled; DTE in **[0, 4]**. 0DTE uses `momentum_dte0_risk_frac=0.50` and `momentum_dte0_max_lots=2`.
- Blocked if `session_mean_reversion_book` (two-way fade already owns the day).
- Morning: price in UPTREND/DOWNTREND (or strong), fast ADX ≥ `momentum_adx_min` **24**, OR break + VWAP proof. Event-day debit needs ADX ≥ **32**.
- Location lean on RANGE/CHOPPY: debit follows the lean (puts at the low, calls at the high).
- After a losing credit stop: one-way continuation allowed when ADX is strong and the tape is not two-way/chop; side aligns to what beat the vertical (BCS→calls, BPS→puts; IC/RANGE free).
- Late window **14:30–14:57**, ADX ≥ 28, VWAP displacement, fresh extreme, half-risk, max 4 lots, ≥ 25 min to hard exit.
- VIX gap ≤ 12%; do not chase SPIKING IV. Day-move cap 200% of priced range.
- Aligned debit beside an open credit is **off** (`allow_correlated_debit_beside_credit=false`) unless env-enabled; second-slot debit cap 3 lots.

#### Economics

- Δ target ≈ 0.45–0.55 ATM/ITM.  
- Capture = premium × `momentum_target_frac` (0.60) must clear 2× friction.  
- Stop 35% of premium; lock trigger +25%; spot invalidation = opposite VWAP.  
- Max 1 momentum ticket / day (`momentum_max_trades_per_day`).  
- `momentum_entries` increment **only on fill**.

---

### 14. Exit Ladder, Profit Lock, and Debit HWM

`ExecutionEngine.monitor_position` never closes on a regime **label** change. Priority order:

| Pri | Name | Typical reason | Notes |
|---|---|---|---|
| 1 | Delta breach | `CLOSE_STOP` | Short Δ vs `delta_close_for_dte` |
| 2 | Spot proximity | `CLOSE_STOP` | Gap-fraction on 0DTE (`prox_gap_frac_dte0=0.70`) |
| 2.5 | Trend-flip (verticals) | `CLOSE_STOP` | Measured trend against the short |
| 3 | Price / premium stop | `CLOSE_STOP` | **If profit-lock armed → `CLOSE_TARGET`** |
| 4 | Profit lock | arm / ratchet | DTE0 40%; DTE1+ 22% (clock steps 15%/10%); IC/fly 10% |
| 5 | Cheap buyback | `CLOSE_TARGET` | Short ≤ `cheap_buyback_pts` (**5.0**) after 13:00 |
| 6 | Time target | `CLOSE_TARGET` | DTE1 lock-anchored ladder; IC/fly 12/10/8 |
| 7 | Hard exit | `HARD_EXIT_15:00` | 15:00 0DTE / 15:15 weekly |

**Credit lock:** stop = credit − keep_frac × peak; near-expiry unlocked give-back capped at 2 pts.  
**Debit HWM:** Phase A free-trade after +25%; Phase B trail from peak once 2× entry, keep ~80% of peak open gain. Hitting the armed debit trail is `CLOSE_TARGET`.  
**Banked exits are not stops** (`banked_exit_is_not_a_stop`) — they do not spend the two-stop halt budget.

Regime rotation can free a slot on a mature opposite trend after `regime_rotation_min_hold_min` (25).

---

### 15. Dominant Structures, Position Sizing, and Risk Allocation

| Strategy | Role | Legs |
|---|---|---|
| `IRON_CONDOR` | True pin only | 4 |
| `IRON_BUTTERFLY` | Near-expiry pin | 4 |
| `BULL_PUT_SPREAD` | With-trend / low fade / location lean | 2 |
| `BEAR_CALL_SPREAD` | With-trend / high fade / location lean | 2 |
| `LONG_CALL` / `LONG_PUT` | Momentum substitute | 1 |

#### Sizing

```text
StructuralLoss/lot = efficacy × Stop + (1 − efficacy) × Wing     # efficacy 0.55 (0.80 failed-break)
MaxRisk = Capital × max_risk_per_trade_pct                       # default 2.0%
RawLots = MaxRisk / StructuralLoss
SizedLots = RawLots × size_mult
```

- `size_mult` from regime; second concurrent ticket × 0.70; extreme fade × 1.05–1.15; soft directional 0.75; weekly unclear range 0.75.  
- Floor `min_lots_fraction=0.60`. HIGH-confidence amortization may size up to amortize ₹20×legs brokerage.  
- `LOT_CAPS_BY_DAY`: Mon 8 / Tue 10 / Wed 6 / Thu 6 / Fri 5, scaled by √(capital / starting).  
- Margin: wing × 65 × 1.10 × (1+addon) × lots ≤ 80% capital. 0DTE addon +18%.  
- Defaults: starting capital **₹10,00,000**, daily loss **8%**, per-trade **2%** (clamped so 3 trades cannot exceed the daily halt). `FORCE_LOTS` can pin size after gates.  
- Event days: `event_size_multiplier`, defined-risk only; weekday `day_size_*` must **not** fall back to the event multiplier.

---

### 16. Live Operations, Telegram, and Durability

#### Run

```bash
python main.py                 # live or paper (PAPER_TRADE_MODE in env.txt)
python bot_controller.py       # Telegram remote
python verify_all.py           # module tests + static pre-flight
python upstox_token.py         # refresh access token
```

- `PAPER_TRADE_MODE=true` uses `PaperOrderExecutor` + optional `FillModel`.  
- Live orders: place **once** (`order_max_retries=0`), reconcile by tag (`order_tag_prefix=nav6`). Exits escalate as LIMIT + market-protection (2%); no MARKET/SL-M for options.  
- `/stop` writes `{LOG_DIR}/algo.stop`. Main flattens (tag Exit-All, including PENDING_ENTRY) then exits.  
- Watchdog: poll 5s; feed degrade 45s / force-exit 120s; square-off deadline **15:18**. A cycle in progress is **alive**.  
- Soft halt at 50% of daily loss (alert); hard halt **flattens**.  
- Startup: PENDING_ENTRY reconcile (probe all dispatch states; flatten via tag — never `execute_close` with empty legs); orphan PLACED without `position_id` flattened (`orphan_flatten_at_broker=true`); OPEN with 0 legs → `_finalize_empty_open_position`; broker flat + local OPEN → `_heal_local_open_broker_flat`.  
- `ALLOW_SAME_CYCLE_REENTRY` default **true** (live = BT).  
- Lot size default **65** — verify against the current NSE series.

#### Telegram (`telegram_reporter.py`)

Five message types: started, heartbeat every 15 min, order placed, order closed, engine stopped. Own daemon thread; `telegram_min_gap_sec=3.5`. Token / chat from `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

#### Console trade report (v7)

Same 84-column block in live and replay, from the persisted book + current chain. Modes: `each_cycle` (default), `on_change`, `off`.

#### Config & secrets

- `env.txt` overrides OS env; unknowns ignored. Token-only file uses code defaults.  
- Holidays: `nse_holidays.json`. Events: `high_impact_events.json` (mtime reload).  
- Dependencies: `requirements.txt` (numpy, pandas, requests, python-telegram-bot, psutil, Google API clients).

#### Primary DB

- Path default `data/nifty_algo_v3.db`.  
- After healthy sessions: `python split_db_per_day.py` (optional `--force`, `--verify-only`, `--dates`, `--include-global`).  
- **No** Google Drive upload in the current `split_db_per_day.py`.  
- Replay without `--db`: use primary if it has `option_chain_snapshot` rows; else every usable `data/per_day/*.db` shard.

---

### 17. Event-Driven Backtest Engine CLI & Replay Architecture

Harness edges only: `SimClock`, `ReplayClient`, `FillModel`, scratch `Database`, Telegram stub. No parallel `decide()` / `monitor_position()` / `_open` / `_close`.

```bash
python backtest_engine.py [OPTIONS]
python split_db_per_day.py [--force] [--verify-only] [--dates YYYY-MM-DD ...]
python verify_all.py
```

| Flag | Meaning |
|---|---|
| (no `--db`) | Healthy primary, else `data/per_day/` |
| `--db PATH [PATH ...]` | File(s) or a directory of `nifty_algo_YYYY-MM-DD.db` |
| `--from` / `--to` | Inclusive date filter |
| `--capital` | Override starting capital (default ₹10,00,000) |
| `--fill-edge` | 0.0 full spread / **0.25** default / 0.5 mid |
| `--stress-exit` | Edge retained on urgent exits (default 0.50) |
| `--csv` | Blotter path |
| `--audit` | Coverage only |
| `--test` | Harness self-test |
| `--verbose` | Cycle log |
| `--trade-report {each_cycle,on_change,off}` | Overrides `TRADE_REPORT_MODE` |

Hunt correlated-debit overlap only when the **live** book itself stacked; do not force the env in replay.

`--audit` / verbose funnel uses `STAGE_ORDER`: safety interlocks → entry window → position limits → contract/DTE → regime verdict → strategy selection → counter-trend symmetry → structure rules → structure build → EV gate. Unclassified tokens are reported, not folded into EV.

---

### 18. Decision & Rejection Census Dictionary

#### Hard gates (representative)

- `feed_stale_entries_blocked`, `regime_unavailable`, `ABORT:…`
- `daily_loss_limit_reached_or_halted`, `circuit_breaker_suspected`, `vix_spike_detected`
- `iv_expanding_never_sell_into_rising_iv`, `iv_spiking`
- `before_entry_window_10:30` / `before_entry_window_09:45`, `past_entry_window_13:00` / `past_entry_window_14:15`
- `max_concurrent_positions_reached`, `max_entries_per_day_N_reached`
- `second_slot_cooldown_Xmin_remaining`, `entry_cooldown_Xmin_remaining`
- `no_material_change_since_exit_*`, `2_consecutive_stops_halt`
- `same_signal_combo_caused_last_stop`, `stop_cooldown_Xmin_remaining`
- `spot_velocity_too_fast`, `straddle_expanding_no_sell_into_rising_iv`
- `opening_range_not_yet_computed`, `opening_range_pending`, `open_spike_wait_unresolved_lower_high`
- `chain_stale_cannot_validate_strikes`, `expiry_day_waiting_for_0dte`
- `confidence_*_insufficient_*`, `dte_X_above_max_4_*`
- `day_move_used_*_no_edge`, `only_Xmin_before_hard_exit_need_Y`
- `wide_or_*_dangerous_to_sell_premium`

#### Map / invariant / range

- `hard_invariant_no_calls_into_UPTREND_adx_X` / `hard_invariant_no_puts_into_DOWNTREND_adx_X`
- `hard_invariant_sticky_no_calls_after_uptrend_*` / `hard_invariant_sticky_no_puts_after_downtrend_*`
- `hard_invariant_no_calls_into_unfinished_high_*` / `hard_invariant_no_puts_into_unfinished_low_*`
- `day_structure_contradicts_bull_premium:*`
- `two_way_wait_no_puts_at_high_*` / `two_way_wait_no_calls_at_low_*` / `two_way_auction_wait_for_extreme_loc_*`
- `range_location_lean_*` / `range_soft_location_lean_*` / `range_wait_no_pin_no_lean_*`
- `range_origin_bull_deferred:*` / `range_origin_bear_deferred:*`
- `counter_trend_entry_blocked:*` / `slot_conflict:*` / `same_side_chase:*`

#### Entry rules

- `butterfly_spot_too_far_from_atm_*`, `butterfly_requires_pin_life_ge_*`, `butterfly_too_late_after_12:00`
- `butterfly_blocked_adx_*`, `butterfly_requires_mature_adx`, `butterfly_requires_narrow_or_*`
- `condor_needs_*min_before_exit_*`, `condor_blocked_strong_adx_*`, `condor_requires_mature_flat_adx`
- `condor_banned_on_two_way_auction`, `condor_requires_narrow_or_got_*`
- `condor_blocked_open_spike_wick_unresolved`, `condor_blocked_expanding_range_*`
- `condor_location_drift_*` / `condor_location_not_mid_*`
- `bull_put_spot_*_below_or_mid_*`, `bull_put_spot_below_vwap_*`
- `bear_call_spot_*_above_or_mid_*`, `bear_call_spot_within_25pts_of_max_pain_*`

#### Economics

- `gross_credit_*_non_positive`, `net_credit_*_non_positive_after_costs`
- `net_credit_*_friction_*_gt_28pct` (threshold is 28%, not 35%)
- `brokerage_*_gt_15pct`
- `*_wing_costs_*pct_of_short_premium_*`
- `credit_risk_ratio_*_below_min_*`
- `target_*_below_*x_roundtrip_friction`
- `ev_gate:ev_*_below_min_*`
- `risk_budget_allows_only_*_lots_below_min_0.60`
- `construct_fail_sticky:*`

#### Momentum

- `momentum_disabled`, `momentum_sell_side_open`, `momentum_dte_0_*` (economics / size — **not** a hard ban)
- `momentum_needs_trend_got_*`, `momentum_adx_*_below_min`
- `momentum_call_at_or_below_vwap`, `momentum_put_at_or_above_vwap`
- `momentum_iv_expanding_no_chase`, `momentum_day_move_used_*`
- `momentum_expected_capture_*_below_*x_friction`
- `momentum_before_entry_window_*`, `momentum_late_*`

---

### Appendix — Default knobs operators actually hit

| Knob | Default | Env |
|---|---|---|
| Paper mode | from env | `PAPER_TRADE_MODE` |
| Capital | ₹10,00,000 | `STARTING_CAPITAL` |
| Daily loss / per-trade | 8% / 2% | `MAX_DAILY_LOSS_PCT` / `MAX_RISK_PER_TRADE_PCT` |
| Concurrent / entries | 2 / 3 | `MAX_CONCURRENT_POSITIONS` / `MAX_ENTRIES_PER_DAY` |
| Correlated debit beside credit | **false** | `ALLOW_CORRELATED_DEBIT_BESIDE_CREDIT` |
| Same-cycle reentry | true | `ALLOW_SAME_CYCLE_REENTRY` |
| Force lots | off | `FORCE_LOTS` |
| ADX trend / strong | 20 / 28 | `ADX_TREND_THRESHOLD` / `ADX_STRONG_THRESHOLD` |
| Weekly window | 09:45–14:15 / 15:15 | `TRADING_WINDOW_*` / `HARD_EXIT_TIME` |
| 0DTE window | 10:30–13:00 / 15:00 | set by DTE in `data_engine` |
| Late momentum | 14:30–14:57 | `MOMENTUM_LATE_WINDOW_*` |
| Lot size / strike step | 65 / 50 | `NIFTY_LOT_SIZE` / `NIFTY_STRIKE_STEP` |
