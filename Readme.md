# NIFTY Intraday Options Engine

**Build:** `v65m57-ctrlc-no-trade` (printed by `MainEngine._print_startup_banner` in `main.py`)
**Invariant:** never open credit into a labelled threatened trend
**Live:** `python main.py`
**Replay:** `python backtest_engine.py` — same `MainEngine.run_one_cycle()`, not a second trading loop

One decision path serves live and replay:

```text
HistoricalStore → SimClock.set → ReplayClient.point → MainEngine.run_one_cycle()
```

Restart the process after a code change. The banner must show `v65m57-ctrlc-no-trade` and a PID start time at or after the fix. Entry rules do not flatten a position that was opened under older code; the exit ladder does.

---

## Debug a cycle in five minutes

Hunt the **first** decision that diverges. End-of-day rupees are the result, not the cause.

### 1. Read the reason the engine already wrote

Every `decide()` call stores a row in `strategy_decisions` and copies the same text onto the latest `cycle_log.no_trade_reason`.

```sql
SELECT decision_time, action, strategy_name, reason
FROM strategy_decisions
WHERE trading_date = '2026-10-09'
ORDER BY decision_id;
```

```sql
SELECT cycle_time, final_regime, price_regime, tape_state,
       action_taken, no_trade_reason
FROM cycle_log
WHERE trading_date = '2026-10-09'
ORDER BY cycle_id;
```

`signals_json` on `strategy_decisions` is the full signal dict for that cycle (spot, ADX, location inputs, impulse, day-move, tape). `params_json` is present only when a structure was built.

A refused sell that also refused the long-premium substitute looks like:

```text
<sell reason>|momentum_refused:<momentum skip reason>
```

`_with_momentum_refuse()` in `strategy_engine.py` appends the second half. Debug the left side first (why credit was refused), then the right side (why `LONG_CALL` / `LONG_PUT` did not replace it).

Closed trades live on `positions` (`status`, `strategy_name`, `entry_time`, `exit_time`, `exit_reason`). The useful detail is `exit_reason` plus the `reason_detail` embedded in it (`grind_over_exit_…`, `tape_adverse_impulse_…`, `adverse_occupancy_…`).

### 2. Jump from the reason prefix to the function

| Reason starts with | Open this |
|---|---|
| `feed_stale`, `ABORT`, `before_entry_window`, `past_entry_window`, `day_move_used`, `credit_impulse_against`, `spot_velocity`, `tape_when_`, `2_consecutive_stops`, `stop_cooldown`, `opening_range` | `StrategyEngine._check_hard_gates` |
| `entry_abort_storm_` | Latch is set in `_check_hard_gates`; the refuse fires in `decide()` via `_entry_abort_storm_blocks`. Family-only. Condor fallback is `_storm_fallback_iron_condor`. |
| `hard_invariant_` | `_hard_credit_into_trend_refusal` (called from `_counter_trend_entry_refusal`, so the log is often prefixed `counter_trend_entry_blocked:`) |
| `counter_trend_entry_blocked` | `_counter_trend_entry_refusal` — it also calls the spent-side and mid-fade helpers below |
| `spent_upside_chase_no_puts` | `_spent_upside_chase_put_refusal` |
| `morning_pullback_no_calls` | `_spent_upside_mid_call_refusal` |
| `spent_downside_bounce_no_calls` | `_spent_downside_bounce_refusal` |
| `spent_downside_bounce_no_puts` | `_spent_downside_bounce_put_refusal` |
| `mid_high_fade_into_bull_grind` | `_mid_range_fade_into_grind_refusal` |
| `post_debit_abort_no_` | `_post_debit_abort_credit_refusal` |
| `post_condor_no_vertical` | `_post_condor_mid_vertical_refusal` |
| `same_side_chase_` / `same_side_reload_` | `_same_side_chase_refusal` |
| `two_way_wait` / `two_way_auction` / `range_wait` / `range_location` | `_resolve_range_strategy` or `_map_regime_to_strategy` |
| `strategy_rules_failed` | `_validate_entry_rules` |
| `params_invalid` / `ev_gate` / `construct_fail_sticky` | `compute_params` → `_compute_ev_gate`; sticky latch is `_sticky_construct_reason` |
| `momentum_` | `_momentum_decision` → `_momentum_gate` |
| `tape_adverse_impulse` / `trend_flip_exit` / `adverse_flip_winner_harvest` / `put_fade_down_day` / `adverse_occupancy` / `grind_over_exit` | `ExecutionEngine.monitor_position` |
| `CLOSE_STOP` / `CLOSE_TARGET` / `HARD_EXIT` | same monitor; banked `CLOSE_TARGET` does not spend the two-stop halt |

### 3. Replay the day the live book saw

Use a per-day shard. Do not point a long replay at the live primary while `main.py` has it open.

```bash
python backtest_engine.py --db data/per_day/nifty_algo_2026-10-09.db --force-lots 0 --verbose
```

`--force-lots 0` is natural sizing. Default fill mode is `live` (expected broker touch). `--mirror-live-aborts` is off unless you pass it; a replay is supposed to test current code, not copy yesterday's broker rejects.

Live-parity preflight fails closed (exit 2) when yesterday's VIX, prior close, chain, or `session_state` is missing. `--allow-unfaithful` only suppresses that exit. That P&L is not go-live evidence.

### 4. What "no trade" usually means

