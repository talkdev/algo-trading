# NIFTY Intraday Options Engine — EOD Forensic Report
**Target Date:** 2026-09-04  |  **Report Generated:** 2026-09-04T12:09:19.919202

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
- **Trades attempted (decisions)**: 9
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
- **Cycles logged**: 9
- **NIFTY open / close / range**: 23984.05 / 23965.35 / 25.95pts (0.108%)
- **VRP mean today (pp)**: 3.851
- **ATM IV open / close (pct)**: 9.63 / 9.486
- **IV crush today (pp)**: 0.144
- **Option chain snapshot rows**: 1566
- **API calls made**: 34
- **Audit log lines (file)**: 85

## 3. Auto-Detected Anomalies / Flags

- [FLAG] NIFTY moved only 0.11% intraday — very low move day, premium may have been thin.
- [OK] Single position engine working correctly: 'position_already_open_single_position_engine' fired 8/9 times — expected behavior while a position is open.

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
- **opening_pcr**: 1.789725750719988
- **current_capital**: 1000000.0
- **daily_pnl**: 0.0
- **circuit_breaker_suspected**: 0
- **vix_spike_detected**: 0
- **event_announced**: 0
- **paper_trade_mode**: 1
- **created_at**: 2026-09-04T11:43:20.636601+05:30
- **updated_at**: 2026-09-04T12:09:13.339304+05:30
- **or_computed**: 1
- **session_initialized**: 1
- **vix_regime_last_checked**: 2026-09-04T11:43:21.269502+05:30
- **prev_spot**: 23965.35
- **prev_vix**: 10.96
- **parkinson_rv_pct**: 0.04864801950630847
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
- **close**: 23965.35
- **high**: 23985.9
- **low**: 23959.95
- **range_pts**: 25.95
- **range_pct**: 0.108
- **net_change_pts**: -18.7
- **net_change_pct**: -0.078
- **direction**: DOWN
- **first_half_range_pts**: 3.2
- **second_half_range_pts**: 20.95
- **volatility_expansion_second_half**: True

- **actual_day_range_pts**: 25.95
- **actual_day_range_pct**: 0.108
- **expected_daily_range_pct_from_vix**: 0.681
- **range_vs_expected_ratio**: 0.16
- **day_classification**: LOW_MOVE
- **avg_hold_minutes_today**: None
- **nifty_intraday_note**: NIFTY 50 weekly expiry is Tuesday. 0DTE gamma risk highest on Tuesday afternoon. Normal daily range for NIFTY in 2026 is 0.5-1.2% based on VIX 12-18 regime.

## 6. VRP / Volatility Deep Dive

- **vrp_mean**: 3.851
- **vrp_min**: 3.746
- **vrp_max**: 4.008
- **vrp_stdev**: 0.093
- **vrp_positive_cycles**: 9
- **vrp_negative_cycles**: 0
- **vrp_rich_cycles**: 9
- **vrp_very_rich_cycles**: 0
- **vrp_cheap_cycles**: 0
- **total_vrp_cycles**: 9
- **atm_iv_open_pct**: 9.63
- **atm_iv_close_pct**: 9.486
- **atm_iv_mean_pct**: 9.619
- **atm_iv_min_pct**: 9.486
- **atm_iv_max_pct**: 9.703
- **iv_crush_pct**: 0.144
- **parkinson_rv_mean_pct**: 5.023
- **parkinson_rv_min_pct**: 4.75
- **parkinson_rv_max_pct**: 5.885


**VRP interpretation for NIFTY options:** VRP > 3pp = RICH (sell premium). VRP 1.5-3pp = FAIR (reduced size). VRP < 0 = CHEAP (buy premium). NIFTY ATM IV in 2026 typically ranges 12-22% depending on VIX regime. IV crush on event days can be 15-30%. Normal daily IV change is -3 to +5%.

## 7. ADX / Trend Profile

- **adx_open**: 49.03
- **adx_close**: 43.25
- **adx_mean**: 47.31
- **adx_max**: 49.03
- **adx_min**: 43.25
- **adx_condition_distribution**: {'VERY_STRONG': 9}
- **trending_cycles**: 9
- **strong_trend_cycles**: 9
- **flat_cycles**: 0


**ADX interpretation for NIFTY intraday:** ADX < 20 = flat/range (ideal for condors). ADX 20-25 = weak trend (spreads ok). ADX 25-32 = moderate trend (directional spreads only). ADX > 32 = strong trend (exit condors, avoid new sells). ADX > 40 = very strong trend (no premium selling at all).

## 8. PCR / Skew / Directional Profile

- **pcr_open**: 1.888
- **pcr_close**: 1.79
- **pcr_mean**: 1.896
- **pcr_min**: 1.79
- **pcr_max**: 1.974
- **pcr_change_open_to_close**: -0.098
- **extreme_fear_cycles**: 9
- **extreme_greed_cycles**: 0
- **neutral_cycles**: 0

