# NIFTY Intraday Options Engine — EOD Forensic Report
**Target Date:** 2026-09-04  |  **Report Generated:** 2026-09-04T11:44:08.629746

## 0. How to Read This Report

This report describes one trading day of a NIFTY-50 intraday options-selling/buying algo engine. The engine runs 5-minute cycles from 10:00-15:00 IST. It sells premium (Iron Condor, Iron Butterfly, Bull Put Spread, Bear Call Spread) when VRP is positive and buys premium (Bull/Bear debit spreads, Long Straddle) when IV is cheap. All positions are intraday only — no overnight holding. NIFTY weekly expiry is Tuesday. The engine uses VRP x Trend x Direction decision matrix. Key signals: VRP (ATM IV minus Parkinson RV), ADX (trend strength), VWAP distance, PCR, 25-delta skew. Exit triggers: premium stop (2x credit), price stop (spot moves), profit target (30-35% of credit), VWAP breach, ADX breach (>30), delta breach (short leg delta >0.40), hard exit at 15:00.

## 1. Table of Contents

2. Executive Summary
3. Auto-Detected Anomalies
4. Session Configuration
5. NIFTY Intraday Profile
6. VRP / Volatility Deep Dive
7. ADX / Trend Profile
8. PCR / Skew / Directional Profile
9. Market Data Timeline (cycle_log)
10. Gate Blockage Analysis
11. Strategy Decision Log
12. No-Trade Reason Frequency
13. Trade-by-Trade Deep Dive
14. Exit Reason Analysis
15. Signal Conditions at Entry
16. Position and Leg Raw Detail
17. P&L Curve (intraday)
18. Option Chain Statistics
19. ATM IV Intraday History
20. API Call Health
21. Data Quality Checks
22. Historical Performance (30-day lookback)
23. Prior Days Comparison
24. NIFTY Intraday Benchmarks
25. Audit Log Warnings and Errors
26. Unified Master Timeline
27. Daily Summary
28. LLM Optimization Notes
29. Raw Data Export Manifest

## 2. Executive Summary

- **Day label**: FRIDAY
- **Day mode**: NORMAL
- **VIX regime (final)**: LOW
- **OR condition / width**: VERY_NARROW / 20.450000000000728
- **Trades attempted (decisions)**: 1
- **Trades executed**: 1
- **Trades closed**: 0
- **Wins / Losses**: 0 / 0
- **Gross P&L (Rs)**: 0
- **Total Costs (Rs)**: 0
- **Net P&L (Rs, from trade_exits)**: 0
- **Net P&L as pct of capital**: 0.0%
- **Realized daily_pnl (session_state)**: 0.0
- **Current capital (session_state)**: 1000000.0
- **Daily halted**: 0
- **Consecutive stops**: 0
- **Cycles logged**: 1
- **NIFTY open / close / range**: 23984.05 / 23984.05 / 0.0pts (0.0%)
- **VRP mean today (pp)**: 3.746
- **ATM IV open / close (pct)**: 9.63 / 9.63
- **IV crush today (pp)**: 0
- **Option chain snapshot rows**: 174
- **API calls made**: 5
- **Audit log lines (file)**: 12

## 3. Auto-Detected Anomalies / Flags

- [FLAG] NIFTY moved only 0.00% intraday — very low move day, premium may have been thin.

## 4. Session Configuration Snapshot

