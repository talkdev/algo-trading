# NIFTY Intraday Options Engine

Agent onboarding doc. Read this before editing gates, regimes, or exits.

| | |
|---|---|
| **Live** | `python main.py` |
| **Replay** | `python backtest_engine.py` — same `MainEngine.run_one_cycle()`, not a second strategy loop |
| **Build banner** | `MainEngine._print_startup_banner` in `main.py` (restart process after code changes) |
| **Primary DB** | `data/nifty_algo_v3.db` (`Config.db_path` / `DB_PATH`) |
| **Replay shards** | `data/per_day/nifty_algo_YYYY-MM-DD.db` via `split_db_per_day.py` |
| **Config** | `env.txt` → `core.load_config()` |

**Core invariant:** never open credit into a labelled threatened trend (`_hard_credit_into_trend_refusal`). Debit (LONG_CALL / LONG_PUT) is the dual-rail expression of that refuse when momentum qualifies.

---

## 1. What this engine is

Defined-risk NIFTY weekly options book for the Indian cash session (IST).

- **Credit rail:** iron condor / butterfly, bull put spread (BPS), bear call spread (BCS) — sell premium when regime says so.
- **Debit rail:** LONG_CALL / LONG_PUT — buy premium when credit is refused or trend-first displaces it.
- **One cycle path** for live and backtest: data → regime → tape → `strategy.decide` → `execution` monitor/entry.

Paper vs live is `PAPER_TRADE_MODE` / broker client; strategy logic does not fork.

---

## 2. Repository map (who owns what)

| File | Role |
|---|---|
| `main.py` | `MainEngine` — startup, `run_one_cycle()`, EOD, Ctrl+C halt |
| `core.py` | `Config`, `Database`, Upstox client, calendars, schema, `load_config` |
| `data_engine.py` | Spot/VIX/chain/OR/ADX/VWAP/day-move/`struct_trend` → `signals` dict + `market_engine.state` |
| `regime_engine.py` | Vol / price / positioning / final regime + confidence + size multiplier |
| `calibration_engine.py` | Self-tuning thresholds persisted in DB |
| `tape_state_engine.py` | WHEN-layer tape label (`TREND_ON`, `TREND_EXHAUSTED`, …) + entry nudges |
| `strategy_engine.py` | Dual-rail `decide()` — gates, map, params, momentum, floors |
| `execution_engine.py` | Entry fills + credit/debit exit ladders + hard-exit sweep |
| `backtest_engine.py` | Replay harness: shards, SimClock, fill models, parallel days |
| `telegram_reporter.py` | Async lifecycle/trade Telegram (optional) |
| `upstox_token.py` | Token refresh helper |
| `split_db_per_day.py` | Split live DB into per-day replay shards |
| `nse_holidays.json` / `high_impact_events.json` | Calendar / event-day inputs |
| `Misc/` | Scratch probes, one-off diagnostics — not production path |

Unrelated screener scripts (`etf-momentum-screener.py`, etc.) are not part of the options loop.

---

## 3. Runtime object graph

```text
MainEngine
├── Config (+ env.txt)
├── Database (SQLite WAL live; scratch DB in replay)
├── UpstoxClient | ReplayClient
├── MarketDataEngine   → signals + state
├── CalibrationEngine  → threshold overrides
├── RegimeEngine       → final_regime, confidence, size_multiplier
├── TapeStateEngine    → tape_state (merged into signals)
├── StrategyEngine     → ENTER | NO_TRADE (+ params)
└── ExecutionEngine    → open/monitor/close positions
```

Shared mutable session memory: `market_engine.state` (and mirrored columns on `session_state`).

---

## 4. One cycle (live = replay)

`MainEngine.run_one_cycle()` (~every `regime_calc_interval_sec`, default ~45s):

```text
1. New-day reset if date rolled
2. MarketDataEngine.run_cycle()     → signals (spot, VIX, chain, OR, ADX, day-move, struct_…)
3. RegimeEngine                     → vol/price/pos/final + confidence + size
4. merge_regime_into_signals()
5. TapeStateEngine.update()         → tape_state
6. ExecutionEngine.monitor_*        → exits / tighten / flatten (open book first)
7. Hard-exit sweep near session end
8. Daily loss halt check
9. If flat-enough for entries: StrategyEngine.decide(signals)
10. If ENTER → ExecutionEngine.execute_entry
11. Persist cycle_log + strategy_decisions; console/Telegram
```