- **skew_open**: 1.021
- **skew_close**: 1.029
- **skew_mean**: 1.038
- **skew_min**: 1.021
- **skew_max**: 1.066
- **fear_skew_cycles**: 0
- **complacent_skew_cycles**: 0


**PCR interpretation for NIFTY:** PCR > 1.5 = extreme fear (contrarian bullish). PCR 1.0-1.5 = fear/put heavy. PCR 0.8-1.0 = neutral. PCR < 0.7 = greed/call heavy (contrarian bearish). **Skew interpretation:** Skew > 1.25 = put IV premium (fear), sell calls. Skew 0.95-1.10 = balanced. Skew < 0.95 = call IV premium (complacency), sell puts.

## 9. Market Data Timeline (cycle_log)

Total cycles: 9. Gaps detected: 0.

| cycle_time | spot | vix | vrp | atm_iv_pct | parkinson_rv_pct | adx | adx_condition | vwap_dist_pct | pcr | skew_ratio | or_condition | volatility_condition | trend_condition | direction | action_taken | no_trade_reason | open_positions | daily_pnl_net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-04T11:43:21.444630+05:30 | 23984.0500 | 10.7500 | 3.7457 | 9.6303 | 5.8846 | 49.0276 | VERY_STRONG | 0.1331 | 1.8879 | 1.0208 | VERY_NARROW | RICH | STRONG_TREND | BULLISH | STRATEGY_SELECTED:BULL_PUT_SPREAD |  | 0 | 0.0000 |
| 2026-09-04T11:48:21.362659+05:30 | 23982.7000 | 10.7500 | 3.7743 | 9.6177 | 5.1512 | 48.9916 | VERY_STRONG | 0.1237 | 1.8889 | 1.0366 | VERY_NARROW | RICH | STRONG_TREND | BULLISH | NO_TRADE | position_already_open_single_position_engine | 1 | -154.3466 |
| 2026-09-04T11:53:21.402230+05:30 | 23984.0000 | 10.7600 | 3.9028 | 9.6944 | 5.0235 | 48.9703 | VERY_STRONG | 0.1245 | 1.9051 | 1.0418 | VERY_NARROW | RICH | STRONG_TREND | BULLISH | NO_TRADE | position_already_open_single_position_engine | 1 | -186.8466 |
| 2026-09-04T11:55:36.789446+05:30 | 23985.9000 | 10.7700 | 3.8998 | 9.7029 | 5.0377 | 48.9703 | VERY_STRONG | 0.1326 | 1.9200 | 1.0515 | VERY_NARROW | RICH | STRONG_TREND | MILD_BULLISH | NO_TRADE | position_already_open_single_position_engine | 1 | -167.3466 |
| 2026-09-04T11:59:19.357707+05:30 | 23980.9000 | 10.7400 | 3.9244 | 9.6605 | 4.8460 | 48.9493 | VERY_STRONG | 0.1080 | 1.9477 | 1.0323 | VERY_NARROW | RICH | STRONG_TREND | MILD_BULLISH | NO_TRADE | position_already_open_single_position_engine | 1 | -203.0966 |
| 2026-09-04T12:01:18.853640+05:30 | 23974.8500 | 10.7400 | 4.0078 | 9.6884 | 4.7498 | 47.5398 | VERY_STRONG | 0.0805 | 1.9741 | 1.0658 | VERY_NARROW | RICH | STRONG_TREND | MILD_BULLISH | NO_TRADE | position_already_open_single_position_engine | 1 | -362.3466 |
| 2026-09-04T12:03:37.245914+05:30 | 23966.7000 | 10.8000 | 3.8767 | 9.5878 | 4.7911 | 46.8333 | VERY_STRONG | 0.0469 | 1.9435 | 1.0395 | VERY_NARROW | RICH | STRONG_TREND | MILD_BULLISH | NO_TRADE | position_already_open_single_position_engine | 1 | -453.3466 |
| 2026-09-04T12:08:37.226187+05:30 | 23959.9500 | 10.9700 | 3.7733 | 9.5021 | 4.8575 | 43.2469 | VERY_STRONG | 0.0190 | 1.8035 | 1.0259 | VERY_NARROW | RICH | STRONG_TREND | MILD_BULLISH | NO_TRADE | position_already_open_single_position_engine | 1 | -524.8466 |
| 2026-09-04T12:09:13.343321+05:30 | 23965.3500 | 10.9600 | 3.7522 | 9.4863 | 4.8648 | 43.2469 | VERY_STRONG | 0.0412 | 1.7897 | 1.0291 | VERY_NARROW | RICH | STRONG_TREND | NEUTRAL | NO_TRADE | position_already_open_single_position_engine | 1 | -446.8466 |