- **trading_date**: 2026-09-04
- **day_mode**: NORMAL
- **vix_regime**: LOW
- **gap_size**: SMALL
- **gap_direction**: FLAT
- **day_label**: FRIDAY
- **or_high**: 23916.3
- **or_low**: 23895.85
- **or_width**: 20.450000000000728
- **or_condition**: VERY_NARROW
- **entry_start**: 10:00
- **entry_end**: 14:00
- **hard_exit_time**: 15:00
- **stop_multiplier**: 1.3
- **size_multiplier**: 0.675
- **wing_width**: 150
- **entry_count**: 1
- **reentry_count**: 0
- **daily_halted**: 0
- **consecutive_stops**: 0
- **last_stop_time**: None
- **last_stop_reason**: None
- **actual_expiry**: 2026-09-08
- **actual_dte**: 4
- **opening_iv**: 0.09630337547510759
- **opening_pcr**: 1.8878511111843346
- **current_capital**: 1000000.0
- **daily_pnl**: 0.0
- **circuit_breaker_suspected**: 0
- **vix_spike_detected**: 0
- **event_announced**: 0
- **paper_trade_mode**: 1
- **created_at**: 2026-09-04T11:43:20.636601+05:30
- **updated_at**: 2026-09-04T11:43:21.505634+05:30
- **or_computed**: 1
- **session_initialized**: 1
- **vix_regime_last_checked**: 2026-09-04T11:43:21.269502+05:30
- **prev_spot**: 23984.05
- **prev_vix**: 10.75
- **parkinson_rv_pct**: 0.05884589032768509
- **parkinson_rv_computed_date**: 2026-09-04
- **vwap_valid**: 1
- **expiry_last_checked**: 2026-09-04T11:43:21.373958+05:30
- **pre_event_spot**: None
- **pre_event_iv**: None
- **event_announcement_time**: None
- **last_entry_time**: 2026-09-04T11:43:21.505612+05:30
- **last_stop_signal_combo**: None
- **gap_fade_opportunity**: 0
- **stop_at_breakeven**: 0
- **stop_moved_to_25pct**: 0

## 5. NIFTY Intraday Profile

- **open**: 23984.05
- **close**: 23984.05
- **high**: 23984.05
- **low**: 23984.05
- **range_pts**: 0.0
- **range_pct**: 0.0
- **net_change_pts**: 0.0
- **net_change_pct**: 0.0
- **direction**: FLAT

- **actual_day_range_pts**: 0.0
- **actual_day_range_pct**: 0.0
- **expected_daily_range_pct_from_vix**: 0.677
- **range_vs_expected_ratio**: 0.0
- **day_classification**: LOW_MOVE
- **avg_hold_minutes_today**: None
- **nifty_intraday_note**: NIFTY 50 weekly expiry is Tuesday. 0DTE gamma risk highest on Tuesday afternoon. Normal daily range for NIFTY in 2026 is 0.5-1.2% based on VIX 12-18 regime.

## 6. VRP / Volatility Deep Dive

- **vrp_mean**: 3.746
- **vrp_min**: 3.746
- **vrp_max**: 3.746
- **vrp_stdev**: 0
- **vrp_positive_cycles**: 1
- **vrp_negative_cycles**: 0
- **vrp_rich_cycles**: 1
- **vrp_very_rich_cycles**: 0
- **vrp_cheap_cycles**: 0
- **total_vrp_cycles**: 1
- **atm_iv_open_pct**: 9.63
- **atm_iv_close_pct**: 9.63
- **atm_iv_mean_pct**: 9.63
- **atm_iv_min_pct**: 9.63
- **atm_iv_max_pct**: 9.63
- **iv_crush_pct**: 0
- **parkinson_rv_mean_pct**: 5.885
- **parkinson_rv_min_pct**: 5.885
- **parkinson_rv_max_pct**: 5.885


**VRP interpretation for NIFTY options:** VRP > 3pp = RICH (sell premium). VRP 1.5-3pp = FAIR (reduced size). VRP < 0 = CHEAP (buy premium). NIFTY ATM IV in 2026 typically ranges 12-22% depending on VIX regime. IV crush on event days can be 15-30%. Normal daily IV change is -3 to +5%.

## 7. ADX / Trend Profile

- **adx_open**: 49.03
- **adx_close**: 49.03
- **adx_mean**: 49.03
- **adx_max**: 49.03
- **adx_min**: 49.03
- **adx_condition_distribution**: {'VERY_STRONG': 1}
- **trending_cycles**: 1
- **strong_trend_cycles**: 1
- **flat_cycles**: 0


**ADX interpretation for NIFTY intraday:** ADX < 20 = flat/range (ideal for condors). ADX 20-25 = weak trend (spreads ok). ADX 25-32 = moderate trend (directional spreads only). ADX > 32 = strong trend (exit condors, avoid new sells). ADX > 40 = very strong trend (no premium selling at all).

## 8. PCR / Skew / Directional Profile