**Replay wiring:**

```text
HistoricalStore (shard) → SimClock.set(t) → ReplayClient.point(t)
  → MainEngine.run_one_cycle()   # identical strategy/execution code
```

Restart the process after code changes. Entry fixes do not rewrite an already-open position’s thesis; the exit ladder still applies.

---

## 5. Dual-rail decision model

```text
                    signals + final_regime
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
     CREDIT MAP (BPS/BCS/IC)           DEBIT (LONG_*)
     regime → structure                 _momentum_gate
     hard invariants                    trend-first / substitute
     bootstrap / EV / construct         DTE / ADX / spent / window
              │                               │
              └──────── decide() ─────────────┘
                              │
                     ENTER + params  or  NO_TRADE + reason
```

| Mode | Config | Behaviour |
|---|---|---|
| Hybrid (default) | `momentum_trend_first=true`, credit on | Qualifying long can pre-empt credit ENTER; may flatten open verticals (`trend_first_flip`) when STRONG |
| Long-only | `LONG_OPTIONS_ONLY=true` | Credit ENTER re-routed through `_long_only_reroute` → momentum |
| Clean long v1 | `CLEAN_LONG_V1=true` | Bypasses regime/momentum; `_clean_long_decide` only |

Default book is **hybrid dual-rail**.

---

## 6. Regime taxonomy (`regime_engine.py`)

### Volatility
`STRONG_SELL_PREMIUM` → `SELL_PREMIUM` → `BORDERLINE_SELL` → `NEUTRAL` → `BUY_OPTIONS` → `ABORT`  
(VRP / IV behavior / OR width / data-error guards)

### Price (`classify_price`) — hard stack, not a vote
1. OR not set → `OBSERVING`
2. Structural trend (`struct_trend` + OR break) → `UPTREND` / `DOWNTREND` / `STRONG_*`
3. CHOPPY unless mature ADX + OR break + same-side VWAP
4. Immature ADX → ORB only if VWAP same side, else `RANGE`
5. STRONG / plain trend via ADX + EMA + OR displacement
6. Stale-extreme reclaim can demote false trends (distance + VWAP/EMA flip, or large distance alone)
7. Else `RANGE`

### Final (`classify_final`) → strategy map
| FinalRegime | Typical structure |
|---|---|
| `PREMIUM_SELL_BULL` | `BULL_PUT_SPREAD` |
| `PREMIUM_SELL_BEAR` | `BEAR_CALL_SPREAD` |
| `PREMIUM_SELL_RANGE` | `IRON_CONDOR` / fly / containment shapes |
| `NO_TRADE` / `ABORT` | Stand-down (debit may still answer sell-side refuses) |

Afternoon extreme fades can mint BEAR/BULL against a soft trend label; measured-trend vetoes exist. Persistence requires consecutive cycles for sell regimes (accelerated on with-trend OR break).

### Confidence
Weighted blend of vol/price/positioning/IV/OR. Directional OR-break days score with RANGE (not below it). `EXPANDING` / `SPIKING` IV → low/none for selling.

---

## 7. Strategy engine — `decide()` spine

Entry: `StrategyEngine.decide` → (optional clean-long) → `_trend_first_flip` → `_decide_core` → trend-first-over-credit → participation / session floor if still `NO_TRADE` and flat.

### `_decide_core` order of battle
1. `_check_hard_gates` — window, feed, halt, green/loss lock, IV expand, concurrency, cooldowns, …
2. On many `NO_TRADE` reasons: consult `_momentum_decision` (debit substitute) unless stand-down / danger
3. Map `final_regime` → structure (`_map_regime_to_strategy`)
4. Counter-trend / spent-side / hard-invariant refuses
5. `compute_params` (strikes, lots, EV, stops) — **also re-checks hard credit invariants**
6. Bootstrap / envelope / slot rules
7. `ENTER` or `NO_TRADE`