## 10. Gate Blockage Analysis

- **total_cycles**: 9
- **total_decisions**: 9
- **no_trade_count**: 8
- **enter_count**: 1
- **no_trade_rate_pct**: 88.9
- **gate_category_counts**: {'risk_gates': 0, 'data_gates': 0, 'market_condition_gates': 0, 'timing_gates': 0, 'strategy_gates': 0, 'event_gates': 0, 'other': 8}
- **top_10_reasons**: {'position_already_open_single_position_engine': 8}


**Gate analysis interpretation:** If risk_gates dominate, the engine is being too conservative on capital/loss limits. If timing_gates dominate, the trading window may be too narrow. If strategy_gates dominate, credit floors or ratio checks are blocking trades — review MIN_CREDITS and credit/width ratio thresholds. If data_gates dominate, there are API or data quality issues.

## 11. Strategy Decision Log

| decision_time | action | strategy_name | reason |
|---|---|---|---|
| 2026-09-04T11:43:21.500058+05:30 | STRATEGY_SELECTED | BULL_PUT_SPREAD | bullish_strong_trend_put_credit_safe_side+RICH_vrp |
| 2026-09-04T11:48:21.392145+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |
| 2026-09-04T11:53:21.487355+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |
| 2026-09-04T11:55:36.850515+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |
| 2026-09-04T11:59:19.423357+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |
| 2026-09-04T12:01:18.885232+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |
| 2026-09-04T12:03:37.283938+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |
| 2026-09-04T12:08:37.295996+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |
| 2026-09-04T12:09:13.387996+05:30 | NO_TRADE | NONE | position_already_open_single_position_engine |


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

- [8x] position_already_open_single_position_engine

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
| 2026-09-04T11:48:21.362659+05:30 | -154.3466 | 23982.7000 | 3.7743 | 48.9916 |
| 2026-09-04T11:53:21.402230+05:30 | -186.8466 | 23984.0000 | 3.9028 | 48.9703 |
| 2026-09-04T11:55:36.789446+05:30 | -167.3466 | 23985.9000 | 3.8998 | 48.9703 |
| 2026-09-04T11:59:19.357707+05:30 | -203.0966 | 23980.9000 | 3.9244 | 48.9493 |
| 2026-09-04T12:01:18.853640+05:30 | -362.3466 | 23974.8500 | 4.0078 | 47.5398 |
| 2026-09-04T12:03:37.245914+05:30 | -453.3466 | 23966.7000 | 3.8767 | 46.8333 |
| 2026-09-04T12:08:37.226187+05:30 | -524.8466 | 23959.9500 | 3.7733 | 43.2469 |
| 2026-09-04T12:09:13.343321+05:30 | -446.8466 | 23965.3500 | 3.7522 | 43.2469 |

## 18. Option Chain Statistics

- **total_rows**: 1566
- **unique_capture_times**: 9
- **unique_strikes**: 87
- **strike_range**: [22100.0, 26400.0]
- **zero_bid_ask_count**: 0
- **zero_bid_ask_pct**: 0.0
- **avg_spread**: 42.284
- **max_spread**: 334.0
- **avg_iv_pct**: 19.15
- **iv_range_pct**: [5.37, 71.58]
- **total_call_oi**: 1951329575
- **total_put_oi**: 2262456040
- **chain_pcr**: 1.159
- **first_capture**: 2026-09-04T11:43:21.442314+05:30
- **last_capture**: 2026-09-04T12:09:13.339678+05:30


_Full chain (1566 rows) in raw JSON export._

## 18b. Intraday 1-Minute Candle Statistics

- **total_1min_bars**: 174
- **first_bar_time**: 2026-09-04T09:15:00+05:30
- **last_bar_time**: 2026-09-04T12:08:00+05:30
- **open**: 23906.8
- **close**: 23963.8
- **high**: 24005.75
- **low**: 23895.85
- **total_volume**: 0
- **avg_bar_range_pts**: 6.394
- **max_bar_range_pts**: 46.95
- **zero_volume_bars**: 0
- **net_change_pts**: 57.0
- **net_change_pct**: 0.238