- **pcr_open**: 1.888
- **pcr_close**: 1.888
- **pcr_mean**: 1.888
- **pcr_min**: 1.888
- **pcr_max**: 1.888
- **pcr_change_open_to_close**: 0.0
- **extreme_fear_cycles**: 1
- **extreme_greed_cycles**: 0
- **neutral_cycles**: 0

- **skew_open**: 1.021
- **skew_close**: 1.021
- **skew_mean**: 1.021
- **skew_min**: 1.021
- **skew_max**: 1.021
- **fear_skew_cycles**: 0
- **complacent_skew_cycles**: 0


**PCR interpretation for NIFTY:** PCR > 1.5 = extreme fear (contrarian bullish). PCR 1.0-1.5 = fear/put heavy. PCR 0.8-1.0 = neutral. PCR < 0.7 = greed/call heavy (contrarian bearish). **Skew interpretation:** Skew > 1.25 = put IV premium (fear), sell calls. Skew 0.95-1.10 = balanced. Skew < 0.95 = call IV premium (complacency), sell puts.

## 9. Market Data Timeline (cycle_log)

Total cycles: 1. Gaps detected: 0.

| cycle_time | spot | vix | vrp | atm_iv_pct | parkinson_rv_pct | adx | adx_condition | vwap_dist_pct | pcr | skew_ratio | or_condition | volatility_condition | trend_condition | direction | action_taken | no_trade_reason | open_positions | daily_pnl_net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-04T11:43:21.444630+05:30 | 23984.0500 | 10.7500 | 3.7457 | 9.6303 | 5.8846 | 49.0276 | VERY_STRONG | 0.1331 | 1.8879 | 1.0208 | VERY_NARROW | RICH | STRONG_TREND | BULLISH | STRATEGY_SELECTED:BULL_PUT_SPREAD |  | 0 | 0.0000 |

## 10. Gate Blockage Analysis

- **total_cycles**: 1
- **total_decisions**: 1
- **no_trade_count**: 0
- **enter_count**: 1
- **no_trade_rate_pct**: 0.0
- **gate_category_counts**: {'risk_gates': 0, 'data_gates': 0, 'market_condition_gates': 0, 'timing_gates': 0, 'strategy_gates': 0, 'event_gates': 0, 'other': 0}
- **top_10_reasons**: {}


**Gate analysis interpretation:** If risk_gates dominate, the engine is being too conservative on capital/loss limits. If timing_gates dominate, the trading window may be too narrow. If strategy_gates dominate, credit floors or ratio checks are blocking trades — review MIN_CREDITS and credit/width ratio thresholds. If data_gates dominate, there are API or data quality issues.

## 11. Strategy Decision Log

| decision_time | action | strategy_name | reason |
|---|---|---|---|
| 2026-09-04T11:43:21.500058+05:30 | STRATEGY_SELECTED | BULL_PUT_SPREAD | bullish_strong_trend_put_credit_safe_side+RICH_vrp |


### 11a. Full Parameters for Selected Strategies


**2026-09-04T11:43:21.500058+05:30 — BULL_PUT_SPREAD**

```json
{
  "valid": true,
  "total_fixed_costs_rupees": 47.443384336,
  "strategy_name": "BULL_PUT_SPREAD",
  "strategy_type": "SELL",
  "selection_reason": "bullish_strong_trend_put_credit_safe_side+RICH_vrp",
  "target_expiry": "2026-09-08",
  "actual_dte": 4,
  "legs": [
    {
      "strike": 23850.0,
      "option_type": "put",
      "action": "SELL",
      "exec_price": 40.7,
      "bid": 40.7,
      "ask": 40.8,
      "ltp": 40.8,
      "delta": -0.2648,
      "gamma": 0.0013,
      "vega": 8.3884,
      "theta": -9.9136,
      "iv": 0.0983,
      "oi": 10287095
    },
    {
      "strike": 23700.0,
      "option_type": "put",
      "action": "BUY",
      "exec_price": 17.7,
      "bid": 17.65,
      "ask": 17.7,
      "ltp": 17.65,
      "delta": -0.1287,
      "gamma": 0.0008,
      "vega": 5.3814,
      "theta": -6.9129,
      "iv": 0.10679999999999999,
      "oi": 10115105
    }
  ],
  "num_legs": 2,
  "gross_credit": 23.000000000000004,
  "gross_debit": null,
  "total_slippage": 0.07499999999999751,
  "total_costs_pts": 0.8122813005538462,
  "total_costs_rupees_per_lot": 5.354900200000001,
  "entry_credit": 22.11271869944616,
  "entry_debit": null,
  "stop_premium": 28.74653430928001,
  "target_premium": 15.036648715623386,
  "stop_value": null,
  "target_value": null,
  "price_stop_pts": 80,
  "tightening_schedule": [
    [
      "13:00",
      0.8
    ],
    [
      "14:00",
      0.65
    ]
  ],
  "final_lots": 2,
  "max_loss_per_lot": 538.9975182990003,
  "total_max_risk": 1077.9950365980005,
  "estimated_margin": 22425.0,
  "hard_exit_time": "15:00",
  "target_pct": 0.32,
  "entry_spot": 23984.05,
  "entry_vix": 10.75,
  "entry_vrp": 3.7457485147422505,
  "entry_time": "2026-09-04T11:43:21.470702+05:30",
  "wing_width": 150.0,
  "stop_at_breakeven": false,
  "stop_moved_to_25pct": false,
  "last_known_premium": 22.11271869944616
}
```

