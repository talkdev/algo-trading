
# NIFTY Intraday Options Algo-Trading Engine — Codebase Guide

> **Audience:** an AI assistant (or engineer) picking up this repo cold and expected to ship code changes fast. This doc trades prose for density. Read section 1 before touching anything — the repo is **mid-migration** and half the files can't actually import each other today.

---

## 1. READ THIS FIRST — the repo has two generations of code mixed together

There are **three overlapping, partially-incompatible implementations** of the same trading engine living in this repo simultaneously. Before making any change, know which family a file belongs to.

### Family A — "5-file" legacy engine (imports don't resolve)

`main.py`, `strategy_engine.py`, `execution_engine.py`, and (partially) `regime_engine.py` all contain headers like `FILE 3 of 5`, `FILE 4 of 5`, `FILE 5 of 5` and do:

```python
from nifty_algo_core import (...)
from market_data_engine import MarketDataEngine, ensure_column
```

**`nifty_algo_core.py` and `market_data_engine.py` do not exist anywhere in this repo.** These four files cannot be imported or run as-is. Their "signals" schema also uses a different vocabulary than the newer files: `volatility_condition`, `trend_condition`, `direction`, `vwap_signal`, `pcr_signal`, `skew_signal`, `preferred_sell_side`, `adx` (singular).

### Family B — "v3.0 / v4.1" regime-based engine (internally consistent, self-contained)

`core.py`, `data_engine.py`, `calibration_engine.py` all do `from core import (...)` / `import data_engine` and are mutually consistent. Their "signals" vocabulary is different again: `vol_regime`, `price_regime`, `positioning_regime`, `final_regime`, `vrp_raw`, `vrp_smoothed`, `confidence_level`.

### Family C — the "real" wiring, as evidenced by `backtest_engine.py`

`backtest_engine.py` is the most recent and most self-aware file in the repo (see its long header comment). It imports:

```python
import core, data_engine, regime_engine, strategy_engine, execution_engine, calibration_engine
```

and constructs them as:

```python
me = data_engine.MarketDataEngine(config, db, client, rl, logger)
ce = calibration_engine.CalibrationEngine(db, config, logger)
se = strategy_engine.StrategyEngine(config, db, me, ce, logger)      # <-- 5 args, includes ce
xe = execution_engine.ExecutionEngine(config, db, me, ce, client, logger)  # <-- 6 args, includes ce
rg = regime_engine.RegimeEngine(config, db, me, logger)
```

This tells you the **intended target architecture**: `core.py` + `data_engine.py` + `regime_engine.py` + `calibration_engine.py` + a `strategy_engine.py`/`execution_engine.py` that accept a `CalibrationEngine` instance. **The `strategy_engine.py` and `execution_engine.py` files currently in the repo do not match this signature** (they take `(config, db, market_engine, logger)` with no `ce`, and they import from the missing `nifty_algo_core`/`market_data_engine`). `regime_engine.py` also still imports from the missing legacy modules even though `backtest_engine.py` expects to `import regime_engine` as a `core`/`data_engine`-family module.

**Practical implication for any task in this repo:**