_Total 1-min bars in DB: 174. Used for ADX, VWAP, Parkinson RV, OR. Stored permanently._

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
| 2026-09-04T11:48:21.359938+05:30 | 23950.0000 | call | 137.0000 | 137.2500 | 137.2500 | 0.1031 | 0.5883 | 7995520 |
| 2026-09-04T11:48:21.359938+05:30 | 23950.0000 | put | 70.0500 | 70.1500 | 70.0500 | 0.0944 | -0.4040 | 19061120 |
| 2026-09-04T11:48:21.359938+05:30 | 24000.0000 | call | 107.1500 | 107.3000 | 107.3000 | 0.1008 | 0.5137 | 19153680 |
| 2026-09-04T11:48:21.359938+05:30 | 24000.0000 | put | 90.0000 | 90.2000 | 90.2000 | 0.0919 | -0.4854 | 20705490 |
| 2026-09-04T11:48:21.359938+05:30 | 24050.0000 | call | 81.6000 | 81.7500 | 81.6000 | 0.0990 | 0.4362 | 8563945 |
| 2026-09-04T11:48:21.359938+05:30 | 24050.0000 | put | 114.2000 | 114.3500 | 114.2000 | 0.0896 | -0.5708 | 5027555 |
| 2026-09-04T11:48:21.359938+05:30 | 24100.0000 | call | 60.0000 | 60.1500 | 60.1500 | 0.0972 | 0.3579 | 13282295 |
| 2026-09-04T11:53:21.397814+05:30 | 23950.0000 | call | 135.8500 | 135.9000 | 135.7500 | 0.1035 | 0.5880 | 7955675 |
| 2026-09-04T11:53:21.397814+05:30 | 23950.0000 | put | 71.2500 | 71.4000 | 71.4000 | 0.0951 | -0.4048 | 19195020 |
| 2026-09-04T11:53:21.397814+05:30 | 24000.0000 | call | 106.6500 | 106.7000 | 106.7000 | 0.1011 | 0.5136 | 19398275 |
| 2026-09-04T11:53:21.397814+05:30 | 24000.0000 | put | 91.3000 | 91.4500 | 91.4500 | 0.0931 | -0.4856 | 20976800 |
| 2026-09-04T11:53:21.397814+05:30 | 24050.0000 | call | 81.0500 | 81.2000 | 81.1500 | 0.0993 | 0.4356 | 8465015 |
| 2026-09-04T11:53:21.397814+05:30 | 24050.0000 | put | 115.8500 | 116.1500 | 116.2000 | 0.0909 | -0.5706 | 5087940 |
| 2026-09-04T11:53:21.397814+05:30 | 24100.0000 | call | 59.7500 | 59.8000 | 59.7500 | 0.0975 | 0.3575 | 13494845 |
| 2026-09-04T11:55:36.784774+05:30 | 23950.0000 | call | 137.4000 | 137.7500 | 137.4500 | 0.1031 | 0.5906 | 7986095 |
| 2026-09-04T11:55:36.784774+05:30 | 23950.0000 | put | 70.7500 | 70.9000 | 70.9000 | 0.0955 | -0.4026 | 19322615 |
| 2026-09-04T11:55:36.784774+05:30 | 24000.0000 | call | 107.6000 | 107.6500 | 107.6500 | 0.1009 | 0.5160 | 19389500 |
| 2026-09-04T11:55:36.784774+05:30 | 24000.0000 | put | 91.0000 | 91.0500 | 91.0000 | 0.0935 | -0.4830 | 21273915 |
| 2026-09-04T11:55:36.784774+05:30 | 24050.0000 | call | 82.3000 | 82.4000 | 82.4000 | 0.0992 | 0.4380 | 8465080 |
| 2026-09-04T11:55:36.784774+05:30 | 24050.0000 | put | 115.0500 | 115.3500 | 115.0500 | 0.0915 | -0.5676 | 5157100 |
| 2026-09-04T11:55:36.784774+05:30 | 24100.0000 | call | 60.8000 | 60.9000 | 60.9500 | 0.0974 | 0.3597 | 13562445 |
| 2026-09-04T11:59:19.354606+05:30 | 23950.0000 | call | 135.2000 | 135.5000 | 135.2000 | 0.1042 | 0.5827 | 7956065 |
| 2026-09-04T11:59:19.354606+05:30 | 23950.0000 | put | 71.6000 | 71.7500 | 71.6500 | 0.0946 | -0.4095 | 19468540 |
| 2026-09-04T11:59:19.354606+05:30 | 24000.0000 | call | 105.6500 | 105.9000 | 105.9000 | 0.1016 | 0.5085 | 19410105 |
| 2026-09-04T11:59:19.354606+05:30 | 24000.0000 | put | 91.7500 | 91.9500 | 91.8000 | 0.0921 | -0.4910 | 21524230 |
| 2026-09-04T11:59:19.354606+05:30 | 24050.0000 | call | 80.3000 | 80.5000 | 80.3000 | 0.0996 | 0.4308 | 8362965 |
| 2026-09-04T11:59:19.354606+05:30 | 24050.0000 | put | 116.4000 | 116.7000 | 116.7000 | 0.0900 | -0.5770 | 5182775 |
| 2026-09-04T11:59:19.354606+05:30 | 24100.0000 | call | 59.1500 | 59.2500 | 59.2000 | 0.0977 | 0.3528 | 13591760 |
| 2026-09-04T12:01:18.851212+05:30 | 23900.0000 | call | 162.6500 | 163.0500 | 163.0500 | 0.1060 | 0.6421 | 7180810 |
| 2026-09-04T12:01:18.851212+05:30 | 23950.0000 | call | 129.8000 | 130.1500 | 130.2000 | 0.1028 | 0.5731 | 7833670 |
| 2026-09-04T12:01:18.851212+05:30 | 23950.0000 | put | 74.5000 | 74.6500 | 74.6500 | 0.0945 | -0.4209 | 19440005 |
| 2026-09-04T12:01:18.851212+05:30 | 24000.0000 | call | 100.8500 | 101.1500 | 101.1500 | 0.1010 | 0.4975 | 19022835 |
| 2026-09-04T12:01:18.851212+05:30 | 24000.0000 | put | 95.5000 | 95.7000 | 95.7000 | 0.0921 | -0.5032 | 21560955 |
| 2026-09-04T12:01:18.851212+05:30 | 24050.0000 | call | 76.6000 | 76.7000 | 76.7500 | 0.0987 | 0.4190 | 8141705 |
| 2026-09-04T12:01:18.851212+05:30 | 24050.0000 | put | 120.5500 | 120.8000 | 120.5000 | 0.0898 | -0.5894 | 5234775 |
| 2026-09-04T12:03:37.239863+05:30 | 23900.0000 | call | 157.4000 | 157.6500 | 157.3000 | 0.1064 | 0.6304 | 7184190 |
| 2026-09-04T12:03:37.239863+05:30 | 23900.0000 | put | 59.1000 | 59.2000 | 59.2000 | 0.0950 | -0.3551 | 26141310 |
| 2026-09-04T12:03:37.239863+05:30 | 23950.0000 | call | 125.0500 | 125.3000 | 125.0500 | 0.1030 | 0.5608 | 7834775 |
| 2026-09-04T12:03:37.239863+05:30 | 23950.0000 | put | 76.6000 | 76.7500 | 76.7500 | 0.0930 | -0.4331 | 19387030 |
| 2026-09-04T12:03:37.239863+05:30 | 24000.0000 | call | 96.8000 | 97.0000 | 96.9000 | 0.1012 | 0.4849 | 19103045 |
| 2026-09-04T12:03:37.239863+05:30 | 24000.0000 | put | 98.2000 | 98.4000 | 98.2000 | 0.0903 | -0.5174 | 21274240 |
| 2026-09-04T12:03:37.239863+05:30 | 24050.0000 | call | 73.0000 | 73.2000 | 73.2000 | 0.0990 | 0.4067 | 8174920 |
| 2026-09-04T12:03:37.239863+05:30 | 24050.0000 | put | 124.0500 | 124.3500 | 124.0500 | 0.0882 | -0.6050 | 5171985 |
| 2026-09-04T12:08:37.223365+05:30 | 23900.0000 | call | 154.1500 | 154.4000 | 154.3500 | 0.1067 | 0.6206 | 7272850 |
| 2026-09-04T12:08:37.223365+05:30 | 23900.0000 | put | 59.8000 | 59.9500 | 59.8000 | 0.0936 | -0.3638 | 25553905 |
| 2026-09-04T12:08:37.223365+05:30 | 23950.0000 | call | 122.1000 | 122.2500 | 122.3500 | 0.1038 | 0.5502 | 8229130 |
| 2026-09-04T12:08:37.223365+05:30 | 23950.0000 | put | 77.8000 | 77.9000 | 77.9000 | 0.0912 | -0.4435 | 18905770 |
| 2026-09-04T12:08:37.223365+05:30 | 24000.0000 | call | 94.3500 | 94.4000 | 94.4000 | 0.1013 | 0.4744 | 19990165 |
| 2026-09-04T12:08:37.223365+05:30 | 24000.0000 | put | 99.8500 | 100.0500 | 100.0500 | 0.0890 | -0.5297 | 20108855 |
| 2026-09-04T12:08:37.223365+05:30 | 24050.0000 | call | 71.0000 | 71.1500 | 71.2000 | 0.0994 | 0.3965 | 8520785 |
| 2026-09-04T12:08:37.223365+05:30 | 24050.0000 | put | 126.1500 | 126.3500 | 126.0500 | 0.0867 | -0.6188 | 4795635 |
| 2026-09-04T12:09:13.339678+05:30 | 23900.0000 | call | 156.3500 | 156.7500 | 156.7500 | 0.1067 | 0.6269 | 7259200 |
| 2026-09-04T12:09:13.339678+05:30 | 23900.0000 | put | 58.4500 | 58.6000 | 58.6000 | 0.0938 | -0.3569 | 25561250 |