## 12. No-Trade Reason Frequency

_No NO_TRADE decisions recorded._

## 13. Trade-by-Trade Deep Dive


### Position `52c64b75-3ded-4313-a5fd-2b5dc118bdcb` — BULL_PUT_SPREAD

- **Entry time**: 2026-09-04T11:43:21.505198+05:30
- **Day label**: FRIDAY
- **Selection reason**: bullish_strong_trend_put_credit_safe_side+RICH_vrp
- **Entry spot / VIX / VRP**: 23984.05 / 10.75 / 3.7457485147422505
- **ATM IV at entry**: 0.09630337547510759
- **Parkinson RV at entry**: 0.05884589032768509
- **ADX at entry**: 49.0275836633565
- **VWAP dist at entry**: 0.13309162015440232
- **PCR at entry**: 1.8878511111843346
- **PCR change at entry**: 0.0
- **Skew at entry**: 1.0207684319833852
- **Volatility condition**: RICH
- **IV behavior**: STABLE
- **Trend condition**: STRONG_TREND
- **ADX condition**: VERY_STRONG
- **Direction**: BULLISH
- **VWAP signal**: NEUTRAL
- **PCR signal**: EXTREME_FEAR_CONTRARIAN
- **Skew signal**: BALANCED
- **Preferred sell side**: PUTS
- **OR condition / width**: VERY_NARROW / 20.450000000000728
- **Target expiry / DTE**: 2026-09-08 / 4
- **Entry credit/debit (pts)**: 22.11271869944616
- **Gross credit (pts)**: 23.000000000000004
- **Slippage (pts)**: 0.07499999999999751
- **Entry costs (Rs)**: 58.153184736
- **Stop premium (pts)**: 28.74653430928001
- **Target premium (pts)**: 15.036648715623386
- **Price stop (pts)**: 80
- **Final lots**: 2
- **Max loss/lot (Rs)**: 538.9975182990003
- **Total max risk (Rs)**: 1077.9950365980005
- **Capital at entry**: 1000000.0
- **Daily P&L at entry**: 0.0
- **Paper trade**: 1


**Legs at entry:**

| action | option_type | strike | exec_price | delta | gamma | vega | theta | iv | oi |
|---|---|---|---|---|---|---|---|---|---|
| SELL | put | 23850.0000 | 40.7000 | -0.2648 | 0.0013 | 8.3884 | -9.9136 | 0.0983 | 10287095 |
| BUY | put | 23700.0000 | 17.7000 | -0.1287 | 0.0008 | 5.3814 | -6.9129 | 0.1068 | 10115105 |


**Exit:** _No matching exit row — position may still be open or exit failed to record. Check positions.status for 52c64b75-3ded-4313-a5fd-2b5dc118bdcb._


**Recomputed cost breakdown (independent cross-check):**

- **stt**: 9.16
- **exchange**: 3.11
- **brokerage**: 40.0
- **sebi**: 0.0088
- **stamp**: 0.0796
- **gst**: 7.76
- **total**: 60.12

- Recorded entry costs (Rs): 58.153184736