### Structures
| Name | Rail |
|---|---|
| `IRON_CONDOR`, `IRON_BUTTERFLY` | Credit range |
| `BULL_PUT_SPREAD` | Credit bull |
| `BEAR_CALL_SPREAD` | Credit bear |
| `LONG_CALL`, `LONG_PUT` | Debit momentum |

### Hard credit invariant (must know)
`_hard_credit_into_trend_refusal` (also invoked from `compute_params` so floor/solvency paths cannot bypass):

- No BCS into labelled `UPTREND` / `STRONG_UPTREND` (narrow fade / structure waivers)
- No BPS into labelled `DOWNTREND` / `STRONG_DOWNTREND` (bounce-confirmed waivers)
- Afternoon BCS without pinned extreme fade (`_extreme_high_call_fade_ok`) refused (session-floor mid-box after 14:00 excepted)
- Afternoon BPS without low-fade / STRONG-up-at-lows refused (same floor mid-box exception)
- Spent upside/downside chase refuses

Logged often as `counter_trend_entry_blocked:…` or `params_invalid:hard_invariant_…`.

### Green / loss locks
- Flat book + `daily_pnl >= green_day_lock_min_rs` → `green_day_lock_no_new_entries` (no new risk)
- Flat book + material red → `loss_day_lock_no_revenge_entries`
- Trend longs that should own the day must **displace open credit before the bank** (`_trend_first_over_credit` / `_trend_first_flip`), not punch through a locked green day

### Momentum / debit (`_momentum_gate`)
Needs direction from price/structure/hard-bridge, ADX/OR/VWAP evidence, not spent-extreme wipe class, window/`momentum_min_minutes_left`, premium band, IV/VIX guards.  
After ~12:00: STRONG or `TREND_ON`/`TREND_STRENGTHENING` or `struct_ok` at confirm ADX (puts stricter without STRONG/structure).

### Session solvency
If still unbanked after noon-ish: `_session_participation_decision` (IC / debit / vertical) then `_session_floor_*` (WIDE credit, late theta, containment, debit lane). Floor paths set `signals["_session_floor"]=True` while building params.

---

## 8. Execution — exits

### Credit vertical / condor (`monitor_position`)
Priority stack (lowest number wins when multiple fire):

| Pri | Name | Typical close |
|---:|---|---|
| 1 | DELTA_BREACH | CLOSE_STOP |
| 2 | SPOT_PROXIMITY | CLOSE_STOP |
| 3 | PRICE_STOP | CLOSE_STOP |
| 4 | PROFIT_LOCK | CLOSE_TARGET |
| 5 | CHEAP_BUYBACK | CLOSE_TARGET |
| 6 | TIME_TARGET | CLOSE_TARGET |
| 7 | HARD_EXIT | HARD_EXIT_15:00 |

Also: tape adverse impulse, trend-flip scratch, adverse occupancy, grind-over, fade-specific stops. See `execution_engine.py`.

### Debit long ladder (`_monitor_debit_position`)
| Step | Role |
|---|---|
| D1 | Premium stop / trail |
| D1b | Spot thesis invalidation (`momentum_spot_stop_pct`) |
| D2 | HWM free-trade / soft / fat trail (`momentum_hwm_*`, persist holds BE clamp into afternoon) |
| D2b | Aged-peak fade harvest |
| D3 | Planned target (`entry × (1 + momentum_target_frac)`) — bank on pullback / thesis death |
| D4 / D4b | Final-window flatten / underwater cut |
| D5 | Hard exit clock |

`trend_first_flatten` in state: execution closes listed credit IDs so the long can enter next cycle.

---

## 9. Clocks and windows (IST defaults)

| Knob | Typical default | Meaning |
|---|---|---|
| OR build | ~09:15–09:30 | Opening range |
| `trading_window_start` | 09:30 | Credit/regime entries |
| `trading_window_last_entry` | 15:12 | Last credit-ish entry |
| `hard_exit_time` | 15:15 | Forced flat (before broker ~15:20 RMS) |
| `momentum_late_window_*` | 15:12–15:15 | Closing-hour debit abutting sell cutoff |
| `sell_regime_cutoff_hhmm` | = late start | Credit map stands down |
| `momentum_min_minutes_left` | ~28–35 | Debit needs room to hard exit |
| `afternoon_credit_min_minutes_left` | ~25 | No late credit scratches |