_... 6 more row(s) omitted — see raw JSON export._


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
| 2026-09-04T11:48:21.359938+05:30 | 23750.0000 | put | 23.6000 | 23.7000 | 23.6000 | 0.1047 | -0.1653 | 6426485 |
| 2026-09-04T11:48:21.359938+05:30 | 23800.0000 | put | 31.3000 | 31.3500 | 31.2500 | 0.1019 | -0.2103 | 16470610 |
| 2026-09-04T11:48:21.359938+05:30 | 23850.0000 | put | 41.1000 | 41.2500 | 41.1000 | 0.0992 | -0.2647 | 10380305 |
| 2026-09-04T11:48:21.359938+05:30 | 23900.0000 | put | 54.0000 | 54.0500 | 54.0000 | 0.0970 | -0.3305 | 26004485 |
| 2026-09-04T11:48:21.359938+05:30 | 24150.0000 | call | 42.8000 | 42.9000 | 42.9000 | 0.0957 | 0.2833 | 6933615 |
| 2026-09-04T11:48:21.359938+05:30 | 24200.0000 | call | 29.7000 | 29.7500 | 29.7000 | 0.0945 | 0.2160 | 14274260 |
| 2026-09-04T11:48:21.359938+05:30 | 24250.0000 | call | 20.2000 | 20.2500 | 20.2000 | 0.0940 | 0.1593 | 6192030 |
| 2026-09-04T11:53:21.397814+05:30 | 23750.0000 | put | 24.0000 | 24.0500 | 24.0000 | 0.1051 | -0.1667 | 6501950 |
| 2026-09-04T11:53:21.397814+05:30 | 23800.0000 | put | 31.9000 | 32.0000 | 31.9500 | 0.1024 | -0.2118 | 16363815 |
| 2026-09-04T11:53:21.397814+05:30 | 23850.0000 | put | 41.7000 | 41.7500 | 41.8000 | 0.0998 | -0.2665 | 10379785 |
| 2026-09-04T11:53:21.397814+05:30 | 23900.0000 | put | 54.8000 | 54.9000 | 54.8000 | 0.0975 | -0.3313 | 25888395 |
| 2026-09-04T11:53:21.397814+05:30 | 24150.0000 | call | 42.5500 | 42.7000 | 42.8000 | 0.0958 | 0.2828 | 7002255 |
| 2026-09-04T11:53:21.397814+05:30 | 24200.0000 | call | 29.8000 | 29.8500 | 29.8500 | 0.0952 | 0.2171 | 14389310 |
| 2026-09-04T11:53:21.397814+05:30 | 24250.0000 | call | 20.1500 | 20.2000 | 20.1500 | 0.0945 | 0.1603 | 6239025 |
| 2026-09-04T11:55:36.784774+05:30 | 23750.0000 | put | 23.9000 | 23.9500 | 23.9500 | 0.1056 | -0.1663 | 6508060 |
| 2026-09-04T11:55:36.784774+05:30 | 23800.0000 | put | 31.6500 | 31.7500 | 31.7500 | 0.1028 | -0.2111 | 16387345 |
| 2026-09-04T11:55:36.784774+05:30 | 23850.0000 | put | 41.5000 | 41.5500 | 41.5500 | 0.1001 | -0.2650 | 10425285 |
| 2026-09-04T11:55:36.784774+05:30 | 23900.0000 | put | 54.5000 | 54.6500 | 54.5000 | 0.0980 | -0.3297 | 25975040 |
| 2026-09-04T11:55:36.784774+05:30 | 24150.0000 | call | 43.4000 | 43.5500 | 43.6000 | 0.0961 | 0.2856 | 7006740 |
| 2026-09-04T11:55:36.784774+05:30 | 24200.0000 | call | 30.4500 | 30.5000 | 30.4500 | 0.0952 | 0.2190 | 14396590 |
| 2026-09-04T11:55:36.784774+05:30 | 24250.0000 | call | 20.6500 | 20.7000 | 20.7000 | 0.0946 | 0.1622 | 6243315 |
| 2026-09-04T11:59:19.354606+05:30 | 23750.0000 | put | 24.3000 | 24.3500 | 24.3000 | 0.1048 | -0.1690 | 6517485 |
| 2026-09-04T11:59:19.354606+05:30 | 23800.0000 | put | 32.1000 | 32.2000 | 32.1000 | 0.1022 | -0.2150 | 16399435 |
| 2026-09-04T11:59:19.354606+05:30 | 23850.0000 | put | 42.0000 | 42.1000 | 42.1500 | 0.0992 | -0.2694 | 10444590 |
| 2026-09-04T11:59:19.354606+05:30 | 23900.0000 | put | 55.2500 | 55.4000 | 55.3500 | 0.0970 | -0.3352 | 26005200 |
| 2026-09-04T11:59:19.354606+05:30 | 24150.0000 | call | 42.1000 | 42.2500 | 42.1000 | 0.0961 | 0.2789 | 7020715 |
| 2026-09-04T11:59:19.354606+05:30 | 24200.0000 | call | 29.4000 | 29.5000 | 29.4000 | 0.0954 | 0.2135 | 14410630 |
| 2026-09-04T11:59:19.354606+05:30 | 24250.0000 | call | 19.8000 | 19.9000 | 19.9500 | 0.0948 | 0.1577 | 6271980 |
| 2026-09-04T12:01:18.851212+05:30 | 23750.0000 | put | 25.2500 | 25.3500 | 25.2500 | 0.1045 | -0.1751 | 6477315 |
| 2026-09-04T12:01:18.851212+05:30 | 23800.0000 | put | 33.5000 | 33.5500 | 33.6000 | 0.1021 | -0.2227 | 16537495 |
| 2026-09-04T12:01:18.851212+05:30 | 23850.0000 | put | 43.9500 | 44.0000 | 44.0000 | 0.0992 | -0.2788 | 10441535 |
| 2026-09-04T12:01:18.851212+05:30 | 23900.0000 | put | 57.6500 | 57.7000 | 57.7500 | 0.0967 | -0.3453 | 26114530 |
| 2026-09-04T12:01:18.851212+05:30 | 24100.0000 | call | 56.0500 | 56.1500 | 56.0000 | 0.0972 | 0.3415 | 13650585 |
| 2026-09-04T12:01:18.851212+05:30 | 24150.0000 | call | 39.8000 | 39.9500 | 39.9500 | 0.0958 | 0.2685 | 7001865 |
| 2026-09-04T12:01:18.851212+05:30 | 24200.0000 | call | 27.6500 | 27.7500 | 27.7000 | 0.0951 | 0.2042 | 14176955 |
| 2026-09-04T12:01:18.851212+05:30 | 24250.0000 | call | 18.7500 | 18.8000 | 18.8000 | 0.0946 | 0.1502 | 6279390 |
| 2026-09-04T12:03:37.239863+05:30 | 23750.0000 | put | 25.7500 | 25.8500 | 25.8500 | 0.1028 | -0.1792 | 6456060 |
| 2026-09-04T12:03:37.239863+05:30 | 23800.0000 | put | 34.3000 | 34.3500 | 34.4000 | 0.1001 | -0.2278 | 16645850 |
| 2026-09-04T12:03:37.239863+05:30 | 23850.0000 | put | 44.9500 | 45.0500 | 44.9500 | 0.0977 | -0.2867 | 10473450 |
| 2026-09-04T12:03:37.239863+05:30 | 24100.0000 | call | 53.3500 | 53.4000 | 53.3500 | 0.0976 | 0.3301 | 13497835 |
| 2026-09-04T12:03:37.239863+05:30 | 24150.0000 | call | 37.7000 | 37.8000 | 37.7000 | 0.0963 | 0.2586 | 6894810 |
| 2026-09-04T12:03:37.239863+05:30 | 24200.0000 | call | 26.2500 | 26.3000 | 26.3000 | 0.0957 | 0.1963 | 14171365 |
| 2026-09-04T12:08:37.223365+05:30 | 23750.0000 | put | 25.7000 | 25.7500 | 25.8000 | 0.1012 | -0.1821 | 6701305 |
| 2026-09-04T12:08:37.223365+05:30 | 23800.0000 | put | 34.5000 | 34.5500 | 34.5500 | 0.0990 | -0.2335 | 16464955 |
| 2026-09-04T12:08:37.223365+05:30 | 23850.0000 | put | 45.4500 | 45.6000 | 45.6000 | 0.0961 | -0.2932 | 10349950 |
| 2026-09-04T12:08:37.223365+05:30 | 24100.0000 | call | 51.6000 | 51.7000 | 51.7000 | 0.0977 | 0.3204 | 14005030 |
| 2026-09-04T12:08:37.223365+05:30 | 24150.0000 | call | 36.5500 | 36.6000 | 36.6000 | 0.0965 | 0.2501 | 7143760 |
| 2026-09-04T12:08:37.223365+05:30 | 24200.0000 | call | 25.5000 | 25.6000 | 25.6000 | 0.0961 | 0.1897 | 14379495 |
| 2026-09-04T12:09:13.339678+05:30 | 23750.0000 | put | 25.0000 | 25.0500 | 25.0500 | 0.1013 | -0.1778 | 6698315 |
| 2026-09-04T12:09:13.339678+05:30 | 23800.0000 | put | 33.8500 | 33.9000 | 33.9000 | 0.0989 | -0.2277 | 16453255 |
| 2026-09-04T12:09:13.339678+05:30 | 23850.0000 | put | 44.3500 | 44.5000 | 44.4000 | 0.0960 | -0.2866 | 10363665 |
| 2026-09-04T12:09:13.339678+05:30 | 24100.0000 | call | 52.7000 | 52.8000 | 52.8500 | 0.0974 | 0.3263 | 14046955 |
| 2026-09-04T12:09:13.339678+05:30 | 24150.0000 | call | 37.1500 | 37.2000 | 37.1500 | 0.0961 | 0.2551 | 7137650 |