- Cost discrepancy entry-only (Rs): None

## 14. Exit Reason Analysis

_No exits recorded today._


**Exit reason benchmarks for NIFTY intraday options:** CLOSE_TARGET (profit at 30-35% of credit) = ideal outcome. CLOSE_STOP (premium doubled) = loss, review entry conditions. CLOSE_TIME (hard exit 15:00) = neutral, time decay captured. CLOSE_VWAP = directional risk management. CLOSE_ADX = trend risk management. CLOSE_DELTA = gamma risk management. EOD_CLOSE = position held too long, review entry timing.

## 15. Signal Conditions at Entry

- **vrp_at_entry**: {'values': [3.7457485147422505], 'mean': 3.746}
- **vix_at_entry**: {'values': [10.75], 'mean': 10.75}
- **adx_at_entry**: {'values': [49.0275836633565], 'mean': 49.028}
- **volatility_condition_at_entry**: {'RICH': 1}
- **trend_condition_at_entry**: {'STRONG_TREND': 1}
- **direction_at_entry**: {'BULLISH': 1}


**Optimal entry conditions for NIFTY intraday premium selling:** VRP > 3pp, VIX 13-20, ADX < 25, OR condition NARROW or VERY_NARROW, volatility_condition RICH or VERY_RICH, iv_behavior STABLE or DECLINING, direction NEUTRAL or aligned with spread side, time 10:30-13:00 IST.

## 16. Position and Leg Raw Detail

| position_id | strategy_name | entry_time | final_lots | entry_credit | entry_debit | status | exit_time | exit_reason | net_pnl_rupees | paper_trade |
|---|---|---|---|---|---|---|---|---|---|---|
| 52c64b75-3ded-4313-a5fd-2b5dc118bdcb | BULL_PUT_SPREAD | 2026-09-04T11:43:21.504352+05:30 | 2 | 22.1127 |  | OPEN |  |  |  | 1 |


### All Legs

| position_id | strike | option_type | action | qty | entry_price | exit_price | entry_delta | entry_gamma | entry_vega | entry_iv | leg_status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 52c64b75-3ded-4313-a5fd-2b5dc118bdcb | 23700.0000 | put | BUY | 130 | 17.7000 |  | -0.1287 | 0.0008 | 5.3814 | 0.1068 | OPEN |
| 52c64b75-3ded-4313-a5fd-2b5dc118bdcb | 23850.0000 | put | SELL | 130 | 40.7000 |  | -0.2648 | 0.0013 | 8.3884 | 0.0983 | OPEN |

## 17. P&L Curve (intraday)

| time | pnl | spot | vrp | adx |
|---|---|---|---|---|
| 2026-09-04T11:43:21.444630+05:30 | 0.0000 | 23984.0500 | 3.7457 | 49.0276 |

## 18. Option Chain Statistics

- **total_rows**: 174
- **unique_capture_times**: 1
- **unique_strikes**: 87
- **strike_range**: [22100.0, 26400.0]
- **zero_bid_ask_count**: 0
- **zero_bid_ask_pct**: 0.0
- **avg_spread**: 42.42
- **max_spread**: 332.9
- **avg_iv_pct**: 19.24
- **iv_range_pct**: [6.59, 71.27]
- **total_call_oi**: 215478445
- **total_put_oi**: 249135380
- **chain_pcr**: 1.156
- **first_capture**: 2026-09-04T11:43:21.442314+05:30
- **last_capture**: 2026-09-04T11:43:21.442314+05:30


_Full chain (174 rows) in raw JSON export._

## 18b. Intraday 1-Minute Candle Statistics

- **total_1min_bars**: 148
- **first_bar_time**: 2026-09-04T09:15:00+05:30
- **last_bar_time**: 2026-09-04T11:42:00+05:30
- **open**: 23906.8
- **close**: 23985.75
- **high**: 24005.75
- **low**: 23895.85
- **total_volume**: 0
- **avg_bar_range_pts**: 6.586
- **max_bar_range_pts**: 46.95
- **zero_volume_bars**: 0
- **net_change_pts**: 78.95
- **net_change_pct**: 0.33


_Total 1-min bars in DB: 148. Used for ADX, VWAP, Parkinson RV, OR. Stored permanently._