0DTE / Tuesday use tighter exits (`tuesday_hard_exit`, expiry window blend).

---

## 10. Database tables agents query first

| Table | Use |
|---|---|
| `strategy_decisions` | Every decide: `action`, `strategy_name`, `reason`, `signals_json`, `params_json` |
| `cycle_log` | Per cycle: regimes, tape, `action_taken`, `no_trade_reason` |
| `positions` / `position_legs` | Trades: entry/exit, `exit_reason`, PnL, HWM fields |
| `session_state` | Live session memory / peaks |
| `daily_summary` | Day PnL rollup |
| `vix_history` | Prev VIX for gaps |
| `intraday_candles` / chain tables | Replay inputs |

```sql
-- Why no trade / what was selected
SELECT decision_time, action, strategy_name, reason
FROM strategy_decisions
WHERE trading_date = 'YYYY-MM-DD'
ORDER BY decision_id;

-- Cycle tape + regime
SELECT cycle_time, final_regime, price_regime, tape_state,
       action_taken, no_trade_reason
FROM cycle_log
WHERE trading_date = 'YYYY-MM-DD'
ORDER BY cycle_id;

-- Closed book
SELECT entry_time, exit_time, strategy_name, net_pnl_rupees, exit_reason,
       price_regime_at_entry, selection_reason
FROM positions
WHERE trading_date = 'YYYY-MM-DD' AND status = 'CLOSED'
ORDER BY entry_time;
```

Credit refuse that also refused debit:

```text
<sell_reason>|momentum_refused:<momentum_skip>
```

Split on `|momentum_refused:` — fix left (credit) then right (debit).

---

## 11. Reason prefix → code

| Reason starts with | Open |
|---|---|
| `feed_stale`, `ABORT`, `before_entry_window`, `past_entry_window`, `green_day_lock`, `loss_day_lock`, `2_consecutive_stops`, `stop_cooldown` | `StrategyEngine._check_hard_gates` |
| `hard_invariant_` | `_hard_credit_into_trend_refusal` (also `compute_params`) |
| `counter_trend_entry_blocked` | `_counter_trend_entry_refusal` (+ spent/mid-fade helpers) |
| `spent_upside_`, `spent_downside_`, `morning_pullback_no_calls` | spent-side helpers in `strategy_engine.py` |
| `bootstrap_credit_` / `bootstrap_momentum_` | `_bootstrap_hot_credit_refusal` / `_bootstrap_momentum_refusal` |
| `params_invalid` / `ev_gate` / `construct_fail` | `compute_params` / sticky construct |
| `momentum_` | `_momentum_gate` / `_momentum_decision` |
| `trend_first_over:` / `trend_first_flip:` | `_trend_first_over_credit` / `_trend_first_flip` |
| `participation_` / `session_floor_` | solvency / floor helpers |
| `tape_when_` / `TREND_EXHAUSTED` | `tape_state_engine` + strategy tape gates |
| `CLOSE_STOP` / `CLOSE_TARGET` / `HARD_EXIT` / `momentum_*_trail` | `ExecutionEngine` monitor |

---

## 12. How to run

### Live
```bash
# token + env.txt configured
python main.py
```

### Replay one day (prefer shard; do not fight live WAL)
```bash
python backtest_engine.py --db data/per_day/nifty_algo_2026-10-09.db --force-lots 0 --verbose
```

### Date range (parallel)
```bash
python backtest_engine.py --from 2026-09-09 --to 2026-11-13 --force-lots 0 --workers 4 --allow-unfaithful
```

| Flag | Meaning |
|---|---|
| `--force-lots 0` | Natural sizing (ignore forced lot env for that run) |
| `--workers N` | Parallel day processes |
| `--allow-unfaithful` | Ignore live-parity preflight failures (not go-live evidence) |
| `--fill-mode live` | Default fill model (broker-like) |
| `--verbose` | Louder cycle/trade output |

Refresh shards after live capture:

```bash
python split_db_per_day.py
# or --force to rebuild
```