_... 1 more row(s) omitted — see raw JSON export._

## 20. API Call Health

- **total_calls**: 34
- **by_category**: {'default': 6, 'quote': 9, 'historical': 9, 'chain': 10}
- **error_count**: 0
- **rate_limited_count**: 0
- **avg_response_ms**: 132.1
- **p95_response_ms**: 578.7
- **max_response_ms**: 635.3
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

- **actual_day_range_pts**: 25.95
- **actual_day_range_pct**: 0.108
- **expected_daily_range_pct_from_vix**: 0.681
- **range_vs_expected_ratio**: 0.16
- **day_classification**: LOW_MOVE
- **avg_hold_minutes_today**: None
- **nifty_intraday_note**: NIFTY 50 weekly expiry is Tuesday. 0DTE gamma risk highest on Tuesday afternoon. Normal daily range for NIFTY in 2026 is 0.5-1.2% based on VIX 12-18 regime.

## 25. Audit Log — Warnings and Errors

Total WARNING/ERROR/CRITICAL lines: 0 of 85 total (85 in DB table)


_No warnings or errors logged today._

## 26. Unified Master Timeline

| time | type | detail |
|---|---|---|
| 2026-09-04T11:43:21.444630+05:30 | CYCLE | spot=23984.05 vix=10.75 vrp=3.7457485147422505 vol=RICH trend=STRONG_TREND dir=BULLISH adx=49.0275836633565 pcr=1.887851 |
| 2026-09-04T11:43:21.500058+05:30 | DECISION | STRATEGY_SELECTED BULL_PUT_SPREAD — bullish_strong_trend_put_credit_safe_side+RICH_vrp |
| 2026-09-04T11:43:21.505198+05:30 | ENTRY | BULL_PUT_SPREAD lots=2 credit/debit=22.11271869944616 vrp=3.7457485147422505 vix=10.75 vol_cond=RICH trend=STRONG_TREND  |
| 2026-09-04T11:48:21.362659+05:30 | CYCLE | spot=23982.7 vix=10.75 vrp=3.77425489523469 vol=RICH trend=STRONG_TREND dir=BULLISH adx=48.99162189295276 pcr=1.88890671 |
| 2026-09-04T11:48:21.392145+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |
| 2026-09-04T11:53:21.402230+05:30 | CYCLE | spot=23984.0 vix=10.76 vrp=3.902789135433869 vol=RICH trend=STRONG_TREND dir=BULLISH adx=48.970343001270734 pcr=1.905100 |
| 2026-09-04T11:53:21.487355+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |
| 2026-09-04T11:55:36.789446+05:30 | CYCLE | spot=23985.9 vix=10.77 vrp=3.8997805132864514 vol=RICH trend=STRONG_TREND dir=MILD_BULLISH adx=48.970343001270734 pcr=1. |
| 2026-09-04T11:55:36.850515+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |
| 2026-09-04T11:59:19.357707+05:30 | CYCLE | spot=23980.9 vix=10.74 vrp=3.9243926622635383 vol=RICH trend=STRONG_TREND dir=MILD_BULLISH adx=48.94931481066527 pcr=1.9 |
| 2026-09-04T11:59:19.423357+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |
| 2026-09-04T12:01:18.853640+05:30 | CYCLE | spot=23974.85 vix=10.74 vrp=4.007762919939564 vol=RICH trend=STRONG_TREND dir=MILD_BULLISH adx=47.539819484475785 pcr=1. |
| 2026-09-04T12:01:18.885232+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |
| 2026-09-04T12:03:37.245914+05:30 | CYCLE | spot=23966.7 vix=10.8 vrp=3.8766525799733094 vol=RICH trend=STRONG_TREND dir=MILD_BULLISH adx=46.833293270145155 pcr=1.9 |
| 2026-09-04T12:03:37.283938+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |
| 2026-09-04T12:08:37.226187+05:30 | CYCLE | spot=23959.95 vix=10.97 vrp=3.77331526374424 vol=RICH trend=STRONG_TREND dir=MILD_BULLISH adx=43.246885573301725 pcr=1.8 |
| 2026-09-04T12:08:37.295996+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |
| 2026-09-04T12:09:13.343321+05:30 | CYCLE | spot=23965.35 vix=10.96 vrp=3.7521639997368705 vol=RICH trend=STRONG_TREND dir=NEUTRAL adx=43.246885573301725 pcr=1.7897 |
| 2026-09-04T12:09:13.387996+05:30 | DECISION | NO_TRADE NONE — position_already_open_single_position_engine |

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