## 19. ATM IV Intraday History

| capture_time | strike | option_type | bid | ask | ltp | iv | delta | oi |
|---|---|---|---|---|---|---|---|---|
| 2026-09-04T11:43:21.442314+05:30 | 23950.0000 | call | 137.3000 | 137.4500 | 137.3000 | 0.1048 | 0.5855 | 7942155 |
| 2026-09-04T11:43:21.442314+05:30 | 23950.0000 | put | 69.8500 | 69.9000 | 69.9000 | 0.0935 | -0.4049 | 18954650 |
| 2026-09-04T11:43:21.442314+05:30 | 24000.0000 | call | 107.2000 | 107.4500 | 107.4500 | 0.1019 | 0.5120 | 18751850 |
| 2026-09-04T11:43:21.442314+05:30 | 24000.0000 | put | 89.8000 | 90.0000 | 89.7500 | 0.0912 | -0.4870 | 20564245 |
| 2026-09-04T11:43:21.442314+05:30 | 24050.0000 | call | 81.7500 | 81.9500 | 81.9500 | 0.0999 | 0.4346 | 8348470 |
| 2026-09-04T11:43:21.442314+05:30 | 24050.0000 | put | 113.9500 | 114.0000 | 114.0000 | 0.0887 | -0.5741 | 4801095 |
| 2026-09-04T11:43:21.442314+05:30 | 24100.0000 | call | 60.2500 | 60.3500 | 60.4000 | 0.0980 | 0.3569 | 13256425 |


### Wing IV History (25-delta strikes)

| capture_time | strike | option_type | bid | ask | ltp | iv | delta | oi |
|---|---|---|---|---|---|---|---|---|
| 2026-09-04T11:43:21.442314+05:30 | 23750.0000 | put | 23.3500 | 23.4500 | 23.4500 | 0.1039 | -0.1650 | 6532500 |
| 2026-09-04T11:43:21.442314+05:30 | 23800.0000 | put | 31.0500 | 31.1500 | 31.0000 | 0.1012 | -0.2103 | 16364595 |
| 2026-09-04T11:43:21.442314+05:30 | 23850.0000 | put | 40.7000 | 40.8000 | 40.8000 | 0.0983 | -0.2648 | 10287095 |
| 2026-09-04T11:43:21.442314+05:30 | 23900.0000 | put | 53.7500 | 53.8500 | 53.8500 | 0.0961 | -0.3306 | 25877540 |
| 2026-09-04T11:43:21.442314+05:30 | 24150.0000 | call | 43.0500 | 43.1500 | 43.0500 | 0.0963 | 0.2826 | 6944015 |
| 2026-09-04T11:43:21.442314+05:30 | 24200.0000 | call | 30.0000 | 30.1000 | 30.0000 | 0.0955 | 0.2169 | 14316445 |
| 2026-09-04T11:43:21.442314+05:30 | 24250.0000 | call | 20.4000 | 20.5000 | 20.6000 | 0.0949 | 0.1607 | 6158490 |

## 20. API Call Health

- **total_calls**: 5
- **by_category**: {'default': 1, 'quote': 1, 'historical': 1, 'chain': 2}
- **error_count**: 0
- **rate_limited_count**: 0
- **avg_response_ms**: 153.0
- **p95_response_ms**: None
- **max_response_ms**: 578.7
- **slow_calls_over_2s**: 0

## 21. Data Quality Checks

- **Cycles with missing spot**: 0
- **Cycles with missing VIX**: 0
- **Cycles with missing VRP**: 0
- **Cycles with missing PCR**: 0
- **Cycles with missing skew**: 0
- **Cycles with missing VWAP dist**: 0
- **Cycles with volatility_condition=UNKNOWN**: 0
- **Cycles with trend_condition=OR_PENDING**: 0
- **Cycles with adx_condition=INSUFFICIENT_DATA**: 0
- **Option chain rows with zero bid/ask**: 0
- **Timing gaps (>10min) in cycle_log**: 0
- **Trades with no matching exit row**: 1
- **Positions still OPEN at report time**: 1
- **Cost discrepancies detected (entry only, >Rs2)**: 0

## 21b. Cumulative Engine Performance (90-day equity curve)