---

## 13. Troubleshooting playbook (agent)

1. **Confirm build** — startup banner matches expected stamp; process restarted after edits.
2. **Pick one day** — end-of-day ₹ is the result; hunt the first wrong `strategy_decisions.reason`.
3. **Classify the failure**
   - Never traded → hard gates / regime NO_TRADE / both rails refused
   - Wrong structure → regime map or fade tree
   - Right structure, bad fill/exit → execution ladder / HWM / hard exit
   - Small long day → debit exits too early (D2/D3) or green lock after micro credit
   - Red after green credit → debit wipe through green lock (should be locked) or flip quality
4. **Replay that shard only** with `--force-lots 0 --workers 1 --verbose`.
5. **Do not day-patch** — prefer systemic invariants (hard credit into trend, afternoon fade fences, spend peaks) over date-named exceptions.
6. **Peaks do not decay** — `day_up_used_peak_pct` / `day_down_used_peak_pct` stay high after a morning chase even if live `%` cools.
7. **Floor ≠ map** — session floor / participation used to bypass invariants; `compute_params` now re-checks hard credit refuses. If a credit still appears after a refuse, search paths that skip `compute_params` (should be none for verticals).

### Common symptoms

| Symptom | First look |
|---|---|
| Credit + `\|momentum_refused:` | Split reason; credit then debit |
| Session flat, VRP rich | Abort storm family / CHOPPY / WIDE stand-down / bootstrap |
| Long dark all morning | ADX maturity, spent-extreme, sell-side markers, window |
| Long small on trend day | `momentum_target_frac` / HWM soft trail / persist OFF / green lock after scalp |
| Afternoon BCS/BPS stop | `_hard_credit_into_trend_refusal` afternoon fade rules |
| IC then huge debit loss | IC→long flip policy; prefer not flipping soft IC |
| Replay ≠ live PnL | Shard stale, `--allow-unfaithful`, fill mode, open live writer lock |

---

## 14. Important signals (names in `signals` / `signals_json`)

| Key | Meaning |
|---|---|
| `spot`, `vix`, `actual_dte`, `active_expiry` | Market identity |
| `or_high` / `or_low` / `or_computed` / `or_condition` | Opening range |
| `adx_15`, `adx_15_mature`, `ema_structure`, `vwap_signal` | Trend stack |
| `struct_trend`, `struct_strength` | Price-only structure (no ADX wait) |
| `day_move_used_pct`, `day_up_used_*`, `day_down_used_*`, `*_peak_pct` | Spend / chase |
| `vol_regime`, `iv_behavior`, `price_regime`, `final_regime` | Regime outputs |
| `confidence_level`, `size_multiplier` | Confidence / size |
| `tape_state` | WHEN layer |
| `afternoon_high_fade` / `afternoon_low_fade` / `two_way_auction` | Fade tags |
| `_session_floor`, `_momentum_hard_trend_sub` | Internal path flags |

---

## 15. Safety / ops notes

- Ctrl+C sets `running=False` and returns; avoids raising inside SQLite writes.
- Live DB: WAL + careful locking. Replay uses throwaway scratch DBs; optional `BT_KEEP_SCRATCH_DIR` to keep day books.
- Do not run a long replay against the live primary while `main.py` holds it — use `data/per_day/`.
- Broker intraday F&O RMS ~15:20; hard exit is intentionally before that race.
- Event days: `high_impact_events.json` + higher debit ADX bars.

---

## 16. Quick mental model

```text
DATA builds truth (OR, ADX, spend, structure)
  → REGIME names the sell book (or NO_TRADE)
    → TAPE says when (trend on / exhausted / coil)
      → STRATEGY chooses credit XOR debit (dual-rail)
        → EXECUTION manages risk on the clock and the premium
```

If you only remember four functions:

1. `data_engine.MarketDataEngine.run_cycle`
2. `regime_engine.RegimeClassifier.classify_final` / `classify_price`
3. `strategy_engine.StrategyEngine.decide`
4. `execution_engine.ExecutionEngine.monitor_position` (+ `_monitor_debit_position`)

Start every investigation at `strategy_decisions.reason` for the first bad cycle.