- If asked to "fix a bug" or "add a feature" to `strategy_engine.py`, `execution_engine.py`, or `regime_engine.py`, first check whether the fix is about the *logic* (safe to do in place) or about *making the engine runnable end-to-end* (requires reconciling imports/signatures against `core.py`/`data_engine.py`/`calibration_engine.py`/`backtest_engine.py`'s expectations first — this is probably the single highest-leverage fix available in the repo).
- `main.py` as it stands **cannot run** (`ModuleNotFoundError: nifty_algo_core`). If asked to "run the engine," you likely need to either (a) point the person at `backtest_engine.py`'s harness/self-test instead, or (b) rewrite `main.py` to wire `core`/`data_engine`/`regime_engine`/`calibration_engine`/`strategy_engine`/`execution_engine` together the way `backtest_engine.py` does.
- `eod_report.py` is schema-agnostic (reads via `sqlite3` directly, `SELECT *`, tolerates missing columns) — it works regardless of which family produced the DB, so it's safe to treat as a stable utility.

---

## 2. Domain in one paragraph

This is a systematic options-selling (and occasionally options-buying) algo for **NIFTY 50 index weekly options** on the Indian NSE, trading through the **Upstox** broker API. It runs a 5-minute decision loop during market hours (09:15–15:30 IST), classifies the day's volatility/trend/positioning "regime," decides whether to sell premium (iron condor, iron butterfly, credit spreads) or buy it (debit spreads, straddles) or stand aside, sizes the trade against a risk-per-trade/risk-per-day budget, executes (paper or live), and manages exits via a priority ladder (premium stop, price stop, profit target, profit-lock trailing stop, VWAP breach, ADX breach, delta breach, hard time exit). Everything is logged to SQLite for a nightly forensic report and for self-calibration of thresholds from historical performance.

Core edge thesis: **sell premium when implied vol (from the option chain) is rich relative to realized vol (Parkinson estimator on 1-min bars)** — this gap is called **VRP** (variance/volatility risk premium) throughout the code, expressed in "percentage points" (pp).

---

## 3. File-by-file map

| File                                       | Family              | Role                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------------ | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core.py`                                | B                   | Foundation:`Config` dataclass + loader (`load_config`), `Database` (SQLite wrapper + full schema + migrations), `RateLimiter`, `UpstoxClient` (REST wrapper), `ExpiryCalendar` (NSE Tuesday-weekly expiry/DTE/holiday logic), logging setup, `now_ist`/`today_ist`, print helpers. **Everything else in Family B/C imports from here.**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `data_engine.py`                         | B                   | `MarketDataEngine` — the per-cycle data pipeline: fetch spot/VIX, candles, option chain; compute ATM IV, PCR, OI walls/max-pain, skew, Parkinson RV, **VRP raw+smoothed**, IV behavior vs. session open, opening range (OR), gap detection, day-move-used-vs-priced-range, VWAP, ADX/EMA/HH-HL (via nested `TechnicalEngine`). Owns `session_state` persistence (mid-day-restart-safe). `run_cycle()` returns the `signals` dict everything downstream consumes.                                                                                                                                                                                                                                                                                                                                                                                                 |
| `regime_engine.py`                       | mixed — see §1    | Classifies`VolatilityRegime` / `PriceRegime` / `PositioningRegime` → `FinalRegime` (e.g. `PREMIUM_SELL_RANGE`, `BUY_STRADDLE`, `NO_TRADE`, `EMERGENCY_EXIT`) with a `ConfidenceLevel` and a `size_multiplier`. Contains its **own** embedded `CalibrationEngine`/`AutoCalibrator` (percentile-based, separate from and incompatible with `calibration_engine.py`'s Bayesian-shrinkage one — same `calibration_state` table, overlapping-but-different columns). Imports from the missing `nifty_algo_core`/`market_data_engine` — needs an import fix to actually run.                                                                                                                                                                                                                                                                   |
| `strategy_engine.py`                     | A (broken imports)  | `StrategyEngine.decide(signals) -> {"action": "ENTER"/"NO_TRADE", ...}`. Hard no-trade gates → VRP×trend×direction strategy-selection matrix (iron condor/butterfly, bull-put/bear-call spread, bull-call/bear-put spread, long straddle, post-event straddle) → post-selection downgrades (straddle→condor if not allowed, condor→spread on direction shift, half-sizing) → strategy-specific entry rules → full parameter computation (strike selection by delta, credit/debit, transaction costs, position sizing, stop/target, margin estimate).                                                                                                                                                                                                                                                                                                                    |
| `execution_engine.py`                    | A (broken imports)  | `ExecutionEngine` — pre-trade validation (capital/daily-loss, portfolio delta, liquidity re-check, price-drift re-check, time window), `PaperOrderExecutor`/`LiveOrderExecutor` (paper mode is the safe default; `LiveOrderExecutor` refuses to construct if `paper_trade_mode=True`), position monitoring (the exit-priority ladder), position close/partial-close (`close_one_side` for condor/butterfly leg-by-leg), transaction cost computation, emergency unwind on partial-fill failure.                                                                                                                                                                                                                                                                                                                                                                      |
| `calibration_engine.py`                  | B                   | Standalone Bayesian-shrinkage`CalibrationEngine` (imports from `core`). Tiered calibration (Tier 0 defaults → Tier 3 robust, 60+ days). Four feedback loops: VRP threshold tuning from win-rate-by-bucket, phantom-trade analysis (were NEUTRAL-blocked trades actually profitable?), exit-quality analysis (premature/late exits), signal-weight learning. Also does drift detection and phantom/exit-quality/regime-accuracy EOD scoring jobs. **This is the calibration engine `backtest_engine.py` actually wires in** — not the one embedded in `regime_engine.py`.                                                                                                                                                                                                                                                                                          |
| `backtest_engine.py`                     | C                   | Event-driven replay simulator. Replays recorded`option_chain_snapshot` + `intraday_candles` rows through the **real** decision code (not a re-implementation) via a `SimClock` that monkey-patches `now_ist`/`today_ist` in every engine module, a `ReplayClient` standing in for `UpstoxClient`, and a `FillModel` for simulated fills. Produces P&L stats, an "entry funnel" (which gate rejected how many candidates, in the real gate order), an EV-gate decomposition, and a rejection census. Has `--audit` (data-coverage check), `--test` (synthetic self-test of the harness itself, not the strategy), and normal replay mode. **This file documents, better than anything else in the repo, what the intended wiring and gate order actually are** — read its header and `STAGE_ORDER` comment before changing gate logic anywhere. |
| `main.py`                                | A (broken imports)  | Intended live entry point: builds all engines, runs the 09:15–15:30 5-minute cycle loop, session-level hard-exit sweeps (13:30 Tuesday / 14:45 general) as defense-in-depth on top of each position's own exit checks, daily-loss halt (realized+unrealized), EOD summary generation + persistence, graceful shutdown (SIGINT/SIGTERM closes positions, saves state).**Cannot currently import** — see §1.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `eod_report.py`                          | independent utility | Standalone script,`python eod_report.py --date YYYY-MM-DD`. Opens the DB read-only via raw `sqlite3`, tolerates missing tables/columns, and renders a very large Markdown forensic report (`reports/eod_report_<date>.md`) plus a raw JSON export per table (`reports/eod_report_<date>_raw/*.json`) — anomaly detection, VRP/ADX/PCR/skew profiles, regime timeline, gate-blockage analysis, per-trade deep dive with legs/exit/IV-crush, slippage, cost-by-strategy, equity curve, calibration drift, a unified master timeline merging cycles/decisions/entries/exits/log lines, and an explicit "LLM Analysis Context" section with prompted questions. Good reference for **every field name that exists across the schema** and how the old and new schemas overlay (checks for both `vrp`/`adx` and `vrp_smoothed`/`adx_15` style columns).         |
| `verify_all.py`, `split_db_per_day.py` | unknown             | **Not fetched** — the links provided for these both actually resolved to `backtest_engine.py`'s content (a link/href mismatch in the request). Fetch these directly from the repo before relying on any description of them.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |

---

## 4. Database (SQLite, `core.py::SCHEMA_SQL` is the source of truth)

Single SQLite file (default `data/nifty_algo_v3.db`), WAL mode, thread-safe wrapper (`core.Database`: `.query`, `.query_one`, `.insert`, `.update`, `.upsert`, `.execute`, `.ensure_column`). Migrations are additive `ALTER TABLE ... ADD COLUMN` statements applied idempotently at startup (`MIGRATION_SQL` list) — **when adding a new column anywhere, add both the `CREATE TABLE` field and a `MIGRATION_SQL` entry**, or existing DBs won't pick it up.

Key tables:

- **`session_state`** (one row per trading day) — the day's mutable state: opening range, VIX regime, entry window, size multiplier, capital, daily P&L, halt flags, consecutive-stops counter, gap info, straddle-at-open, etc. This is the in-memory `self.state` dict in `MarketDataEngine`, persisted every write via `_save_session_state()`.
- **`positions`** / **`position_legs`** — one row per position (condor/spread/etc.) and one row per leg (strike/type/action/qty/entry-exit price/greeks).
- **`trade_entries`** / **`trade_exits`** — richer, append-only history of entries and exits (vs. `positions` which is mutated in place), used for reporting/calibration queries.
- **`cycle_log`** — one row every ~5-minute cycle: spot, vix, vrp_raw/smoothed, ADX, EMA structure, PCR, skew, OR condition, regime fields, action taken, no-trade reason, running daily P&L. This is the primary timeline for debugging "why didn't it trade" or "why did it exit."
- **`option_chain_snapshot`** — full chain snapshot every cycle (strike × call/put × bid/ask/ltp/oi/iv/greeks). **This is what `backtest_engine.py` replays** — without enough days of this table populated (from live/paper runs), the backtester has nothing to replay (`--audit` tells you what's usable).
- **`daily_summary`** — one row per trading day, EOD aggregate (win rate, P&L, drawdown, VIX/NIFTY OHLC, strategies used, no-trade-reason histogram).
- **`strategy_decisions`** — every `StrategyEngine.decide()` call, ENTER or NO_TRADE with full reason string and params/signals JSON blobs.
- **`regime_decisions`** — every `RegimeEngine` classification (only populated once `regime_engine.py`'s import issue is fixed and it's actually wired into the run loop).
- **`calibration_state`** — append-only history of calibration runs (both `calibration_engine.py`'s and `regime_engine.py`'s embedded calibrator write here, to overlapping but not identical column sets — see §1).
- **`vix_history`**, **`market_snapshots`**, **`intraday_candles`**, **`api_call_log`**, **`audit_log`**, **`phantom_trades`** (NEUTRAL-blocked trades, simulated post-hoc to see if they'd have won), **`exit_quality_log`**, **`regime_accuracy_scores`**, **`expiry_results`**.

---

## 5. Config system (`core.py::Config` / `load_config()`)

**As of v4.1, `env.txt` holds only `UPSTOX_ACCESS_TOKEN=`.** Every other tunable (windows, costs, thresholds, exits, calibration, sizing — ~120 fields) has a hardcoded default in `load_config()` that reproduces the previously-shipped `env.txt` values. Any key still present in `env.txt` or the OS environment **overrides** its default (env values win). If you need to change a threshold, either add/edit the key in `env.txt` or change the default in `core.py::load_config()` — check both places when debugging "why is this threshold X."

Notable defaults worth knowing: `paper_trade_mode` defaults `True` and is **force-reset to `True`** unless `LIVE_RATES_VERIFIED=true` is also set (defense in depth against accidentally going live). `max_risk_per_trade_pct` is clamped below `max_daily_loss_pct / 3`. Lot size defaults to 65 (verify against the live NSE contract spec — the code prints a manual reminder, it does not auto-verify).

A large fraction of `Config` fields (the `v3.1`/`v3.2`/`v3.3` blocks — `stop_mult_dte0`, `target_pct_dte0`, `price_stop_wing_frac`, `credit_risk_ratio_dte0_early/mid/late`, `iv_sigma_cap_ratio`, `ev_blend_*_w`, `min_lots_fraction`, etc.) carry long inline comments explaining a specific dated incident that motivated the value (usually referencing a `2026-09-08` VIX-11 0DTE replay). **Read the comment above a constant before changing it** — most encode a lesson from a real measured failure mode, not an arbitrary tuning choice.

---

## 6. Core domain vocabulary

- **DTE** — trading-day count to next weekly expiry (Tuesday, `ExpiryCalendar`). DTE 0 = expiry day itself.
- **VRP (variance/volatility risk premium)** — `ATM IV% − Parkinson RV%`, in percentage points. Positive = options rich vs. realized movement = sell-premium edge. Smoothed via an exponentially-weighted buffer (`vrp_smoothing_cycles`, default 5) to reduce noise; anomaly-bounded against `vrp_anomaly_limit()` (DTE-aware: looser on 0DTE because low realized/implied ratio is *normal* near expiry) so one bad data point can't poison the smoothed series.
- **OR (opening range)** — 09:15–09:45 high/low. Classified `VERY_NARROW`→`VERY_WIDE` by the *more conservative* of (a) width as % of spot and (b) width vs. the opening ATM straddle (i.e., vs. the market's own priced expectation for the day).
- **Day-move-used** — realized intraday range vs. the range implied by the opening straddle (scaled by elapsed-time-fraction and a range/displacement conversion factor `day_move_range_factor≈1.93`, since a straddle prices expected |displacement| but the metric needs expected *range*). Used as a volatility-gate circuit breaker.
- **Regimes** (`regime_engine.py`): `VolatilityRegime` (STRONG_SELL_PREMIUM / SELL_PREMIUM / NEUTRAL / BUY_OPTIONS / HIGH_VOL_CAUTION / ABORT), `PriceRegime` (trend/range/choppy/observing), `PositioningRegime` (from OI walls, PCR, skew), combined by majority-vote into `FinalRegime` with a `ConfidenceLevel` (HIGH/MEDIUM/LOW/NONE from 3/2/1/0 signal agreements) and a day-of-week × confidence × event × VIX-level `size_multiplier`.
- **Day types** (`ExpiryCalendar.get_day_type`): `EXPIRY_DAY` (DTE0), `PRE_EXPIRY` (DTE1, and Monday), `NEW_CYCLE` (Wednesday), `WEEKEND_RISK` (Friday), `MID_WEEK`, `NON_TRADING`.
- **Exit priority ladder** (`ExecutionEngine.monitor_position`, evaluated in this order): premium stop (with time-based tightening schedule + profit-lock trailing) → price-based spot-move stop → profit target → VWAP breach (full close for directional spreads, one-side close for condor/butterfly) → ADX trend breach → portfolio delta breach → hard exit time → short-leg delta breach.
- **Paper vs. live** — `PaperOrderExecutor` simulates fills at bid(sell)/ask(buy) from the live chain; `LiveOrderExecutor` places real Upstox orders and refuses to construct under `paper_trade_mode=True`. Multiple independent guards prevent accidental live trading (see §5).

---

## 7. Where to look for a given task

- **"Why didn't the engine take a trade on date X"** → `cycle_log.no_trade_reason` / `strategy_decisions` table for that date, or run `eod_report.py --date X` and read §17 "Gate Blockage Analysis" / §19. If you have chain-snapshot history for that date, `backtest_engine.py --from X --to X --verbose` gives the real gate-by-gate funnel.
- **"Change a threshold"** → find the `Config` field in `core.py::load_config()` (check the inline comment for why it's set that way), or the calibrated equivalent in `calibration_engine.py::NIFTY_2026_DEFAULTS`/`CalibrationState` if it's meant to auto-tune from data.
- **"Add a new strategy type"** → `strategy_engine.py`: add to `DTE_REQUIREMENTS`/`MIN_CREDITS`/`PRICE_STOPS`, extend `_select_strategy`, `_validate_strategy_entry_rules`, `_build_legs_spec`, and the sizing/cost math in `compute_strategy_params`. Mirror any new exit behavior in `execution_engine.py::monitor_position`.
- **"Add a new market signal / indicator"** → `data_engine.py`, either in `TechnicalEngine` (stateless, e.g. new indicator) or as a new `MarketDataEngine` method wired into `run_cycle()`'s signals dict, then persisted in `_persist_cycle_log`/`_persist_market_snapshot` and added to the `cycle_log`/`market_snapshots` schema in `core.py`.
- **"Change regime classification logic"** → `regime_engine.py::RegimeClassifier` (`classify_volatility`, `classify_positioning`, `classify_final`) — but first fix its imports (§1) if you need it to actually run against current data.
- **"Fix/extend the nightly report"** → `eod_report.py` — it's independent of the engine's Python API (reads DB directly), so it's low-risk to edit. Add a new `compute_*` function and wire it into `generate_report()`'s `md.append(...)` sequence and the `raw_exports` dict.
- **"Make the live engine actually runnable"** → this is the biggest structural task in the repo; see §1. In order: (1) decide whether `strategy_engine.py`/`execution_engine.py`/`regime_engine.py` should be rewritten to match the `core`/`data_engine`/`calibration_engine` API (à la `backtest_engine.py`'s usage), or whether missing `nifty_algo_core.py`/`market_data_engine.py` shim modules should be reconstructed instead; (2) once one `StrategyEngine`/`ExecutionEngine` API is settled, update `main.py`'s imports and engine construction to match; (3) verify against `backtest_engine.py --test` (synthetic self-test) and then `--audit`/real replay.
- **"Validate a change didn't break anything"** → `backtest_engine.py --test` runs a synthetic-data self-test of the whole plumbing (clock injection, replay client, signal construction, decision path, exit ladder, fills, cost accounting) independent of any real recorded data — good smoke test after touching `data_engine.py`/`strategy_engine.py`/`execution_engine.py`/`calibration_engine.py`, once their imports are consistent with what it expects.

---

## 8. Conventions to preserve

- All timestamps are IST (`core.now_ist()`/`today_ist()`, via `zoneinfo`/`pytz` fallback). Never use naive UTC `datetime.now()`.
- Money in Rupees (`Rs`), option premium in index points (`pts`), lot size default 65 shares (`config.lot_size`).
- Every `Database` write should go through `.insert`/`.update`/`.upsert`/`.ensure_column` — don't hand-roll SQL against the connection directly except in read-only reporting scripts.
- Functions that touch external state (API calls, DB) are defensive: broad `try/except`, log-and-continue rather than crash the 5-minute loop. Preserve this pattern in new code — a single bad cycle should never kill the day's session.
- Inline comments prefixed `v3.1`/`v3.2`/`v3.3`/etc. document *why* a number is what it is, usually tied to a specific dated production incident. Treat these as regression-test documentation, not clutter — don't delete them when refactoring nearby code.