_No cumulative data yet — needs at least 1 completed day with daily_summary._

## 22. Historical Performance (30-day lookback)

_No historical exit data available in 30-day lookback window._

## 23. Prior Days Comparison

_No prior days summary data available._

## 24. NIFTY Intraday Benchmarks

- **actual_day_range_pts**: 0.0
- **actual_day_range_pct**: 0.0
- **expected_daily_range_pct_from_vix**: 0.677
- **range_vs_expected_ratio**: 0.0
- **day_classification**: LOW_MOVE
- **avg_hold_minutes_today**: None
- **nifty_intraday_note**: NIFTY 50 weekly expiry is Tuesday. 0DTE gamma risk highest on Tuesday afternoon. Normal daily range for NIFTY in 2026 is 0.5-1.2% based on VIX 12-18 regime.

## 25. Audit Log — Warnings and Errors

Total WARNING/ERROR/CRITICAL lines: 0 of 12 total (12 in DB table)


_No warnings or errors logged today._

## 26. Unified Master Timeline

| time | type | detail |
|---|---|---|
| 2026-09-04T11:43:21.444630+05:30 | CYCLE | spot=23984.05 vix=10.75 vrp=3.7457485147422505 vol=RICH trend=STRONG_TREND dir=BULLISH adx=49.0275836633565 pcr=1.887851 |
| 2026-09-04T11:43:21.500058+05:30 | DECISION | STRATEGY_SELECTED BULL_PUT_SPREAD — bullish_strong_trend_put_credit_safe_side+RICH_vrp |
| 2026-09-04T11:43:21.505198+05:30 | ENTRY | BULL_PUT_SPREAD lots=2 credit/debit=22.11271869944616 vrp=3.7457485147422505 vix=10.75 vol_cond=RICH trend=STRONG_TREND  |

## 27. Daily Summary (engine EOD)

_No daily_summary row found._

## 28. LLM Optimization Notes

This section summarizes what an LLM should focus on when analyzing this report to improve the engine:

1. **Gate calibration**: Check section 10 (Gate Blockage Analysis). If timing_gates or strategy_gates block >60% of decisions, thresholds need loosening. If risk_gates dominate, capital allocation or stop multiplier needs review.

2. **VRP quality**: Check section 6. If vrp_negative_cycles > 30% of total, the Parkinson RV computation may be overstating realized vol, or the day had genuine IV compression. Compare atm_iv_open vs atm_iv_close.

3. **Entry timing**: Check section 15. Optimal NIFTY intraday entry is 10:30-12:30 IST. Entries after 13:00 have reduced time for theta decay and higher gamma risk near expiry.

4. **Exit quality**: Check section 14. CLOSE_STOP exits indicate the stop multiplier (currently 2x credit) may be too tight for the day's volatility. CLOSE_TIME exits indicate positions were held too long without hitting target.

5. **ADX threshold**: Check section 7. If strong_trend_cycles > 30% of total cycles, the ADX exit threshold (currently 30) may need raising to 35 to avoid premature exits on choppy days.

6. **Credit floors**: Check section 12 (no-trade reasons). If net_credit_below_minimum appears frequently, MIN_CREDITS may be too high for the current VIX regime. In VIX 12-15 regime, NIFTY weekly ATM credit is typically 25-40pts for a 150pt wing condor.

7. **Cost drag**: Check section 13 cost discrepancy. Total round-trip costs for a 1-lot condor (4 legs) at current brokerage should be approximately Rs 200-350. If recorded costs differ by >10%, the cost computation has a bug.

8. **Chain data quality**: Check section 18. zero_bid_ask_pct > 20% indicates stale or missing chain data. avg_spread > 5pts for ATM strikes indicates poor liquidity or data quality issues.

9. **PCR and skew reliability**: Check section 8. If >50% of cycles have missing PCR or skew, the directional bias module is operating blind and direction=NEUTRAL will dominate, leading to more condor selections regardless of actual market direction.

10. **Capital carry-forward**: Check section 4 (session_state). current_capital should reflect yesterday's ending capital, not the flat starting_capital default. If they match exactly, the carry-forward logic may not have run.

## 29. Raw Data Export Manifest

All tables exported to: `eod_report_2026-09-04_raw/`