| Symptom | First place to look |
|---|---|
| Banner build is older than this file | Process was not restarted. Entry fixes do not apply to an already-open position. |
| Sell reason plus `\|momentum_refused:` | Credit path refused, then debit path refused. Split on the pipe. |
| Whole session dark, VRP still rich, one family aborting | Abort storm is family-scoped. If the mapped name is the cooled family, `decide()` returns before rules/EV. A rich RANGE book can fall through to `IRON_CONDOR` via `_storm_fallback_iron_condor`. |
| Morning spent-high, lunch looks "unspent" | Gates that fence chase read `day_up_used_peak_pct` / `day_down_used_peak_pct`. Those peaks do not decay when the time-scaled gauge falls. |
| Credit refused, debit also dark after a fade scalp | `session_mean_reversion_book` blocks momentum until `_trend_continuation_clears_fade_latch` (strong ADX and the tape has actually reversed). |
| Green ticket held into the other side | Trend-flip only scratches underwater non-fades. `adverse_occupancy_*` is the slot-free for a wrong-side hold; `grind_over_exit_*` is the slow RANGE rollover. |
| Ctrl+C left a torn DB | Handler sets `running = False` and returns. It does not raise inside SQLite, and close does not `wal_checkpoint(TRUNCATE)`. |

---

## What changed since the last README (`v65m7s` → `v65m57`)

The previous README stamp was `v65m7s-credit-impulse-dte0` (2026-09-29). Current banner is `v65m57-ctrlc-no-trade`.

### Process and replay

- **Ctrl+C / SIGTERM** sets `running = False`. `run_one_cycle()` returns immediately, so a manual stop cannot still open or close while shutdown saves state. The handler does not raise `KeyboardInterrupt` inside a SQLite write.
- **Abort storm is family-scoped.** `_check_hard_gates` only latches `_entry_abort_storm_family`. `decide()` refuses that strategy name. Other structures stay eligible. Trigger: two same-family `ABORTED` rows within 10 minutes, or three within 20 minutes, then a 20-minute cooldown (`entry_abort_storm_cooldown_min`).
- **Condor fallback** when the cooled family is the only mapped vertical and vol is still a rich RANGE (`_storm_fallback_iron_condor`). Live 2026-10-01 lost a STRONG_SELL condor to a blanket BPS latch; that blanket is gone.
- **Replay fills default to `--fill-mode live`** (expected broker average after the aggressive limit). `--fill-edge` applies only with `--fill-mode edge` (default 0.0 = full spread). `--force-lots 0` overrides `env.txt` for that replay only. `--mirror-live-aborts` is opt-in.

### Data the gates read

- **`day_up_used_peak_pct` / `day_down_used_peak_pct`** (`data_engine.py`). Peak of the time-scaled gauge. A morning print above the fence stays spent after lunch even if the live percentage falls.
- **Parkinson RV immature window** is 90 HL bars, clamped to anchor + 1.00pp. A shorter clamp was flipping VRP negative around 10:00 and hard-blocking the book (`VOL_BUY_OPTIONS`).
- **Exhausted-move condor** (`regime_engine.py`): RANGE + NEUTRAL vol can still be `PREMIUM_SELL_RANGE` when VRP ≥ 1.0, `day_move_used_pct` ≥ 80, OR is VERY_NARROW / NARROW / MODERATE, and it is not an event day. Size discount `exhausted_move_condor_size` (0.60). The signal flag is `exhausted_move_condor`.

### Credit refusals added or tightened

All of these run inside `_counter_trend_entry_refusal` unless noted, so the logged prefix is `counter_trend_entry_blocked:<STRATEGY>:…`.

| Helper | Refuses |
|---|---|
| `_spent_upside_chase_put_refusal` | BPS glued to the high (off high < 40pts and loc ≥ 0.80). Pullbacks stay open. 0DTE HIGH + strong ADX + quiet IV under `PREMIUM_SELL_BULL` is the with-trend exception. |
| `_spent_upside_mid_call_refusal` | BCS on a morning pullback off a real upside grind, before 12:15. A synthetic `afternoon_high_fade` from OR-high reject does not waive it. |
| `_spent_downside_bounce_refusal` | BCS in the mid band (loc 0.30–0.75) after downside peak ≥ 100% while price is bouncing. Lows stay with-trend; highs stay fadeable. |
| `_spent_downside_bounce_put_refusal` | 0DTE BPS near the highs (loc ≥ 0.65) after the same spent downside. |
| `_mid_range_fade_into_grind_refusal` | BCS tagged high-fade while session loc < 0.70 and EMA is BULLISH or TRANSITIONAL above VWAP. |
| `_post_debit_abort_credit_refusal` | Opposite credit for 20 minutes after a long-premium abort (`LONG_PUT` abort blocks BCS, `LONG_CALL` abort blocks BPS). |
| `_post_condor_mid_vertical_refusal` | Non-fade vertical in the hour after a profitable condor `CLOSE_TARGET`. |
| `_same_side_chase_refusal` | After `CLOSE_STOP`, reloading puts at loc ≥ soft-lean high (0.58) or calls at loc ≤ soft-lean low (0.42). Tagged opposite extreme fades still pass. |

**Hard invariant waivers** (`_hard_credit_into_trend_refusal`):

- Bear-call into `UPTREND` / `STRONG_UPTREND`: day-structure bearish waives only when EMA is not `BULLISH` or `TRANSITIONAL`. A pinned high fade waives only through `_extreme_high_call_fade_ok` (loc ≥ 0.95, `afternoon_high_fade`, two-way auction, ADX below strong, not `STRONG_UPTREND`, clock ≥ 12:00).
- Bull-put into `DOWNTREND` / `STRONG_DOWNTREND`: waived only by `_low_fade_bounce_confirmed` (the fade has lifted off the low, or it is an OR-low reclaim). A tag sitting on the knife does not waive.
- Sticky latch still blocks a one-cycle RANGE flicker after a labelled refuse. It clears when ADX falls under the trend threshold, location leaves the extreme (calls loc < 0.70, puts loc > 0.30), or a real waiver appears.

