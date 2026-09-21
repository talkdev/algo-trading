
# NIFTY Intraday Algorithmic Options Trading Engine & Backtest Framework

## Architecture Overview & Technical Reference Manual

---

### Table of Contents

1. [Executive Summary &amp; Core Design Philosophy](#1-executive-summary--core-design-philosophy)
2. [Module Architecture &amp; Import Dependency Graph](#2-module-architecture--import-dependency-graph)
3. [Engine Initialization, Data Structures &amp; State Mechanics](#3-engine-initialization-data-structures--state-mechanics)
4. [Master Pipeline Lifecycle: `decide()`](#4-master-pipeline-lifecycle-decide)
5. [Hard Gate Validation Engine (`_check_hard_gates`)](#5-hard-gate-validation-engine-_check_hard_gates)
6. [Regime Classification &amp; Regime-to-Strategy Mapping](#6-regime-classification--regime-to-strategy-mapping)
7. [Range Strategy Resolution &amp; Two-Way Extreme Fading Logic](#7-range-strategy-resolution--two-way-extreme-fading-logic)
8. [Strategy Entry Rule Validation (`_validate_entry_rules`)](#8-strategy-entry-rule-validation-_validate_entry_rules)
9. [Mathematical Parameter Computation &amp; Leg Construction (`compute_params`)](#9-mathematical-parameter-computation--leg-construction-compute_params)
10. [Cost, Slippage, and Margin Architecture](#10-cost-slippage-and-margin-architecture)
11. [Rigorous Expected Value (EV) Gate Model (`_compute_ev_gate`)](#11-rigorous-expected-value-ev-gate-model-_compute_ev_gate)
12. [DTE Mechanics &amp; Continuous Sqrt-Life Interpolation (`by_dte`)](#12-dte-mechanics--continuous-sqrt-life-interpolation-by_dte)
13. [Long-Premium Momentum Alternative Route (`_momentum_decision` &amp; `compute_momentum_params`)](#13-long-premium-momentum-alternative-route-_momentum_decision--compute_momentum_params)
14. [Dominant Structures, Position Sizing, and Risk Allocation](#14-dominant-structures-position-sizing-and-risk-allocation)
15. [Event-Driven Backtest Engine CLI &amp; Replay Architecture](#15-event-driven-backtest-engine-cli--replay-architecture)
16. [Comprehensive Decision &amp; Rejection Census Dictionary](#16-comprehensive-decision--rejection-census-dictionary)

---

### 1. Executive Summary & Core Design Philosophy

The system is an institutional-grade, cost-aware, intraday options trading and backtesting engine engineered specifically for NIFTY 50 index weekly and near-dated options traded on the National Stock Exchange of India (NSE).

#### Core Tenets:

- **Cost-Aware Structural Edge**: Every candidate option structure must clear realistic round-trip institutional friction (exchange STT, SEBI turnover fees, stamp duty, GST, broker order fees, and bid-ask slippage). Edge is never assumed; expected edge must exceed realistic round-trip costs by explicit mathematical hurdles.
- **Strict Intraday Demarcation**: All positions open after the opening stabilization period and are aggressively flattened before 15:15 IST (or configured `hard_exit_time`). Zero overnight gap or weekend jump risk is assumed.
- **Asymmetric Regime Hierarchy**: The primary engine book is a premium seller (Iron Condor, Iron Butterfly, Bull Put Spread, Bear Call Spread). Long-premium momentum alternatives (Long Call, Long Put) operate strictly as secondary directional substitutes when sell-side structures are rejected due to strong trend displacement or expanding IV.
- **Two-Way Auction & Mean-Reversion Preservation**: Identifies intraday auction expansions, rejecting delta-neutral pin condors in expanding sessions and transitioning into high/low extreme-fade directional credit verticals.

---

### 2. Module Architecture & Import Dependency Graph

The trading suite is decoupled into specialized components that interact through validated dictionaries, immutable state snapshots, and standardized database schemas.

```
                  ┌───────────────────────────────┐
                  │           main.py             │
                  │      (Production Driver)      │
                  └──────────────┬────────────────┘
                                 │
                   ┌─────────────┴──────────────┐
                   │                            │
                   ▼                            ▼
        ┌───────────────────────┐    ┌───────────────────────┐
        │   bot_controller.py   │    │  backtest_engine.py   │
        │  (Live Market Loop)   │    │ (Historical Replay CLI│
        └──────────┬────────────┘    └──────────┬────────────┘
                   │                            │
                   ├────────────────────────────┤
                   ▼                            ▼
        ┌───────────────────────────────────────────────┐
        │              strategy_engine.py               │
        │   - StrategyEngine (Hard Gates, decide(),     │
        │     Param Computation, EV Gate, Momentum)     │
        └───────┬───────────────────────────────┬───────┘
                │                               │
                ▼                               ▼
    ┌───────────────────────┐       ┌───────────────────────┐
    │   regime_engine.py    │       │    data_engine.py     │
    │ - RegimeEngine        │       │ - MarketDataEngine    │
    │ - RegimeClassifier    │       │ - Upstox Data Poller  │
    │ - CalibrationEngine   │       │ - Live Bar Aggregator │
    └───────────┬───────────┘       └───────────┬───────────┘
                │                               │
                ▼                               ▼
    ┌───────────────────────────────────────────────────┐
    │                      core.py                      │
    │ - Config, Database, ExpiryCalendar                │
    │ - UpstoxClient, RateLimiter, AlertNotifier        │
    │ - IST Clock Helpers (now_ist, today_ist, by_dte)  │
    └───────────────────────────────────────────────────┘
```

#### Key Module Roles & Imports:

1. **`core.py`**:
   - `Config`: Central dataclass with 150+ operational parameters, thresholds, risk budgets, and DTE-dependent curves.
   - `Database`: SQLite persistence layer enforcing write-ahead logging (WAL), table creation, execution audit, and cycle logs.
   - `ExpiryCalendar`: Derives true NSE weekly Tuesday/Thursday expiries, bank holidays, and calculates integer DTE (`actual_dte`).
   - `RateLimiter` & `UpstoxClient`: Institutional API interaction with exponential backoff and payload validation.
   - `now_ist()`, `today_ist()`, `by_dte()`: Timezone-aware date/time functions locked strictly to Indian Standard Time (Asia/Kolkata, UTC+05:30).
2. **`data_engine.py`**:
   - `MarketDataEngine`: Ingests ticks, generates 1-minute and 15-minute OHLCV candles, computes intraday indicators (EMA 9/21, VWAP, Supertrend, ADX 14/15, Parkinson Realized Volatility), processes option chains, computes synthetic strike Greeks (Black-76 / Black-Scholes), strike bid/ask spreads, and tracks ATM straddle prices.
3. **`regime_engine.py`**:
   - `RegimeClassifier` & `RegimeEngine`: Four-tier hierarchical classification:
     - Volatility Regime (`classify_volatility`): `SELL_PREMIUM`, `STRONG_SELL_PREMIUM`, `BUY_VOLATILITY`, `NEUTRAL`, `CAUTION`.
     - Price Regime (`classify_price`): `UPTREND`, `STRONG_UPTREND`, `DOWNTREND`, `STRONG_DOWNTREND`, `RANGE_BOUND`, `BREAKOUT_EXPANSION`, `OBSERVING`.
     - Positioning Regime (`classify_positioning`): `BULL_BIAS`, `BEAR_BIAS`, `PINNED`, `UNCLEAR`.
     - Final Regime (`classify_final`): Synthesis yielding `PREMIUM_SELL_RANGE`, `PREMIUM_SELL_BULL`, `PREMIUM_SELL_BEAR`, `MOMENTUM_BUY_CALL`, `MOMENTUM_BUY_PUT`, or `NO_TRADE`.
   - `CalibrationEngine`: Runs rolling 20-session Bayesian shrinkage calibration against historical distribution metrics (VIX, VRP, OI, PCR, Skew, Day Ranges).
4. **`strategy_engine.py`**:
   - Consumes `signals` dict produced by `RegimeEngine` and `MarketDataEngine`. Executes hard gates, assigns strategy structures, solves wing-fit delta strikes, prices slippage and institutional charges, evaluates the barrier EV model, handles the momentum alternative fallback, and outputs executable order specifications.
5. **`execution_engine.py`**:
   - Manages state machines for active orders, parent-child leg orders, bracket orders, trail tracking, delta breach monitors, proximity alerts, and emergency unwinds.
6. **`backtest_engine.py`**:
   - High-fidelity historical tick/1-minute replay simulator with synthetic fill models, bid/ask spread consumption, margin-aware execution, and portfolio performance metrics.

---

### 3. Engine Initialization, Data Structures & State Mechanics

#### `StrategyEngine.__init__` Lifecycle:

```python
class StrategyEngine:
    def __init__(self, config: Config, database: Database,
                 market_engine: MarketDataEngine, logger):
        self.config = config
        self.db = database
        self.market_engine = market_engine
        self.logger = logger
        self.cal_engine = CalibrationEngine(database, config, logger)
        self.calendar = ExpiryCalendar(
            holidays_file=getattr(config, "holidays_file", "nse_holidays.json")
        )
        self._ensure_tables()
        self._session_prices: List[Tuple[dtime, float]] = []
        self._construct_fail_counts: Dict[str, int] = {}
```

#### Persistent State Store (`market_engine.state`):

The engine relies on an in-memory state dictionary synchronized continuously with SQLite:

- `entry_count`: Number of completed structure entries today.
- `consecutive_stops`: Consecutive stop-loss breaches recorded in current session (triggers halt if ≥ 2).
- `last_stop_time`, `last_stop_reason`, `last_stop_combo`: Fingerprints of the last stopped position.
- `last_exit_time`, `last_exit_spot`, `last_exit_strategy`: Exact exit timestamps and spot prices to enforce time cooldowns (`ENTRY_COOLDOWN_MIN = 10`) and distance hurdles.
- `session_high`, `session_low`: Dynamic intraday extreme prices.
- `session_mean_reversion_book`: Boolean flag set after a completed high/low fade scalp.
- `construct_fail`: Latch tracking repeated economic/structural leg failures on static auction prints (`_sticky_construct_reason`).
- `displaced_tape`: Trend displacement latch (`_tape_displacement`) preventing single-cycle regime flip whipsaws.

#### Database Persistence Schema:

Table `strategy_decisions` records every evaluation cycle:

- `timestamp`: ISO-8601 IST string.
- `strategy_name`: Assigned strategy or `NONE` / `NO_TRADE`.
- `action`: `ENTER`, `NO_TRADE`, `HOLD`, `EXIT`.
- `reason`: Machine-readable rejection token or acceptance tag.
- `regime`, `confidence`, `spot`, `vix`: Market context snapshot.
- `dte`: Days to expiry at evaluation moment.
- `params_json`: Complete JSON dump of legs, credit, strikes, stops, targets, margin, and EV breakdown.

---

### 4. Master Pipeline Lifecycle: `decide()`

The `decide(signals: dict) -> dict` method is the top-level entry point called every cycle (typically every 1 to 3 minutes).

```
                            [ signals: dict ]
                                    │
                                    ▼
                         _note_price(signals)
                                    │
                                    ▼
                         _check_hard_gates()
                                    │
                    ┌───────────────┴───────────────┐
                 [Fails]                         [Passes]
                    │                               │
                    ▼                               ▼
       _momentum_decision(reason)        _map_regime_to_strategy()
          ┌─────────┴─────────┐                     │
      [Allowed]           [Blocked]                 ▼
          │                   │          _counter_trend_entry_refusal()
          ▼                   ▼                     │
    Return ENTER        Return NO_TRADE             ▼
     (Long Px)          (Persist Gate)    _validate_entry_rules()
                                                    │
                                                    ▼
                                          _sticky_construct_reason()
                                                    │
                                                    ▼
                                          compute_params()
                                                    │
                                    ┌───────────────┴───────────────┐
                                 [Fails]                         [Passes]
                                    │                               │
                                    ▼                               ▼
                      Condor Demotion to Vertical?            Return ENTER
                                    │                         (Credit Structure)
                      _momentum_decision(fail)
                                    │
                             Return Decision
```

#### Execution Steps:

1. **Price Memory Registration (`_note_price`)**: Appends current `(current_time, spot)` to `self._session_prices` for closing-hour velocity and displacement calculations.
2. **Hard Gate Sweep (`_check_hard_gates`)**: Evaluates global session, time, volatility, and risk gates. If tripped:
   - Queries `_momentum_decision(signals, gate_reason)`. If momentum passes, returns long-premium `ENTER`.
   - Otherwise appends momentum rejection reason via `_with_momentum_refuse`, persists to database, and returns `NO_TRADE`.
3. **Regime to Strategy Mapping (`_map_regime_to_strategy`)**: Maps `final_regime` to target strategy structure.
4. **Counter-Trend Entry Symmetry (`_counter_trend_entry_refusal`)**: Verifies that a credit spread is not entering against an intraday directional displacement.
5. **Strategy-Specific Entry Rules (`_validate_entry_rules`)**: Validates delta constraints, pin vetoes, and opening range boundaries.
6. **Sticky Construct Check (`_sticky_construct_reason`)**: Prevents continuous order building when the auction is stationary and previously failed economics.
7. **Parameter Computation & EV Gating (`compute_params`)**: Selects strikes, builds legs, calculates round-trip friction, applies EV barrier simulation, and checks risk budgets.
   - **Condor Demotion (`_demote_condor_on_econ_fail`)**: If an Iron Condor fails economics on wing geometry (`wing_cost` or `condor_weak_side`), checks for bearish lean or afternoon fade evidence. If present, immediately demotes to `BEAR_CALL_SPREAD` or `BULL_PUT_SPREAD`. If parameters pass, returns `ENTER`.
   - **Momentum Substitution**: If parameters are invalid, passes the structural failure token to `_momentum_decision`.
8. **Final Decision Dispatch**: Persists decision, updates state, and returns action payload:
   ```json
   {
     "action": "ENTER",
     "strategy_name": "BEAR_CALL_SPREAD",
     "reason": "regime:PREMIUM_SELL_BEAR:conf=HIGH:dte=1:or=NARROW:adx=24",
     "params": { ... }
   }
   ```

---

### 5. Hard Gate Validation Engine (`_check_hard_gates`)

The hard gate suite enforces absolute risk parameters. It evaluates sequentially:

| Order | Hard Gate Identifier                            | Trigger Condition                                                                        | Rationale & Protection                                                                                                          |
| ----- | ----------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 1     | `regime_engine_abort`                         | `signals.get("block_new_entries")` or `final_regime in ("NO_TRADE", "ABORT", None)`  | Upstream regime classifier detected corrupted data, news halt, or unclassifiable tape.                                          |
| 2     | `daily_loss_limit_reached_or_halted`          | `state.get("daily_halted") == True`                                                    | Session P&L breached maximum cumulative capital loss limit (`daily_loss_limit_pct`).                                          |
| 3     | `circuit_breaker_suspected`                   | `signals.get("circuit_breaker_suspected") == True`                                     | Spot index halted or tick dispersion indicates exchange-wide circuit filter.                                                    |
| 4     | `vix_spike_detected`                          | `signals.get("vix_spike_detected") == True`                                            | India VIX jumped ≥ 15% intraday; short premium tail-risk explodes.                                                             |
| 5     | `iv_expanding_never_sell_into_rising_iv`      | `iv_behavior in ("EXPANDING", "SPIKING")` (unless extreme fade)                        | Vega expansion destroys short option credit before theta can decay.                                                             |
| 6     | `before_entry_window` / `past_entry_window` | `current_time < trading_window_start (09:20)` or `current_time > last_entry (14:15)` | Avoids opening print auction noise and avoids late entries with insufficient theta runway.                                      |
| 7     | `max_concurrent_positions_reached`            | `open_count >= config.max_concurrent_positions (1)`                                    | Single-position constraint preventing over-leverage and operational complexity.                                                 |
| 8     | `max_entries_per_day_reached`                 | `today_entries >= config.max_entries_per_day (default 2, 3 for 2-way fade)`            | Over-trading protection on choppy, non-trending sessions.                                                                       |
| 9     | `entry_cooldown_remaining`                    | Time since last action < cooldown (`ENTRY_COOLDOWN_MIN = 10`)                          | Prevents immediate revenge entries after an exit.                                                                               |
| 10    | `no_material_change_since_exit`               |                                                                                          | spot - last_exit_spot                                                                                                           |
| 11    | `2_consecutive_stops_halt`                    | `state.get("consecutive_stops") >= 2`                                                  | Kills the engine for the rest of the day after two failed stops.                                                                |
| 12    | `same_signal_combo_caused_last_stop`          | Current regime/signal tuple matches last stop signature                                  | Prevents repeating identical failed theses.                                                                                     |
| 13    | `stop_cooldown_remaining`                     | Time since last stop < STOP_COOLDOWN_MAP (30 to 45 min)                                  | Enforces emotional/tape reset period after a stop-out (`CLOSE_STOP: 30m, CLOSE_ADX: 45m, CLOSE_VWAP: 20m, CLOSE_DELTA: 30m`). |
| 14    | `spot_velocity_too_fast`                      | 5-minute spot displacement > threshold (e.g. 50 pts)                                     | Prevents selling credit in front of an aggressive price locomotive.                                                             |
| 15    | `straddle_expanding`                          | ATM straddle price expanding > 5% over rolling 15 min                                    | Direct proxy for implied volatility repricing against the short seller.                                                         |
| 16    | `opening_range_pending`                       | `or_computed == False` or `price_regime == "OBSERVING"`                              | Opening 15-minute range must resolve before structural bounds exist.                                                            |
| 17    | `open_spike_wait`                             | `day_high_is_open_spike == True` and `current_time < 12:15`                          | Opening wick creates false range tops; must wait for afternoon confirmation.                                                    |
| 18    | `chain_stale`                                 | Options snapshot timestamp > 120 seconds old                                             | Protects against quote staleness, broken feeds, and phantom fills.                                                              |
| 19    | `expiry_day_waiting_for_0dte`                 | Tuesday session where 0DTE chain is not yet loaded                                       | Prevents accidental execution on weekly contracts on expiry morning.                                                            |
| 20    | `confidence_insufficient`                     | `confidence_level in ("LOW", "NONE")` (unless extreme fade)                            | Cost friction requires minimum statistical edge.                                                                                |
| 21    | `dte_above_max` / `dte_requires_confidence` | DTE > max_dte (4) or DTE ≥ 4 with confidence ≠ HIGH                                    | Protects against zero-theta far-dated contracts.                                                                                |
| 22    | `day_move_used_no_edge`                       | Range move consumed ≥ 75% of opening straddle                                           | Expected remaining intraday price movement is exhausted.                                                                        |
| 23    | `insufficient_minutes_before_exit`            | Mins to hard exit < required (50 to 90 min)                                              | Must allow adequate time for theta decay to outpace friction.                                                                   |
| 24    | `wide_or_dangerous`                           | `or_condition in ("WIDE", "VERY_WIDE")` without confirmed trend                        | Volatile open creates high failure rate for range structures.                                                                   |

---

### 6. Regime Classification & Regime-to-Strategy Mapping

The mapping from multi-dimensional market signals into candidate option strategies is performed by `_map_regime_to_strategy`:

```
               [ final_regime (from RegimeEngine) ]
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     ▼                           ▼                           ▼
PREMIUM_SELL_RANGE       PREMIUM_SELL_BULL           PREMIUM_SELL_BEAR
     │                           │                           │
     ▼                           ▼                           ▼
_resolve_range_strategy()   BULL_PUT_SPREAD             BEAR_CALL_SPREAD
     │
     ├─ DTE == 0, narrow, flat ADX, spot near ATM ──► IRON_BUTTERFLY
     ├─ Bearish Day Structure / Lean ───────────────► BEAR_CALL_SPREAD
     ├─ Two-Way Auction at Range High (loc ≥ 0.85) ──► BEAR_CALL_SPREAD
     ├─ Two-Way Auction at Range Low (loc ≤ 0.15) ───► BULL_PUT_SPREAD
     └─ Stationary / Normal Range ──────────────────► IRON_CONDOR
```

#### Directional Trend Overrides:

- **`PREMIUM_SELL_BULL`**: Spot is in confirmed uptrend (above VWAP, Supertrend Bullish, EMA 9 > 21). Maps to **`BULL_PUT_SPREAD`** (credit put spread sold below support).
- **`PREMIUM_SELL_BEAR`**: Spot is in confirmed downtrend (below VWAP, Supertrend Bearish, EMA 9 < 21). Maps to **`BEAR_CALL_SPREAD`** (credit call spread sold above resistance).
- **Two-Way Auction Positioning Override (`PATCH_V31`)**: If `signals["two_way_auction"]` is true and session range ≥ 85 points:
  - If range location ≥ 0.80: Overrides `final_regime` to `PREMIUM_SELL_BEAR` (High Fade).
  - If range location ≤ 0.20: Overrides `final_regime` to `PREMIUM_SELL_BULL` (Low Fade).

---

### 7. Range Strategy Resolution & Two-Way Extreme Fading Logic

Range-bound markets are resolved via `_resolve_range_strategy`:

#### 1. Bearish Structure Lean Detection (`_range_day_bearish_lean`):

Even within an overall range, intraday structure frequently leans asymmetric. A Bear Call Spread is selected instead of an Iron Condor if:

- `lower_high_confirmed` is true and spot is below VWAP.
- Failed breakout above the opening range occurred (`failed_break_above`).
- PCR < 0.80 with call OI additions heavily exceeding put OI additions.

#### 2. Two-Way Auction Extreme Resolution:

On volatile range sessions where the index swings between extremes without a persistent trend:

- Selling an Iron Condor in the middle of the range guarantees testing one or both wings.
- The engine calculates session range position via `_fade_range_pos(signals)`:
  ```text

  ```

loc = (spot - eff_lo) / (eff_hi - eff_lo)

```
  (Where `eff_hi` and `eff_lo` remove open-spike wicks when the spike gap ≥ 15 pts).
- If session_range ≥ 85 pts and DTE ≥ 1:
  - If loc ≥ 0.85: Selects **`BEAR_CALL_SPREAD`** (`afternoon_high_fade = True`).
  - If loc ≤ 0.15: Selects **`BULL_PUT_SPREAD`** (`afternoon_low_fade = True`).
  - If 0.15 < loc < 0.85: Returns **`NO_TRADE`** (`two_way_auction_wait_for_extreme`).

#### 3. Iron Butterfly Resolution (0DTE Pin Spec):
An Iron Butterfly (selling ATM Put + ATM Call, buying wings) is strictly restricted:
- Requires DTE in (0, 1) (typically 0DTE).
- `or_condition` must be `VERY_NARROW` or `NARROW`.
- ADX_15 < 20 (flat volatility, absent momentum).
- `current_time` < 12:00 IST (pre-midday entry).
- |spot - atm_strike| < 50 pts (spot closely anchored to strike).

---

### 8. Strategy Entry Rule Validation (`_validate_entry_rules`)

Before strike construction, `_validate_entry_rules` runs structural checks tailored to the chosen strategy:

#### 1. Iron Butterfly Rules:
- Must have DTE in (0, 1).
- On 0DTE, entry after 12:00 IST is forbidden.
- Spot must be within 50 points of ATM strike.
- ADX_15 must be ≤ 22.
- Opening range condition cannot be `WIDE` or `VERY_WIDE`.

#### 2. Iron Condor Rules:
- **Runway Gate**: Minutes to hard exit must be ≥ 75 min (on 0DTE) or ≥ 90 min (on DTE ≥ 1).
- **Momentum Gate**: ADX_15 must be below `adx_strong_threshold` (default 25) and ADX_15 must be mature.
- **Two-Way Veto**: Banned on confirmed two-way expanding auctions.
- **Opening Wick Veto**: If day high was set on the 09:15-09:30 open spike, weekly condor is blocked (high probability of afternoon re-test).
- **Opening Range Mid-Positioning**: On 0DTE, condors cannot be entered if spot is too close to opening range boundaries. Spot must be centered:
  ```text
OR_low + 30 < spot < OR_high - 30
```

#### 3. Bull Put Spread Rules:

- **Location Check**: Spot cannot be deeply below Opening Range Midpoint (spot ≥ OR_mid - 30 on 0DTE, -15 on DTE ≥ 1).
- **VWAP Support**: Spot cannot be more than 30 points below VWAP (unless taking a thesis-driven failed-break reclaim vertical).
- **Resistance Distance**: Spot cannot be within 20 points below a major session resistance ceiling.

#### 4. Bear Call Spread Rules:

- **Location Check**: Spot cannot be deeply above Opening Range Midpoint (spot ≤ OR_mid + 30 on 0DTE, +15 on DTE ≥ 1).
- **VWAP Resistance**: Spot cannot be more than 30 points above VWAP.
- **Support Distance**: Spot cannot be within 20 points above a major session support floor.
- **Max Pain Magnet Veto**: If spot is within 25 points of Max Pain, selling a Bear Call spread is vetoed unless the market is in a confirmed strong downtrend.

---

### 9. Mathematical Parameter Computation & Leg Construction (`compute_params`)

`compute_params(strategy_name, selection_reason, signals, size_mult)` is the core quantitative builder:

```
                      Target Strategy & Signals
                                  │
                                  ▼
                     Delta-Primary Strike Solver
                     (_select_strikes & chain)
                                  │
                                  ▼
                       Leg Validation & Pricing
                    (_build_validated_legs & bid/ask)
                                  │
                                  ▼
                   Gross Credit & Net Credit Engine
                     (Deduct Costs & Slippage)
                                  │
                                  ▼
                    Structural Economic Sanity Checks
                     (Wing Ratios, Credit Ratios)
                                  │
                                  ▼
                      Target & Stop Loss Sizing
                     (DTE-Dependent Dynamic Multipliers)
                                  │
                                  ▼
                   Expected Value (EV) Barrier Engine
                         (_compute_ev_gate)
                                  │
                                  ▼
                    Capital Allocation & Lot Sizing
                    (Risk Budget, Margin, Lot Caps)
                                  │
                                  ▼
                       Valid Parameter Package
```

#### 1. Delta-Primary Strike Selection Engine (`_select_strikes`):

The engine targets precise option deltas matched to DTE and volatility:

- **Target Delta Assignment**:
  - Weekly (DTE ≥ 2) Condors: Targets Δ ≈ 0.18 (if strong ADX) or Δ ≈ 0.20 (flat).
  - 0DTE / Near-dated: Targets Δ ≈ 0.22 - 0.25 (shaved down by 0.01-0.02 if VIX < 14).
  - Directional Verticals: Retains canonical Δ ≈ 0.28 - 0.32 on the favoured side.
- **Expected Move Bounds Clamping**:
  Short strikes are clamped within an Expected Move (EM) band:
  ```text

  ```

[band_lo, band_hi] = [em_band_lo × EM, em_band_hi × EM]

```
  (Weekly bounds: 0.55 × EM to 1.35 × EM for condors; up to 2.10 × EM for verticals).
- **Extreme-Fade Strike Cushioning**:
  - Failed-break verticals pin the short beyond the tested extreme plus cushion (≥ 80 pts).
  - High/Low fades pin the short strike at least 80 pts outside the confirmed extreme, bumping one strike step further if nearest rounding lands within the proximity stop band.
- **Adaptive Wing Width Fitting (`_fit_wing_width`)**:
  - On weekly condors (DTE ≥ 2), multi-day vega makes protective wings expensive. The engine widens the wing step-by-step from base width up to wing_max until long premium ≤ wing_cost_frac_max_weekly (50%).
  - Wing factor is interpolated via continuous sqrt-life:
    ```text
wing_factor = by_dte(DTE, 0.50, 0.62)
```

    ```text
wing_max = round≤ft((by_dte(DTE, 250.0, 450.0)) / (step)) × step

```
    (Interpolates from 250 pts on 0DTE to 450 pts on DTE ≥ 2).

#### 2. Pricing & Validation (`_build_validated_legs`):
For every leg:
- Validates bid > 0, ask > 0, and spread ≤ spread_max_pts.
- Execution Price Model:
  ```text
Exec Price_SELL = bid + fill_edge × (ask - bid)
```

```text
Exec Price_BUY = ask - fill_edge × (ask - bid)
```

  (Default `fill_edge` = 0.25, meaning conservative execution paying 25% away from the quote edge toward the mid).

#### 3. Dynamic Stop Loss & Profit Target Geometry:

The stop and target are mathematically tied to net credit received and DTE via `by_dte`:

- **Target Fraction (`_get_target_pct`)**:
  - Base target interpolated via `config.target_pct_for_dte(actual_dte)`:
    ```text

    ```

target_pct = by_dte(DTE, 0.70, 0.40)

```
    (Yields ≈ 0.70 on 0DTE, ≈ 0.52 on 1DTE, 0.40 on DTE ≥ 2, shaved by -0.03 for DTE ≥ 3, and shaved further by -0.035 to -0.07 on elevated VIX ≥ 12).
- **Stop Loss Multiplier (`stop_mult_for_dte`)**:
  - Base stop multiplier interpolated via `config.stop_mult_for_dte(actual_dte)`:
    ```text
stop_mult = by_dte(DTE, 1.60, 1.70)
```

    (Yields 1.60× on 0DTE, ≈ 1.66× on 1DTE, 1.70× on DTE ≥ 2, capped at structural wing loss).

---

### 10. Cost, Slippage, and Margin Architecture

Trading NIFTY options intraday is economically fatal if costs are ignored. The engine prices real-world exchange frictions at the per-order and statutory level:

#### 1. Statutory & Brokerage Costs (`_compute_costs`):

- **Brokerage**: Flat ₹20 per executed order leg.
- **STT (Securities Transaction Tax)**: 0.0625% on sell-side option turnover (on premium).
- **Exchange Turnover Charges**: 0.05% on premium turnover.
- **SEBI Turnover Fee**: ₹10 per crore turnover.
- **Stamp Duty**: 0.003% on buy-side turnover.
- **GST**: 18% levied on (Brokerage + Exchange Turnover Charges + SEBI Fees).

#### 2. Realistic Spread-Aware Slippage (`_compute_slippage`):

Slippage is not an abstract percentage; it depends on the actual bid-ask spreads of the selected strikes:

```text
Slippage_entry = sum(legs) (ask_i - bid_i) / (2) × entry_slippage_mult (0.35)
```

```text
Slippage_exit_stressed = sum(legs) (ask_i - bid_i) / (2) × exit_slippage_mult (2.25)
```

The stressed exit multiplier models liquidity evaporation during emergency stop triggers.

#### 3. Institutional Structural Economic Checks:

Before advancing to EV calculations, the structure must clear strict cost ratios:

1. **Net Credit Positive**: Net Credit = Gross Credit - Entry Costs - Entry Slippage > 0.
2. **Friction Ceiling**: Round-trip friction cannot exceed 35% of net credit.
3. **Brokerage Burden**: Brokerage cannot exceed 20% of net credit at target size.
4. **Wing Cost Dominance**: Long protective wing premium cannot exceed 50% (`wing_cost_frac_max`) of short premium sold (Iron Condors only).
5. **Credit-to-Wing Ratio**:
   ```text

   ```

Net Credit / Wing Width >=
    MIN_CREDIT_RATIO_DTE0[strat]  (for 0DTE: 0.13 IC, 0.18 Fly, 0.11 Verticals)
    MIN_CREDIT_RATIO[strat]       (for DTE >= 1: 0.10 IC, 0.15 Fly, 0.08 Verticals)

```
6. **Profit vs Friction**: Expected profit (Net Credit × Target %) must be at least 1.75× total round-trip friction.

---

### 11. Rigorous Expected Value (EV) Gate Model (`_compute_ev_gate`)

The EV engine implements a continuous-time barrier probability model blended with market deltas and empirical priors.

```

       ┌───────────────────────────┐   ┌───────────────────────────┐   ┌───────────────────────────┐
       │   Empirical OR Prior      │   │   Barrier / GBM Model     │   │   Market Quoted Delta     │
       │   (p_win_prior by DTE)    │   │   (p_win_model via sigma) │   │   (p_mkt = 1 - max|delta|)│
       └─────────────┬─────────────┘   └─────────────┬─────────────┘   └─────────────┬─────────────┘
                     │                               │                               │
                     │ (Weight = 0.30)               │ (Weight = 0.40)               │ (Weight = 0.30)
                     └───────────────────────┬───────┴───────────────────────────────┘
                                             │
                                             ▼
                                  Blended Win Probability
                                          (p_win)
                                             │
                                             ▼
                               Three-Outcome Expectancy Model
                               [ Win, Stop-Loss, Tail Jump ]
                                             │
                                             ▼
                                    Net Expectancy (EV)
                                             │
                         ┌───────────────────┴───────────────────┐
                         ▼                                       ▼
                   EV ≥ Threshold                          EV < Threshold
                      [ PASS ]                                [ REJECT ]

```

#### 1. Three-Outcome Expectancy Equation:
Intraday short option books do not have a binary payoff. They experience three distinct path resolutions:
1. **Target Win**: Position hits profit target cleanly (p_win_eff).
2. **Stop Loss**: Position triggers the premium/spot stop (p_stop).
3. **Tail Loss / Jump**: Position suffers a fast volatility jump or gap through the stop toward the wing (p_tail).

```text
EV = p_win_eff · (Reward - Friction_calm) - p_stop · (Stop Loss + Friction_stressed) - p_tail · (Tail Loss + Friction_stressed)
```

Where:

- Tail Loss = Stop Loss + 0.30 × \max(Wing Loss - Stop Loss, 0).
- p_tail = config.gamma_tail_prob_for_dte(DTE) = by_dte(DTE, 0.055, 0.020) (clamped ≤ 0.20, scaled 1.8× on wide OR and 1.5× on strong ADX).
- p_win_eff = p_win · (1 - p_tail).
- p_stop = 1 - p_win_eff - p_tail.

#### 2. Tri-Partite Win Probability (p_win) Formulation:

```text
p_win = w_m · p_win_model + w_p · p_win_prior + w_k · p_mkt
```

(Default weights: w_m = 0.40, w_p = 0.30, w_k = 0.30; on thesis reclaim verticals: w_m = 0.25, w_p = 0.20, w_k = 0.55).

- **Barrier Model (p_win_model)**:
  Uses the true short strike distance b = |strike - spot| - barrier_pull_pts.
  ```text

  ```

σ_{pts} = \min(σ_{iv}, σ_{straddle} × 1.15)

```
  ```text
z = (b) / (σ_{pts)}
```

```text
p_touch = sum_{b in barriers} 2 · Φ(-z)
```

- *Max Pain Magnet Adjustment*: If DTE == 0 and spot is within 0.5 × σ_{pts} of Max Pain, p_touch is discounted by 15% (p_touch ≤ftarrow p_touch × 0.85).

```text
p_win_model = clamp(1.0 - p_touch, 0.20, 0.93)
```

- **Market Delta Prior (p_mkt)**:
  ```text

  ```

p_mkt = 1.0 - \max(|Δ_{short_legs}|)

```
- **Empirical Table Prior (p_win_prior)**:
  Indexed by DTE and Opening Range Condition (`VERY_NARROW`, `NARROW`, `MODERATE`, `WIDE`, `VERY_WIDE`), adjusted by smoothed VRP bonus (+0.05 if VRP > 3.5 pp).

#### 3. EV Rejection Threshold:
```text
Min EV = \max(Net Credit × 0.03, Friction × 0.35)
```

If EV < Min EV, the parameter package is marked `valid: False` with `ev_gate:ev_X_below_min_Y`.

---

### 12. DTE Mechanics & Continuous Sqrt-Life Interpolation (`by_dte`)

In early versions of the engine, DTE logic was bifurcated into rigid step functions (`if dte == 0: ... else: ...`), which caused severe discontinuities on Monday (1DTE) and Friday (2DTE). Modern engine architecture solves this via **continuous square-root remaining-life interpolation** implemented in `core.by_dte`.

#### Sqrt-Life Interpolation Mathematical Formulation:

Option life is parameterized by its time variance sqrt(T). With market session life anchors defined as:

- Life_expiry = 0.5 sessions (sqrt(0.5) ≈ 0.7071)
- Life_weekly = 2.5 sessions (sqrt(2.5) ≈ 1.5811)

For any integer or floating DTE:

```text
life = DTE + 0.5
```

```text
w = \frac{sqrt(2.5) - sqrt(life)}{sqrt(2.5) - sqrt(0.5)} = \frac{1.5811 - sqrt(DTE + 0.5)}{0.8740},   w in [0.0, 1.0]
```

```text
by_dte(DTE, V_expiry, V_weekly) = V_weekly + w · (V_expiry - V_weekly)
```

Evaluating weights:

- **0DTE**: w = 1.0 → Exact expiry-day parameter.
- **1DTE**: w ≈ 0.41 → Smoothly interpolates 41% towards expiry anchor.
- **≥ 2DTE**: w = 0.0 → Exact weekly anchor.

#### Comprehensive DTE Interpolation Matrix:

| Operational Parameter / Behavior          | 0DTE (Expiry Tuesday)                            | 1DTE (Monday)                    | 2DTE (Friday)                        | 3DTE / 4DTE (Wed / Thu)                       | Function / Mechanism                      |
| ----------------------------------------- | ------------------------------------------------ | -------------------------------- | ------------------------------------ | --------------------------------------------- | ----------------------------------------- |
| **Interpolation Weight (w)**        | 1.00                                             | ≈ 0.41                          | 0.00                                 | 0.00                                          | `core.dte_blend(dte)`                   |
| **Tradeable Universe**              | Iron Butterfly, Iron Condor, Bull Put, Bear Call | Iron Condor, Bull Put, Bear Call | Iron Condor, Bull Put, Bear Call     | Iron Condor, Bull Put, Bear Call (Restricted) | `DTE_REQUIREMENTS`                      |
| **Max DTE Tradeable Gate**          | Permitted                                        | Permitted                        | Permitted                            | Permitted up to 4; rejected if > 4            | `_check_hard_gates`                     |
| **Confidence Level Requirement**    | LOW/MEDIUM/HIGH (or Extreme Fade)                | MEDIUM / HIGH                    | MEDIUM / HIGH                        | HIGH only (`confidence == "HIGH"`)          | `_check_hard_gates`                     |
| **Gamma Tail Probability (p_tail)** | 0.055 (5.5%)                                     | ≈ 0.035 (3.5%)                  | 0.020 (2.0%)                         | 0.020 (2.0%)                                  | `config.gamma_tail_prob_for_dte(dte)`   |
| **Credit-to-Wing Ratio Minimum**    | IC: 0.13, Fly: 0.18, Vert: 0.11                  | IC: 0.10, Fly: 0.15, Vert: 0.08  | IC: 0.10, Fly: 0.15, Vert: 0.08      | IC: 0.10, Fly: 0.15, Vert: 0.08               | `MIN_CREDIT_RATIO[_DTE0]`               |
| **Wing Cost Fraction Cap**          | 0.50 (`wing_cost_frac_max`)                    | 0.50                             | 0.50 (`wing_cost_frac_max_weekly`) | 0.50                                          | Condors only; bypassed on verticals       |
| **Target Net Credit \%**            | 0.70 (70%)                                       | ≈ 0.52 (52%)                    | 0.40 (40%)                           | 0.37 (37%, -0.03 shave)                       | `config.target_pct_for_dte(dte)`        |
| **Stop Loss Multiple**              | 1.60× net credit                                | ≈ 1.66× net credit             | 1.70× net credit                    | 1.70× net credit                             | `config.stop_mult_for_dte(dte)`         |
| **Delta Close Threshold**           | Δ ≥ 0.45                                       | ≈ 0.48                          | Δ ≥ 0.50                           | Δ ≥ 0.50                                    | `config.delta_close_for_dte(dte)`       |
| **EV Carry Discount**               | 1.00 (No theta carry credit)                     | ≈ 0.78                          | 0.65 (`ev_carry_discount_dte2p`)   | 0.65                                          | `config.ev_carry_discount_for_dte(dte)` |
| **Wing Factor & Max Wing Width**    | Factor: 0.50, Max: 250 pts                       | Factor: ≈ 0.57, Max: ≈ 370 pts | Factor: 0.62, Max: 450 pts           | Factor: 0.62, Max: 450 pts                    | Adaptive wing clamping via`by_dte`      |
| **Margin Add-on Buffer**            | +18% broker tightening                           | +10% buffer                      | +10% buffer                          | +10% buffer                                   | Fully hedged lot margin model             |
| **Opening Range Buffer**            | ± 30 pts from OR mid                            | ± 15 pts from OR mid            | ± 15 pts from OR mid                | ± 15 pts from OR mid                         | `_validate_entry_rules`                 |
| **Max Pain Pinning Bonus**          | Active in EV model (15% touch cut)               | Inactive (0%)                    | Inactive (0%)                        | Inactive (0%)                                 | 0DTE afternoon magnetism                  |
| **Momentum Substitute Option**      | **Strictly Forbidden** (0DTE gamma cliff)  | Permitted if sell-side blocked   | Permitted if sell-side blocked       | Permitted if sell-side blocked                | `_momentum_gate`                        |

---

### 13. Long-Premium Momentum Alternative Route (`_momentum_decision` & `compute_momentum_params`)

When market conditions invalidate short premium selling, the engine evaluates the Long-Premium Momentum Alternative:

#### 1. Substitution Pre-Conditions (`_momentum_gate`):

1. **Sell-Side Refusal Required**: Momentum never runs independently as an unprompted buyer. It executes strictly as a substitute when the sell-side strategy was refused with valid refusal markers (`momentum_block_markers`).
2. **0DTE Ban**: DTE == 0 is strictly blocked due to explosive afternoon theta decay. Requires DTE in [1, 4].
3. **No Mean-Reversion Overlap**: Blocked if session has completed a two-way extreme fade scalp (`session_mean_reversion_book`).
4. **Strong Trend Alignment**:
   - Morning Route (< 14:00 IST): Requires confirmed `price_regime` in `UPTREND`, `STRONG_UPTREND`, `DOWNTREND`, or `STRONG_DOWNTREND`. Requires ADX_15 ≥ 25 and spot breaking the opening range boundary.
   - Closing-Hour Route (≥ 14:00 IST): Controlled by `_in_late_momentum_window`. Requires EMA 9/21 alignment, fresh session high/low extreme within 30 min, and tape displacement > 35 pts.
5. **No Volatility Top**: India VIX gap ≤ 12% and IV behavior not spiking.

#### 2. Momentum Strike Selection & Economics (`compute_momentum_params`):

- Direction: +1 → **`LONG_CALL`**, -1 → **`LONG_PUT`**.
- Delta Target: Delta ≈ 0.45 - 0.55 (slightly In-The-Money or exact At-The-Money, maximizing gamma while controlling spread friction).
- **Hard Edge Hurdle**:
  ```text

  ```

Expected Capture = Option Premium × momentum_target_frac (0.35)

```
  ```text
Expected Capture ≥ Friction × 2.0
```

  If the target profit does not cover twice the round-trip ticket costs, the momentum trade is aborted.

- **Stop Loss & Target Structure**:
  - Stop Loss: Premium drops by 20% to 25%.
  - Profit Target: Premium gains 35% to 45%.
  - Spot Invalidation Backstop: Opposite side of intraday VWAP.

---

### 14. Dominant Structures, Position Sizing, and Risk Allocation

#### 1. Dominant Strategy Structures:

- **`IRON_CONDOR`**: Primary range harvester on quiet/normal sessions (DTE ≥ 0). 4 legs (Short OTM Put + Long Far OTM Put, Short OTM Call + Long Far OTM Call). Delta-neutral.
- **`IRON_BUTTERFLY`**: 0DTE pin harvester on very narrow morning sessions. 4 legs (Short ATM Put + Long Wing Put, Short ATM Call + Long Wing Call).
- **`BULL_PUT_SPREAD`**: Bullish trend structure or Two-Way Auction Low Fade. 2 legs (Short OTM Put + Long Lower OTM Put). Net credit.
- **`BEAR_CALL_SPREAD`**: Bearish trend structure or Two-Way Auction High Fade. 2 legs (Short OTM Call + Long Higher OTM Call). Net credit.
- **`LONG_CALL` / `LONG_PUT`**: Intraday momentum substitutes on trend expansion days (DTE in [1, 4]). 1 leg (Long ATM/Slightly ITM option). Net debit.

#### 2. Position Sizing Equations (`_estimate_lots` & `compute_params`):

Sizing uses a multi-tiered constraint model:

- **Stop Efficacy Blend**:
  ```text

  ```

Structural Loss per Lot = efficacy · Stop Loss + (1 - efficacy) · Wing Loss

```
  (Where `efficacy` defaults to 0.55, hard-capped at 0.80; on failed-break Bull Put verticals `efficacy` is locked at 0.80).
- **Risk Budget**:
  ```text
Max Risk = Current Capital × max_risk_per_trade_pct (0.006)
```

```text
Raw Lots = (Max Risk) / (Structural Loss per Lot)
```

```text
Sized Lots = Raw Lots × size_mult
```

- **Minimum Economic Fraction (`_min_frac = 0.60`)**:
  - If Sized Lots < 0.60: Triggers minimum-ticket affordability check. If Raw Lots ≥ 1.0 on clear setup with non-unclear positioning, clips to 1 lot. Otherwise rejects: `risk_budget_allows_only_X_lots_below_min_0.60`.
- **Fixed-Cost Amortization Floor (`v3.10 [G6]`)**:
  - On HIGH-confidence setups with thin gross capture, if final_lots == 1 but Raw Lots ≥ 1.5 (and non-event day), sizes up to \min(round(raw_lots), day_cap, 3) so fixed order brokerage is amortized.
- **Daily Day Cap Table (`LOT_CAPS_BY_DAY`) Scaled by Equity**:
  ```text

  ```

Day Cap = \max(1, floor( LOT_CAPS_BY_DAY[day] × sqrt(Capital / Starting Capital) ))

```
  ```python
  LOT_CAPS_BY_DAY = {
      "MONDAY":    8,
      "TUESDAY":   10,
      "WEDNESDAY": 6,
      "THURSDAY":  6,
      "FRIDAY":    5,
  }
```

- **Capital Margin Constraint**:
  ```text

  ```

Wing Margin per Lot = Wing Width × Lot Size (65) × 1.10 × (1 + addon)

```
  ```text
Total Margin = Margin per Lot × Lots ≤ Capital × 0.80
```

---

### 15. Event-Driven Backtest Engine CLI & Replay Architecture

`backtest_engine.py` provides a deterministic, tick/1-minute event-driven historical replay harness that runs the identical `StrategyEngine`, `RegimeEngine`, and `MarketDataEngine` instances as live trading.

#### 1. Harness Simulation Architecture:

- **`SimClock`**: Stand-in for `core.now_ist()` and `core.today_ist()`. Injects simulated naive datetimes into `core`, `data_engine`, `regime_engine`, `strategy_engine`, `execution_engine`, and `calibration_engine` namespaces.
- **`HistoricalStore` & `MultiStore`**: Read-only SQLite interfaces. `MultiStore` routes across multi-day splits (`nifty_algo_YYYY-MM-DD.db`), allowing rolling Sharpe, Sortino, and multi-session maximum drawdown calculations.
- **`DaySlice`**: Ingests 1-minute OHLCV bars and multi-expiry chain snapshots (`by_time_exp`), preventing chain collision across Tuesday weekly and far-dated series.
- **`ReplayClient`**: Intercepts all broker API requests (`get_ltp`, order placement), feeding stored snapshots to `MarketDataEngine` while raising fatal exceptions if broker endpoints are reached.
- **`FillModel`**: Two-sided spread-aware execution simulation:
  ```text

  ```

Price_SELL = bid + (mid - bid) · (edge) / (0.5)

```
  ```text
Price_BUY = ask - (ask - mid) · (edge) / (0.5)
```

  On urgent exits (priority stops 1, 2, 3, 7), edge ≤ftarrow edge · stress_mult (default 0.25 × 0.50 = 0.125).

#### 2. Replay Cycle Lifecycle (`run_day`):

For each recorded snapshot in `day.cycles`:

1. `SimClock.set(capture_dt)`
2. `ReplayClient.point(day, capture_time)`
3. `MarketDataEngine.run_cycle()` → Updates indicators, rolling VWAP, and ATM straddles.
4. `BacktestRunner._classify()` → Calls `regime.process_signals()` and merges regime snapshot.
5. Exit Evaluation on Active Position:
   - Evaluates Hard Exits (`_hard_exit_of`), Profit Targets, Dynamic Trail Stops, Proximity Triggers, and Delta Breaches on the **position's original expiry chain** (preventing false Tuesday 0DTE pricing).
   - If exit triggers: Executes `_close()`, logs `Trade`, records rupee P&L, commissions, slippage, and marks cooldowns.
6. Entry Evaluation (`StrategyEngine.decide`):
   - If flat: Calls `se.decide(signals)`. If `action == "ENTER"`, executes `_open()`, deducting statutory fees and slippage.
7. Logs rejection bucket to `res.reason_log` for the sequential entry funnel.

#### 3. CLI Command Line Reference:

```bash
python3 backtest_engine.py [OPTIONS]
```

- `--db [PATHS ...]`: Path to one or more SQLite database files, or a directory containing `nifty_algo_YYYY-MM-DD.db` files.
- `--from YYYY-MM-DD`: Start date filter (inclusive).
- `--to YYYY-MM-DD`: End date filter (inclusive).
- `--capital AMOUNT`: Override starting capital (default: ₹500,000).
- `--fill-edge FLOAT`: Execution pricing model edge:
  - `0.0`: Pessimistic (pay full ask / sell full bid).
  - `0.25`: Conservative institutional default (pays 25% away from quote edge toward mid).
  - `0.5`: Mid-price execution.
- `--stress-exit FLOAT`: Edge retained during emergency stops (default: 0.50).
- `--csv PATH`: Output path for the completed trade blotter CSV.
- `--audit`: Inspect database coverage, candle continuity, chain depth, and exit without running simulation.
- `--test`: Run internal unit and harness regression test suite.
- `--verbose`: Enable cycle-by-cycle logging to console.
- `--trade-report {each_cycle, on_change, off}`: Trade reporting verbosity:
  - `each_cycle`: Prints trade mark-to-market status on every evaluation cycle.
  - `on_change`: Prints trade only on entry, exit, or significant P&L change.
  - `off`: Suppresses per-trade logging, outputting only the final audit.

#### 4. Diagnostic Funnel Architecture (`STAGE_ORDER`):

The funnel categorizes rejections strictly in the order `decide()` evaluates them:

1. `safety interlocks`: Account drawdown halts, VIX spikes, circuit filters, day move exhausted.
2. `entry window`: Time < 09:20, time > 14:15, runway to hard exit < 50-90 min.
3. `position limits`: Max concurrent positions (1), daily entry count, cooldowns, consecutive stops.
4. `contract / DTE`: DTE > 4, missing expiry resolution.
5. `regime verdict`: Upstream regime rejects (vol neutral, choppy, observing, event restriction).
6. `strategy selection`: Day structure bearish vetoes, lean skips.
7. `counter-trend symmetry`: Counter-trend entry refusal against measured trend displacement.
8. `structure rules`: Butterfly delta/spot buffer, condor mature ADX, put/call spread VWAP alignment.
9. `structure build`: Leg pricing, gross/net credit, wing cost dominance, credit-risk ratio, margin, sizing.
10. `EV gate`: Tri-partite continuous-time barrier expected value hurdle.

---

### 16. Comprehensive Decision & Rejection Census Dictionary

The engine produces structured rejection tokens. The following dictionary maps tokens to exact root causes:

#### 1. Hard Gate Tokens:

- `daily_loss_limit_reached_or_halted`: Intraday drawdown exceeded maximum daily loss threshold.
- `circuit_breaker_suspected`: Spot index movement halted or exceeded maximum velocity safety bound.
- `vix_spike_detected`: India VIX jumped ≥ 15% intraday.
- `iv_expanding_never_sell_into_rising_iv`: Implied volatility expanding rapidly; short premium prohibited.
- `before_entry_window_09:20`: Current clock time before trading start window.
- `past_entry_window_14:15`: Current clock time past last allowable entry window.
- `max_concurrent_positions_reached`: An active position is already open in the single-slot engine.
- `max_entries_per_day_2_reached`: Session trade count ceiling met.
- `entry_cooldown_Xmin_remaining`: Cooldown period active following prior trade closure.
- `no_material_change_since_exit_Xpts_lt_Ypts_needed`: Spot price has not moved far enough from last exit price.
- `2_consecutive_stops_halt`: Engine disabled for remainder of day following two consecutive stopped trades.
- `same_signal_combo_caused_last_stop`: Refusing entry on identical signal signature that caused prior stop.
- `stop_cooldown_Xmin_remaining`: Cooldown active following a stopped trade.
- `spot_velocity_too_fast`: Spot moved too many index points over rolling 5 minutes.
- `straddle_expanding_no_sell_into_rising_iv`: ATM straddle price expanding; premium selling prohibited.
- `opening_range_pending`: 15-minute opening range bar incomplete.
- `open_spike_wait_unresolved_lower_high`: Morning spike high unconfirmed; waiting until 12:15 IST.
- `chain_stale_cannot_validate_strikes`: Option chain timestamp older than 120 seconds.
- `confidence_LOW_insufficient_edge_after_costs`: Statistical confidence too low to overcome cost friction.
- `dte_X_above_max_4_intraday_only`: Expiry date beyond 4 trading sessions.
- `day_move_used_Xpct_of_opening_straddle_no_edge`: Intraday move exhausted expected range.
- `only_Xmin_before_hard_exit_need_Y`: Remaining session minutes insufficient for theta decay.
- `wide_or_VERY_WIDE_dangerous_to_sell_premium`: High opening volatility makes range structures dangerous.

#### 2. Strategy Entry Rule Tokens:

- `butterfly_spot_too_far_from_atm_X`: Spot deviated > 50 pts from ATM strike.
- `butterfly_requires_dte_0_or_1_not_X`: Iron Butterfly attempted on DTE ≥ 2.
- `butterfly_too_late_after_12:00_on_0dte`: Iron Butterfly attempted in afternoon session.
- `butterfly_blocked_adx_X_needs_flat_below_22`: Market trend too strong for pin strategy.
- `condor_needs_Xmin_before_exit_only_Ymin`: Iron Condor runway below required minimum.
- `condor_blocked_strong_adx_X`: Iron Condor attempted with ADX ≥ 25.
- `condor_requires_mature_adx`: ADX calculation has not accumulated sufficient 15-min bars.
- `condor_banned_on_two_way_auction`: Iron Condor attempted on expanding two-way session.
- `weekly_condor_blocked_open_spike_wick`: Weekly condor blocked by unconfirmed opening wick.
- `put_spread_spot_X_below_or_mid_Y`: Bull Put spread spot too deep below opening range center.
- `put_spread_spot_X_below_vwap_Y`: Bull Put spread spot more than 30 pts below VWAP.
- `call_spread_spot_X_above_or_mid_Y`: Bear Call spread spot too deep above opening range center.
- `call_spread_spot_X_above_vwap_Y`: Bear Call spread spot more than 30 pts above VWAP.
- `bear_call_spot_X_near_max_pain_Y`: Bear Call spread blocked by Max Pain magnet below.

#### 3. Parameter & Structural Economics Tokens:

- `chain_only_X_strikes`: Incomplete chain snapshot with fewer than 10 strikes.
- `gross_credit_X_non_positive`: Structure yields zero or negative gross premium.
- `net_credit_X_non_positive_after_costs`: Gross credit completely consumed by entry friction.
- `net_credit_Xpts_friction_Ypts_is_Zpct_gt_35pct`: Friction consumes more than 35% of net credit.
- `brokerage_Xpts_at_Ylots_is_Zpct_gt_20pct`: Brokerage order fees consume more than 20% of credit.
- `X_wing_costs_Ypct_of_short_premium_gt_70pct`: Long protective wing is too expensive relative to short leg.
- `credit_risk_ratio_X_below_min_Y`: Net credit divided by wing width fails minimum threshold.
- `target_Xpts_below_Yx_roundtrip_friction`: Profit target does not clear required friction multiple.
- `ev_gate:ev_Xpts_below_min_Ypts`: Net continuous expected value falls below mathematical hurdle.
- `risk_budget_allows_only_X_lots_below_min_1`: Sizing algorithm cannot allocate at least 1 full lot within risk limits.
- `construct_fail_sticky:X`: Structural rejection latched; suppressing redundant computations until auction moves.

#### 4. Momentum Alternative Tokens:

- `momentum_disabled`: Long-premium substitute disabled in configuration.
- `momentum_sell_side_open`: Momentum rejected because sell-side structure was not cleanly blocked.
- `momentum_dte_0_below_min`: Long premium rejected on 0DTE due to theta cliff.
- `momentum_needs_trend_got_RANGE_BOUND`: Momentum requires confirmed directional price regime.
- `momentum_adx_X_below_min`: Trend strength insufficient (ADX < 25).
- `momentum_call_at_or_below_vwap`: Long Call rejected because spot is below VWAP.
- `momentum_put_at_or_above_vwap`: Long Put rejected because spot is above VWAP.
- `momentum_iv_expanding_no_chase`: Buying premium blocked because IV is already spiking.
- `momentum_day_move_used_Xpct_exhausted`: Directional move has already consumed available intraday range.
- `momentum_expected_capture_Xpts_below_Yx_friction`: Long option target gain does not cover twice round-trip execution friction.