**Day-move hard gate:** afternoon fades and away-side lean *presence* (`_away_side_lean_present`) waive the block. Booking still refuses a spent side later via `_away_side_allowed` and the helpers above. The waiver is not the same thing as permission to sell the spent side.

### Exits added inside `monitor_position`

Evaluated after the tape adverse-impulse flatten and the trend-flip block, before the premium price stop.

| `reason_detail` prefix | Action | When |
|---|---|---|
| `adverse_flip_winner_harvest_` | `CLOSE_TARGET` | Non-fade vertical, adverse (or sticky) trend, ADX ≥ 22, hold ≥ 15m, still green, peak gain ≥ 10% of credit, current gain ≤ 75% of that peak. |
| `put_fade_down_day_new_low_` | `CLOSE_STOP` | Low-fade BPS, already 5% underwater, hold ≥ 12m, session is red vs open by ≥ 50pts, day low is ≥ 40pts under entry spot. |
| `adverse_occupancy_` | `CLOSE_TARGET` | Non-fade vertical, mature ADX ≥ 22, hold ≥ 30m, signed impulse and location running against the credit side. Needs underwater, a 10% banked gain, or hold ≥ 55m. Frees the slot. |
| `grind_over_exit_` | `CLOSE_STOP` | Non-fade vertical, ADX ≥ 18, hold ≥ 15m, liquidation > 1.15× credit, session loc flipped against the side, range ≥ 85pts. Does not wait for a full trend label. |

Trend-flip itself is still the underwater measured-trend scratch (`trend_flip_exit_…`). Fades are excluded from these scratches.

### Momentum

- After a harvested fade, `session_mean_reversion_book` keeps debit dark. `_trend_continuation_clears_fade_latch` re-opens the aligned long when ADX is at the strong threshold and price/regime have reversed (low-fade then a real downtrend → puts; high-fade then a real uptrend → calls). A `STRONG_*` print with ADX ≥ max(strong, 50) also clears it.
- Two-way wait still skips debit on a choppy tape. A measured one-way (`ADX` ≥ strong and a trend label, not choppy) is allowed through `momentum_skipped_two_way_wait`.

---

## LONG_OPTIONS_ONLY tuning (2026-10-31)

With `LONG_OPTIONS_ONLY=true` the long-premium path is the book's only expression, not a substitute:

- `decide()` re-routes any credit ENTER through `_long_only_reroute` → `_momentum_decision` (reason prefix `long_only_credit_not_tradeable:`). Before, the credit ENTER was dropped in `main.py` and the long path was never consulted.
- `_momentum_gate` skips the sell-side marker check (`momentum_sell_side_open`) in long-only mode, and an opposing credit label no longer vetoes a long that matches the price label.
- `momentum_max_dte` 4 → 7 so a holiday-shifted week (10-19, 5 DTE) is tradeable.
- `momentum_reload_after_win` (1): one continuation clip after banked winners; on the same side it needs a fresh extreme beyond the last exit (`momentum_reload_needs_fresh_extreme`).
- Exit D1b `momentum_spot_invalidation_*`: `CLOSE_STOP` once spot is `momentum_spot_stop_pct` (0.33%) against a long ticket and the lock has not armed.
- Exit D4b `momentum_final_hour_cut_*`: with `momentum_final_cut_min` (60) or less to hard exit, a long ticket still below its entry value is closed (`CLOSE_STOP`). Tickets opened inside that window are exempt.
- Replay: `BT_KEEP_SCRATCH_DIR=<dir>` saves each day's book as `day_<date>.db`. Scratch `-wal/-shm` files are now removed on teardown.
- `momentum_counter_regime_size` (0.5): a long against the credit regime label (call under `PREMIUM_SELL_BEAR`, put under `PREMIUM_SELL_BULL`) trades at half the clamped size. It is applied after `FORCE_LOTS` via `params["size_scale"]`.

### Trend-first hybrid (`LONG_OPTIONS_ONLY=false`)

With the credit book on, `momentum_trend_first` (default true) makes the long path primary:

- The momentum gate skips the sell-side marker check, as in long-only mode.
- A qualifying long pre-empts a credit ENTER (`trend_first_over:`).
- **Flip.** Only credit verticals are open, the long qualifies, and the slot rule would refuse it. `_trend_first_flip` then sets `state["trend_first_flatten"]` and logs `trend_first_flip:<credit>-><long>`. The execution monitor closes those credits (`reason_detail=trend_first_flip`), and the long enters on the next cycle.
  - A flip close books P&L but leaves the `last_exit_*` / stop-streak memory unchanged. The long is gated on the same state it qualified on.
  - Same-side stacks the slot rule already allows keep their credit.
- Replay does not `PRAGMA quick_check` the live DB. That full-file scan stays on live startup (`Database`), and shards are integrity-checked when `split_db_per_day.py` replaces them. Preflight still point-reads yesterday's VIX and the prior close. The throwaway replay book uses an in-memory journal so cycle writes are not fsynced; the live primary stays WAL + `synchronous=FULL`.

36-day replay (2026-09-08..10-30, 12 lots):

| Book | Total | Days traded | Green | Red | Worst day |
|---|---:|---:|---:|---:|---:|
| long-only + counter-regime size | ₹458,070 | 16 | 13 | 3 | −₹14,219 |
| trend-first hybrid + flip | ₹552,043 | 33 | 25 | 8 | −₹18,372 |

Known weakness: the credit book sold bear-call spreads into the 10-28/10-29/10-30 bull legs (−₹31k together). Tape labels at entry (RANGE, ADX 18–27) match the winning credits, so no label filter separates them.

## Clean long-only module (`CLEAN_LONG_V1=true`)

The rules are fixed in `Misc/clean_long_v1_preregistration.md`, written before any run. They were not tuned on the shards. With the switch on, `decide()` calls only `_clean_long_decide`, and none of the momentum gates, regime map or credit book run.

- **Entry:** the first closed 5-minute bar between 09:45 and 13:30 that closes beyond the 09:15–09:44 opening range, with EMA9/EMA21 of 5-minute closes agreeing. It buys the ATM weekly option at the ask, once per day.
  - The signal must be the latest closed bar. A passed signal (after a feed gap or a refused entry) is not chased: `clean_long_signal_passed_<hh:mm>`.
- **Params:** `compute_momentum_params(..., strike_override=ATM, clean=True)` skips the momentum edge test and the risk-budget refusal. `FORCE_LOTS` sets the size.
- **Validation:** a clean entry uses the 09:45–13:35 window and no hard-exit buffer. The session's day-label window, such as 10:30–13:00 on 0 DTE, does not apply.
- **Exits:** `ExecutionEngine._monitor_clean_long` runs before every other exit rule:
  - the stop sits at the OR midpoint;
  - after +1R it trails at max(entry, best − 1R);
  - the position goes flat at 15:00.
  - R = |entry spot − OR mid|. The positions row carries the OR midpoint in `selection_reason` (`clean_long_v1:dir=…:or_mid=…`).

The variant was chosen on September only, then October was run once. Engine replay, 34 days, 12 lots:

| | Total | Green | Red | Flat | Worst day |
|---|---:|---:|---:|---:|---:|
| September (selection) | ₹235,983 | 7 | 2 | 6 | −₹29,233 |
| October (single test) | ₹426,677 | 9 | 9 | 1 | −₹48,027 |
| 34 days | ₹662,660 | 16 | 11 | 7 | −₹48,027 |

The engine matches the standalone replay within ₹270 a day. The exceptions are two feed-gap days, 09-09 and 10-07, where the replay chased a passed signal.

### v2 stop-and-reverse (`CLEAN_LONG_REVERSE`, default true)

If the day's first clean trade closes at a loss, one opposite-side breakout (same OR, same EMA rule, a bar closing after that exit, same exits and no-chase rule) may be taken. The selection reason carries `:rev=1`.

This rule was tuned on the same 34 days to reach the >17 profitable-day target, so it is **in-sample**. On September alone it was no better than v1b, which made more money there (₹234,836 against ₹209,515 in the standalone replay, with the same 7 green days). Engine replay, 12 lots:

| | Total | Green | Red | Flat | Worst day |
|---|---:|---:|---:|---:|---:|
| September | ₹235,983 | 7 | 2 | 6 | −₹29,233 |
| October | ₹530,084 | 11 | 7 | 1 | −₹56,988 |
| 34 days | ₹766,067 | 18 | 9 | 7 | −₹56,988 |

### Fixed-risk sizing (`CLEAN_LONG_RISK_RS`, default 0 = off)

A per-trade rupee budget, in two parts:

- **Sizing:** lots = risk / ((R × |delta| + ask − bid) × lot size + round-trip costs per lot). The inputs come from the entry quote, and the result is capped at `FORCE_LOTS` through `params["size_scale"]`.
- **Backstop:** `_monitor_clean_long` closes the trade (`clean_long_risk_cap`) once the open loss at the liquidation bid plus 2 × entry costs reaches the budget, wherever spot is.

The day can still lose two budgets when the reverse trade also loses.

| | Total | Green | Red | Sum of red days | Worst trade | Worst day |
|---|---:|---:|---:|---:|---:|---:|
| 12 lots always | ₹766,067 | 18 | 9 | −₹192,962 | −₹48,027 | −₹56,988 |
| `CLEAN_LONG_RISK_RS=20000` | ₹509,760 | 19 | 8 | −₹105,706 | −₹20,806 | −₹25,310 (10-28: −₹20,806 put, −₹4,504 reverse) |

### Breakeven stop (`CLEAN_LONG_BE_R`, default 0.5)

Once spot has moved 0.5R in favour, the clean stop is never worse than the entry spot. Failed breakouts that first ran 0.6–0.7R (09-30, 10-22) now exit near entry instead of at the OR midpoint.

It was chosen in the replay against time stops (10–30 min, 0.25–0.5R), which cut winners and helped only one month. The breakeven loss reduction held from 0.4R to 0.6R. Only 0.5R also raised October P&L, so the profit part is the fragile part. Engine replay with ₹20k risk:

| | Total | Green | Red | Sum of red days | Worst day |
|---|---:|---:|---:|---:|---:|
| Without breakeven | ₹509,760 | 19 | 8 | −₹105,706 | −₹25,310 |
| `CLEAN_LONG_BE_R=0.5` | ₹533,362 | 19 | 8 | −₹84,838 | −₹19,505 |

September red-day losses go from −₹39,551 to −₹23,553, and October from −₹66,155 to −₹61,285.

For the remaining failed-at-once breakouts (09-17, 10-05, 10-27, 10-28: ≤0.24R in favour), earlier ideas (2-bar OR buffer, scale-in, multi re-entry, tighter BE) were rejected — they cut 37–58% of profit or turned green days red.

### v3 structure filters (2026-11-01)

Three entry filters plus a confirmed mid-reclaim flip, chosen against those four red days without shrinking the big winners:

| Gate | Rule | Days it fixes |
|---|---|---|
| Exhausted CALL | Skip CALL when OR close loc ≥ `CLEAN_LONG_EXHAUST_OR_LOC` (0.85) and OR width ≤ `CLEAN_LONG_EXHAUST_OR_WIDTH` (120). Latch: no further first-trade CALLs that day; puts stay open. | 10-05, 10-27 (and 10-06) |
| Weak morning CALL | Skip CALL before 11:00 when OR loc ∈ [0.40, 0.60] and ext/OR-width < `CLEAN_LONG_WEAK_EXT_FRAC` (0.05). Same call-block latch. | 09-17 |
| Spent late PUT | Skip PUT when OR width ≥ 150 and signal time ∈ 10:15–11:00. | 10-28 |
| Failed-put flip | After a losing PUT, a CALL may enter on **two** consecutive 5m closes above mid with EMA bullish (`CLEAN_LONG_FAILED_FLIP`). Stop anchor = ORL (`stop=` in selection_reason). | accelerates 10-28 reclaim when the put was taken |

Engine replay, 12 lots, ₹20k risk, BE 0.5, 34 days:

| | Total | Green | Red | Flat | 09-17 | 10-05 | 10-27 | 10-28 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Before (v2 + BE 0.5) | ₹533,363 | 19 | 8 | 7 | −₹19,505 | −₹17,493 | −₹15,637 | −₹18,129 |
| After (v3 filters) | ₹611,964 | 19 | 6 | 9 | ₹0 | −₹303 | ₹0 | +₹2,677 |

Collateral: 09-23 goes flat (was +₹3,984) — same weak-morning CALL shape as 09-17. 10-30 improves (flip path). Rejected again: BE ≤ 0.30 (scratches 09-24), single-bar mid flip (whipsaws 10-28 11:25).

## Where the code lives

```text
bot_controller.py          Telegram /run /stop. Writes algo.stop. Not the trading loop.
main.py                    MainEngine.run_one_cycle  (live and replay)
  data_engine.py           Bars, chain, impulse, day-move, peaks, OR, VIX
  regime_engine.py         WHICH side → final_regime + size_multiplier
  tape_state_engine.py     WHEN → tape_state (fail-open NEUTRAL)
  strategy_engine.py       Gates, map, invariant, EV, momentum, decide()
  execution_engine.py      Place, reconcile, monitor_position, flatten
  calibration_engine.py    Rolling ~20-session shrinkage (instance in main.py)
core.py                    Config, Database, ExpiryCalendar, UpstoxClient, by_dte
backtest_engine.py         SimClock, ReplayClient, FillModel, preflight
telegram_reporter.py       Daemon thread. Never send on the trading thread.
restore_primary_db.py      Recover a corrupt primary. Refuse if main.py holds it.
split_db_per_day.py        Read-only snapshot → data/per_day/*.db
verify_all.py              Compile + static preflight. Does not start the loop.
tape_state_engine.py       WHEN self-test when run as main.
upstox_token.py            OAuth refresh.
stock-tsmom.py             Standalone stock utility. Not on the NIFTY cycle.
```

`verify_all.py` still lists `clean-db.py` (missing) and does not compile `tape_state_engine.py` or `restore_primary_db.py`. Run `python tape_state_engine.py` on its own.

### `run_one_cycle()` order

1. Return immediately if `running` is already false (Ctrl+C).
2. Day reset.
3. `MarketDataEngine.run_cycle()`.
4. `RegimeEngine.process_signals` + merge.
5. `TapeStateEngine.update` (fail-open `NEUTRAL`).
6. Patch `cycle_log` (regime + tape) and `market_snapshots` (regime only).
7. `monitor_all_positions` — always, including when new entries are blocked.
8. Hard-exit sweep, daily-loss halt.
9. `decide()` → `process_entry_decision`.
10. P&L, console trade report, live Telegram.

Cycle interval default is **15s** (`REGIME_CALC_INTERVAL_SEC`). A comment in `run_one_cycle` still says 45; trust `Config`.

Outer clock for calling `decide()` is 09:30 through the late-momentum end (14:57). Sell-side windows inside the hard gates are DTE-derived: **0DTE 10:30–13:00 / hard exit 15:00**, weekly **09:45–14:15 / hard exit 15:15**. Watchdog square-off deadline is 15:18. A cycle that is slow but still inside `run_one_cycle` is alive.

### `decide()` order

```text
_note_price
_check_hard_gates          → on NO_TRADE, try _momentum_decision
_map_regime_to_strategy    → on NO_TRADE, try momentum
_entry_abort_storm_blocks  → family only; else condor fallback or momentum
_build_decision_intent
_same_side_chase_refusal
_counter_trend_entry_refusal
_slot_conflict
_validate_entry_rules      → IC pin fail may demote to a vertical
_sticky_construct_reason
compute_params             → IC econ fail may demote once
ENTER or NO_TRADE
```

Stand-downs that do **not** consult momentum: `block_new_entries`, dead auth, missing spot, `AUTH_FEED_DEAD`, `VIX_EMERGENCY`.

Every other NO_TRADE path calls `_momentum_decision` before it returns. If debit also refuses, the stored reason gains `|momentum_refused:…`.

---

## Hard gates (`_check_hard_gates`)

Session clocks come from `state` (set by DTE in `data_engine`), not from weekday constants. Intent exemptions are post-selection. Hard gates do not call `_intent_exempt`.

| # | Token family | Meaning |
|---|---|---|
| 1 | `feed_stale_entries_blocked` | Watchdog / degraded feed |
| 2 | `ABORT:…` / `regime_unavailable` / `regime_engine_no_trade` | Upstream halt |
| 3 | `daily_loss_limit_reached_or_halted` | Daily halt. Default action flattens |
| 4 | `circuit_breaker_suspected` | Exchange halt / tick explosion |
| 5 | `vix_spike_detected` | India VIX spike |
| 6 | `iv_expanding_never_sell_into_rising_iv` / `iv_spiking` | Waived for afternoon fade or away-side intent |
| 7 | `before_entry_window_*` / `past_entry_window_*` | 0DTE 10:30–13:00. Weekly 09:45–14:15 |
| 8 | `max_concurrent_positions_reached` | OPEN+PENDING ≥ 2 |
| 9 | `max_entries_per_day_N_reached` | OPEN+CLOSED ≥ 3, +1 (cap 4) on a live two-way fade |
| 10 | `second_slot_cooldown_*` | Open book and < 10 min since last entry. Opposite extreme fade exempt |
| 11 | `entry_cooldown_*` | < 10 min since last act. Opposite rotation, extreme fade, pin→dir demotion exempt |
| 12 | `no_material_change_since_exit_*` | Spot move < max(0.12%, 15pts). After `CLOSE_STOP`, opposite credit uses `stop_opposite_reconfirm_min` (20) and may emit `tape_when_stop_opposite_*` |
| 13 | `2_consecutive_stops_halt` | Blotter streak ≥ 2. `CLOSE_TARGET` is not a stop |
| 14 | `same_signal_combo_caused_last_stop` | Same regime fingerprint |
| 15 | `stop_cooldown_*` | CLOSE_STOP 30m, CLOSE_ADX 45m, CLOSE_VWAP 20m, CLOSE_DELTA 30m |
| 16 | `spot_velocity_too_fast_*` | Abs 3-bar move vs VIX-scaled limit (floor 25pts). Two-way extreme fade exempt |
| 16b | `credit_impulse_against_bear_*` / `credit_impulse_against_bull_*` | Signed 3-bar impulse against the credit thesis. Same floor. Two-way extreme fade exempt |
| 16c | *(latch only)* | Abort storm recorded on signals. Enforced later, per family |
| 17 | `straddle_expanding_no_sell_into_rising_iv` | ATM straddle up > 6% vs ~5 min lookback. No IV-regime confirm |
| 18 | `opening_range_not_yet_computed` / `opening_range_pending` | OR not ready / price still `OBSERVING` |
| 19 | `open_spike_wait_unresolved_lower_high` | Open-high wick unresolved. Pullback 30pts or lower-high loc ≥ 0.70 after 10:45, else wait until 12:15 |
| 20 | `chain_stale_cannot_validate_strikes` | Chain age > 120s |
| 21 | `expiry_day_waiting_for_0dte_series_listed` | Tuesday morning, 0DTE chain not loaded |
| 22 | `confidence_*_insufficient_*` | LOW/NONE. Extreme fade / away-side intent can waive |
| 23 | `dte_X_above_max_4_*` / `dte_requires_confidence` | DTE > 4 blocked. DTE ≥ 4 needs HIGH |
| 24 | `day_move_used_*_no_edge` | Threat-side spend ≥ block. Condors use total range. See waiver note above |
| 25 | `only_Xmin_before_hard_exit_need_Y` | < 90 min, or < 50 min after 13:00 for directional / fade credit |
| 26 | `wide_or_*_dangerous_to_sell_premium` | WIDE / VERY_WIDE without a confirmed trend |
| 27 | `tape_when_*` | WHEN entry gate. Two-way extreme fades exempt. `NEUTRAL` and a disabled gate fail open |

`PENDING_ENTRY` counts toward concurrent slots. Successful OPEN+CLOSED count toward the daily entry cap. Aborted drafts do not burn the cap; the family storm cools re-fires.

### TapeState WHEN (first match)

| Label | Entry | Exit / size |
|---|---|---|
| `TURN_STARTING` | Block the old side | `tape_force_flat` can flatten a position-matched underwater ticket |
| `TREND_EXHAUSTED` | Block this-side chase | — |
| `BREAK_FROM_COIL` | Allow | — |
| `COIL` | Block directional. RANGE and fades stay | Latches for a later break |
| `TREND_STRENGTHENING` | Allow | Size × `tape_state_size_boost` (1.10, cap 1.15) |
| `TREND_ON` | Allow | — |
| `NEUTRAL` | Allow | Fail-open |

---

## Regime map

**Volatility:** `STRONG_SELL_PREMIUM`, `SELL_PREMIUM`, `BORDERLINE_SELL`, `NEUTRAL`, `BUY_OPTIONS`, `ABORT`
**Price:** `STRONG_UPTREND`, `UPTREND`, `RANGE`, `DOWNTREND`, `STRONG_DOWNTREND`, `CHOPPY`, `OBSERVING`
**Positioning:** `STRONG_RANGE`, `RANGE`, `BULLISH`, `BEARISH`, `UNCLEAR`
**Final:** `PREMIUM_SELL_RANGE`, `PREMIUM_SELL_BULL`, `PREMIUM_SELL_BEAR`, `NO_TRADE`, `ABORT`

There is no `MOMENTUM_BUY_*` final regime. Long premium is a strategy-layer substitute.

ADX defaults: trend 20, strong 28. Sell-regime cutoff is 14:30 (`sell_regime_cutoff_hhmm`). After that the classifier is `NO_TRADE` and only the late-momentum route can buy.

```text
PREMIUM_SELL_RANGE → _resolve_range_strategy()
PREMIUM_SELL_BULL  → hard invariant, then (price=RANGE defers to range),
                     day-structure bearish veto, two_way_wait_no_puts_at_high,
                     BULL_PUT_SPREAD
PREMIUM_SELL_BEAR  → hard invariant, mid-range fade-into-grind,
                     (price=RANGE defers), two_way_wait_no_calls_at_low,
                     BEAR_CALL_SPREAD
```

Two-way overlay when range ≥ 85pts and no fade flag yet: loc ≥ 0.80 forces BEAR, loc ≤ 0.20 forces BULL.

### Range ladder (`_resolve_range_strategy`, first match)

1. Structural bearish lean → `BEAR_CALL_SPREAD`. Friday / weekend-risk stays delta-neutral.
2. Confirmed two-way (range ≥ 85): loc ≥ 0.85 → BCS, loc ≤ 0.15 → BPS, else `two_way_auction_wait_for_extreme`.
3. Location lean (range ≥ 50, not an event day). Mature ADX ≥ 20 and positioning not UNCLEAR: loc ≥ 0.62 → BPS, loc ≤ 0.38 → BCS. Soft 0.58 / 0.42 needs `_soft_location_evidence`. loc ≥ 0.70 / ≤ 0.30 can lean alone.
4. True pin only: NARROW / VERY_NARROW OR, mature ADX in [12, 20), loc in (0.40, 0.60), session range < 100, sell-premium vol, before 12:00, no grind-away. Butterfly when `dte_blend ≥ 0.35`, ADX < 18, and |spot−ATM| < 50. Else condor.
5. Else `range_wait_no_pin_no_lean`.

```text
loc = (spot - eff_lo) / (eff_hi - eff_lo)
```

`eff_hi` / `eff_lo` strip open-spike wicks when the spike gap ≥ 15pts. `_session_range_pos` is the raw session location. Fade flags use `_fade_range_pos`. A disagreement between those two is what `_mid_range_fade_into_grind_refusal` exists to catch.

---

## Structures, costs, EV

| Strategy | Role | Legs |
|---|---|---|
| `IRON_CONDOR` | True pin only | 4 |
| `IRON_BUTTERFLY` | Near-expiry pin | 4 |
| `BULL_PUT_SPREAD` | With-trend / low fade / location lean | 2 |
| `BEAR_CALL_SPREAD` | With-trend / high fade / location lean | 2 |
| `LONG_CALL` / `LONG_PUT` | Momentum substitute | 1 |

Construction prices are quote-edge, not the replay fill model:

```text
SELL = bid (else LTP, else ask)
BUY  = ask (else LTP, else bid)
```

### Costs (Budget 2026 defaults)

| Item | Default | Env |
|---|---|---|
| Brokerage | ₹20 / order | `BROKERAGE_PER_ORDER` |
| STT (options sell) | 0.15% | `STT_OPTIONS_SELL` |
| Exchange txn | 0.03553% | `EXCHANGE_TXN_RATE` |
| SEBI | ₹10 / crore | `SEBI_RATE` |
| Stamp (buy) | 0.003% | `STAMP_DUTY_BUY_OPTIONS` |
| GST | 18% on brokerage + exchange + SEBI | hardcoded |

Entry slippage is half-spread × 0.35. Stress exit slippage is half-spread × 2.25.

Economic hurdles: net credit > 0; round-trip friction ≤ 28% of net credit; brokerage ≤ 15%; wing cost ≤ 50% near-dated / 58% weekly of short premium; credit/wing ≥ the DTE minimum (location-edge may use 0.04); target ≥ 1.25× round-trip friction.

EV is a 0.40 model / 0.30 prior / 0.30 market-delta blend. Min EV is max(net credit × 0.03, friction × 0.35). Location-edge credits may clear down to −friction. DTE ≥ 2 carry uses `ev_carry_discount_dte2p` (0.62).

```text
life = DTE + 0.5
w = (√2.5 − √life) / (√2.5 − √0.5)     # clamp [0, 1]
by_dte(DTE, V_expiry, V_weekly) = V_weekly + w · (V_expiry − V_weekly)
```

| | 0DTE | 1DTE | ≥ 2DTE |
|---|---|---|---|
| Session | 10:30–13:00, hard exit 15:00 | 09:45–14:15, hard exit 15:15 | same as 1DTE |
| Target % | 0.70 | ~0.52 | 0.40 |
| Stop × credit | 1.60 | ~1.66 | 1.70 |
| Lock arm | 40% | 22% weekly bar | 22%; after 13:30 → 15%; after 14:15 → 10%; IC/fly 10% |

NIFTY weekly expiry is Tuesday (Monday if Tuesday is a holiday). `ExpiryCalendar.get_dte` is trading-day DTE.

### Sizing

```text
StructuralLoss/lot = efficacy × Stop + (1 − efficacy) × Wing
MaxRisk = Capital × max_risk_per_trade_pct          # default 2%
SizedLots = (MaxRisk / StructuralLoss) × size_mult
```

`size_mult` comes from regime, then ×0.70 on a second open ticket, ×1.05–1.15 on extreme fades, ×1.10 (cap 1.15) on `TREND_STRENGTHENING`. Floor `min_lots_fraction` is 0.60. Defaults: capital ₹10,00,000, daily loss 8%, lot size 65. `FORCE_LOTS` pins size after the gates.

### Momentum substitute

Runs only when a sell-side refusal matches `momentum_block_markers`. DTE in [0, 4]. 0DTE uses half risk and a 2-lot cap. One momentum ticket per day, counted on fill.

Also refuses: puts into a rally / calls into a dump (`momentum_impulse_against_*`); 0DTE side-spend ≥ 100% with ADX < 50 (`momentum_dte0_exhausted_adx_*`); VIX gap > 12%; a mean-reversion book that continuation has not cleared. Late window is 14:30–14:57. Aligned debit beside an open credit is off unless `ALLOW_CORRELATED_DEBIT_BESIDE_CREDIT=true`.

### Exit ladder (credit)

| Order | Name | Reason |
|---|---|---|
| 1 | Delta breach | `CLOSE_STOP` |
| 2 | Spot proximity | `CLOSE_STOP` |
| 2.4 | Tape adverse impulse | `CLOSE_STOP` / `tape_adverse_impulse_*` |
| 2.5 | Trend-flip, then winner harvest | `CLOSE_STOP` or `CLOSE_TARGET` |
| 2.6 | Put-fade thesis break | `CLOSE_STOP` |
| 2.7 | Adverse occupancy | `CLOSE_TARGET` |
| 2.8 | Grind-over | `CLOSE_STOP` |
| 2.9 | Regime rotation | `CLOSE_TARGET` after `regime_rotation_min_hold_min` (25) |
| 3 | Premium stop | `CLOSE_STOP`, or `CLOSE_TARGET` if the profit lock is armed |
| 4 | Profit lock | arm / ratchet |
| 5 | Cheap buyback | short ≤ 5.0pts after 13:00 |
| 6 | Time target | `CLOSE_TARGET` |
| 7 | Hard exit | 15:00 on 0DTE, 15:15 weekly |

Debit HWM: Phase A free-trades after the lock trigger; Phase B trails once high-water is 2× entry. Hitting the armed debit trail is `CLOSE_TARGET`.

---

## Replay CLI

```bash
python backtest_engine.py [OPTIONS]
python split_db_per_day.py [--force] [--verify-only] [--dates YYYY-MM-DD ...] [--allow-unfaithful]
python restore_primary_db.py [--dry-run] [--force-shards]
python verify_all.py
python tape_state_engine.py
```

| Flag | Meaning |
|---|---|
| (no `--db`) | Healthy primary, else `data/per_day/` |
| `--db PATH [PATH ...]` | File, several files, or a directory of `nifty_algo_YYYY-MM-DD.db` |
| `--from` / `--to` | Inclusive dates |
| `--capital` | Override starting capital |
| `--fill-mode {live,edge}` | Default `live` (broker touch). `edge` is the legacy bid/ask blend |
| `--fill-edge` | Edge mode only. 0 = full spread, 0.5 = mid |
| `--stress-exit` | Edge mode only (default 0.50) |
| `--force-lots N` | This replay only. `0` = natural sizing |
| `--mirror-live-aborts` | Opt-in. Copy live `ABORTED` rejects. Leave off when testing new code |
| `--csv` | Blotter path |
| `--audit` | Coverage only |
| `--test` | Harness self-test |
| `--verbose` | Cycle log |
| `--allow-unfaithful` | Ignore live-parity FAIL |
| `--trade-report {each_cycle,on_change,off}` | Overrides `TRADE_REPORT_MODE` |

Primary DB default is `data/nifty_algo_v3.db`. WAL stays on disk (`wal_autocheckpoint=0`). Close does not TRUNCATE. Startup runs `PRAGMA quick_check`. If that fails, stop `main.py` and run `python restore_primary_db.py`.

---

## Operator knobs

| Knob | Default | Env |
|---|---|---|
| Paper mode | on unless live rates verified | `PAPER_TRADE_MODE` |
| Live rates verified | false (loader forces paper) | `LIVE_RATES_VERIFIED` |
| Capital / daily loss / per-trade | ₹10,00,000 / 8% / 2% | `STARTING_CAPITAL` / `MAX_DAILY_LOSS_PCT` / `MAX_RISK_PER_TRADE_PCT` |
| Concurrent / entries | 2 / 3 | `MAX_CONCURRENT_POSITIONS` / `MAX_ENTRIES_PER_DAY` |
| Cycle interval | 15s | `REGIME_CALC_INTERVAL_SEC` |
| Correlated debit beside credit | false | `ALLOW_CORRELATED_DEBIT_BESIDE_CREDIT` |
| Same-cycle reentry | true | `ALLOW_SAME_CYCLE_REENTRY` |
| Force lots | off | `FORCE_LOTS` |
| ADX trend / strong | 20 / 28 | `ADX_TREND_THRESHOLD` / `ADX_STRONG_THRESHOLD` |
| TapeState on / entry / exit / size | true | `TAPE_STATE_ENABLED` / `TAPE_STATE_ENTRY_GATE` / `TAPE_STATE_EXIT_GATE` / `TAPE_STATE_SIZE_NUDGE` |
| TapeState size boost | 1.10 (cap 1.15) | `TAPE_STATE_SIZE_BOOST` |
| Abort-storm cooldown | 20 min | Config field only (`entry_abort_storm_cooldown_min`). Not wired to `env.txt` |
| Post-debit-abort credit cooldown | 20 min | Config field only (`post_debit_abort_credit_cooldown_min`). Not wired to `env.txt` |
| Stop-opposite reconfirm | 20 min | `STOP_OPPOSITE_RECONFIRM_MIN` |
| Exhausted move (0DTE / 1DTE+) | 100% / 125% | `EXHAUSTED_MOVE_PCT` / `EXHAUSTED_MOVE_PCT_DTE1PLUS` |
| Lot size / strike step | 65 / 50 | `NIFTY_LOT_SIZE` / `NIFTY_STRIKE_STEP` |

Config is `env.txt` over OS env. Unknown keys are ignored. Holidays: `nse_holidays.json`. Events: `high_impact_events.json` (reloaded on mtime).

Telegram: `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. `/stop` writes `{LOG_DIR}/algo.stop`. The engine flattens (including `PENDING_ENTRY`) and exits without WAL TRUNCATE.
