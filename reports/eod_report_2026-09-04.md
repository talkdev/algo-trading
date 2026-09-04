# NIFTY Intraday Options Engine — EOD Forensic Report
**Target Date:** 2026-09-04  |  **Report Generated:** 2026-09-04T10:57:39.402173

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
- **NIFTY open / close / range**: 23961.55 / 23961.55 / 0.0pts (0.0%)
- **VRP mean today (pp)**: 3.462
- **ATM IV open / close (pct)**: 9.7 / 9.7
- **IV crush today (pp)**: 0
- **Option chain snapshot rows**: 174
- **API calls made**: 5
- **Audit log lines (file)**: 123

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
- **opening_iv**: 0.09699700165310858
- **opening_pcr**: 1.206876379899232
- **current_capital**: 1000000.0
- **daily_pnl**: 0.0
- **circuit_breaker_suspected**: 0
- **vix_spike_detected**: 0
- **event_announced**: 0
- **paper_trade_mode**: 1
- **created_at**: 2026-09-04T10:56:25.337273+05:30
- **updated_at**: 2026-09-04T10:56:26.000242+05:30
- **or_computed**: 1
- **session_initialized**: 1
- **vix_regime_last_checked**: 2026-09-04T10:56:25.774622+05:30
- **prev_spot**: 23961.55
- **prev_vix**: 10.84
- **parkinson_rv_pct**: 0.06237523607273729
- **parkinson_rv_computed_date**: 2026-09-04
- **vwap_valid**: 1
- **expiry_last_checked**: 2026-09-04T10:56:25.890558+05:30
- **pre_event_spot**: None
- **pre_event_iv**: None
- **event_announcement_time**: None

## 5. NIFTY Intraday Profile

- **open**: 23961.55
- **close**: 23961.55
- **high**: 23961.55
- **low**: 23961.55
- **range_pts**: 0.0
- **range_pct**: 0.0
- **net_change_pts**: 0.0
- **net_change_pct**: 0.0
- **direction**: FLAT

- **actual_day_range_pts**: 0.0
- **actual_day_range_pct**: 0.0
- **expected_daily_range_pct_from_vix**: 0.683
- **range_vs_expected_ratio**: 0.0
- **day_classification**: LOW_MOVE
- **avg_hold_minutes_today**: None
- **nifty_intraday_note**: NIFTY 50 weekly expiry is Tuesday. 0DTE gamma risk highest on Tuesday afternoon. Normal daily range for NIFTY in 2026 is 0.5-1.2% based on VIX 12-18 regime.

## 6. VRP / Volatility Deep Dive

- **vrp_mean**: 3.462
- **vrp_min**: 3.462
- **vrp_max**: 3.462
- **vrp_stdev**: 0
- **vrp_positive_cycles**: 1
- **vrp_negative_cycles**: 0
- **vrp_rich_cycles**: 1
- **vrp_very_rich_cycles**: 0
- **vrp_cheap_cycles**: 0
- **total_vrp_cycles**: 1
- **atm_iv_open_pct**: 9.7
- **atm_iv_close_pct**: 9.7
- **atm_iv_mean_pct**: 9.7
- **atm_iv_min_pct**: 9.7
- **atm_iv_max_pct**: 9.7
- **iv_crush_pct**: 0
- **parkinson_rv_mean_pct**: 6.238
- **parkinson_rv_min_pct**: 6.238
- **parkinson_rv_max_pct**: 6.238


**VRP interpretation for NIFTY options:** VRP > 3pp = RICH (sell premium). VRP 1.5-3pp = FAIR (reduced size). VRP < 0 = CHEAP (buy premium). NIFTY ATM IV in 2026 typically ranges 12-22% depending on VIX regime. IV crush on event days can be 15-30%. Normal daily IV change is -3 to +5%.

## 7. ADX / Trend Profile

- **adx_open**: 0.0
- **adx_close**: 0.0
- **adx_mean**: 0.0
- **adx_max**: 0.0
- **adx_min**: 0.0
- **adx_condition_distribution**: {'FLAT': 1}
- **trending_cycles**: 0
- **strong_trend_cycles**: 0
- **flat_cycles**: 1


**ADX interpretation for NIFTY intraday:** ADX < 20 = flat/range (ideal for condors). ADX 20-25 = weak trend (spreads ok). ADX 25-32 = moderate trend (directional spreads only). ADX > 32 = strong trend (exit condors, avoid new sells). ADX > 40 = very strong trend (no premium selling at all).

## 8. PCR / Skew / Directional Profile

- **pcr_open**: 1.207
- **pcr_close**: 1.207
- **pcr_mean**: 1.207
- **pcr_min**: 1.207
- **pcr_max**: 1.207
- **pcr_change_open_to_close**: 0.0
- **extreme_fear_cycles**: 0
- **extreme_greed_cycles**: 0
- **neutral_cycles**: 1

- **skew_open**: 1.014
- **skew_close**: 1.014
- **skew_mean**: 1.014
- **skew_min**: 1.014
- **skew_max**: 1.014
- **fear_skew_cycles**: 0
- **complacent_skew_cycles**: 0


**PCR interpretation for NIFTY:** PCR > 1.5 = extreme fear (contrarian bullish). PCR 1.0-1.5 = fear/put heavy. PCR 0.8-1.0 = neutral. PCR < 0.7 = greed/call heavy (contrarian bearish). **Skew interpretation:** Skew > 1.25 = put IV premium (fear), sell calls. Skew 0.95-1.10 = balanced. Skew < 0.95 = call IV premium (complacency), sell puts.

## 9. Market Data Timeline (cycle_log)

Total cycles: 1. Gaps detected: 0.

| cycle_time | spot | vix | vrp | atm_iv_pct | parkinson_rv_pct | adx | adx_condition | vwap_dist_pct | pcr | skew_ratio | or_condition | volatility_condition | trend_condition | direction | action_taken | no_trade_reason | open_positions | daily_pnl_net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-04T10:56:25.958827+05:30 | 23961.5500 | 10.8400 | 3.4622 | 9.6997 | 6.2375 | 0.0000 | FLAT | 0.0922 | 1.2069 | 1.0143 | VERY_NARROW | RICH | TRENDING | NEUTRAL | STRATEGY_SELECTED:IRON_CONDOR |  | 0 | 0.0000 |

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
| 2026-09-04T10:56:25.994580+05:30 | STRATEGY_SELECTED | IRON_CONDOR | trending_neutral_condor_half_size+RICH_vrp |


### 11a. Full Parameters for Selected Strategies


**2026-09-04T10:56:25.994580+05:30 — IRON_CONDOR**

```json
{
  "valid": true,
  "total_fixed_costs_rupees": 94.819254524,
  "strategy_name": "IRON_CONDOR",
  "strategy_type": "SELL",
  "selection_reason": "trending_neutral_condor_half_size+RICH_vrp",
  "target_expiry": "2026-09-08",
  "actual_dte": 4,
  "legs": [
    {
      "strike": 24150.0,
      "option_type": "call",
      "action": "SELL",
      "exec_price": 38.9,
      "bid": 38.9,
      "ask": 39.0,
      "ltp": 38.9,
      "delta": 0.2589,
      "gamma": 0.0013,
      "vega": 8.3177,
      "theta": -9.7081,
      "iv": 0.0978,
      "oi": 7483970
    },
    {
      "strike": 23800.0,
      "option_type": "put",
      "action": "SELL",
      "exec_price": 34.5,
      "bid": 34.5,
      "ask": 34.55,
      "ltp": 34.55,
      "delta": -0.2308,
      "gamma": 0.0012,
      "vega": 7.8187,
      "theta": -9.2538,
      "iv": 0.0992,
      "oi": 15852070
    },
    {
      "strike": 24300.0,
      "option_type": "call",
      "action": "BUY",
      "exec_price": 12.5,
      "bid": 12.45,
      "ask": 12.5,
      "ltp": 12.5,
      "delta": 0.1058,
      "gamma": 0.0007,
      "vega": 4.6976,
      "theta": -5.4315,
      "iv": 0.0969,
      "oi": 12287015
    },
    {
      "strike": 23650.0,
      "option_type": "put",
      "action": "BUY",
      "exec_price": 14.7,
      "bid": 14.65,
      "ask": 14.7,
      "ltp": 14.65,
      "delta": -0.1094,
      "gamma": 0.0007,
      "vega": 4.8123,
      "theta": -6.1688,
      "iv": 0.1074,
      "oi": 4433910
    }
  ],
  "num_legs": 4,
  "gross_credit": 46.2,
  "gross_debit": null,
  "total_slippage": 0.12499999999999911,
  "total_costs_pts": 1.6055074819076922,
  "total_costs_rupees_per_lot": 9.5387318,
  "entry_credit": 44.46949251809231,
  "entry_debit": null,
  "stop_premium": 57.810340273520005,
  "target_premium": 30.23925491230277,
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
  "final_lots": 1,
  "max_loss_per_lot": 1083.9438801285003,
  "total_max_risk": 1083.9438801285003,
  "estimated_margin": 11212.5,
  "hard_exit_time": "15:00",
  "target_pct": 0.32,
  "entry_spot": 23961.55,
  "entry_vix": 10.84,
  "entry_vrp": 3.4621765580371298,
  "entry_time": "2026-09-04T10:56:25.981203+05:30",
  "wing_width": 150.0,
  "stop_at_breakeven": false,
  "stop_moved_to_25pct": false,
  "last_known_premium": 44.46949251809231
}
```

## 12. No-Trade Reason Frequency

_No NO_TRADE decisions recorded._

## 13. Trade-by-Trade Deep Dive


### Position `87054612-717e-481c-b269-4e1c76a06832` — IRON_CONDOR

- **Entry time**: 2026-09-04T10:56:25.999994+05:30
- **Day label**: FRIDAY
- **Selection reason**: trending_neutral_condor_half_size+RICH_vrp
- **Entry spot / VIX / VRP**: 23961.55 / 10.84 / 3.4621765580371298
- **ATM IV at entry**: 0.09699700165310858
- **Parkinson RV at entry**: 0.06237523607273729
- **ADX at entry**: 0.0
- **VWAP dist at entry**: 0.09216641836118664
- **PCR at entry**: 1.206876379899232
- **PCR change at entry**: 0.0
- **Skew at entry**: 1.0143149284253579
- **Volatility condition**: RICH
- **IV behavior**: STABLE
- **Trend condition**: TRENDING
- **ADX condition**: FLAT
- **Direction**: NEUTRAL
- **VWAP signal**: NEUTRAL
- **PCR signal**: STABLE
- **Skew signal**: BALANCED
- **Preferred sell side**: BOTH
- **OR condition / width**: VERY_NARROW / 20.450000000000728
- **Target expiry / DTE**: 2026-09-08 / 4
- **Entry credit/debit (pts)**: 44.46949251809231
- **Gross credit (pts)**: 46.2
- **Slippage (pts)**: 0.12499999999999911
- **Entry costs (Rs)**: 104.357986324
- **Stop premium (pts)**: 57.810340273520005
- **Target premium (pts)**: 30.23925491230277
- **Price stop (pts)**: 80
- **Final lots**: 1
- **Max loss/lot (Rs)**: 1083.9438801285003
- **Total max risk (Rs)**: 1083.9438801285003
- **Capital at entry**: 1000000.0
- **Daily P&L at entry**: 0.0
- **Paper trade**: 1


**Legs at entry:**

| action | option_type | strike | exec_price | delta | gamma | vega | theta | iv | oi |
|---|---|---|---|---|---|---|---|---|---|
| SELL | call | 24150.0000 | 38.9000 | 0.2589 | 0.0013 | 8.3177 | -9.7081 | 0.0978 | 7483970 |
| SELL | put | 23800.0000 | 34.5000 | -0.2308 | 0.0012 | 7.8187 | -9.2538 | 0.0992 | 15852070 |
| BUY | call | 24300.0000 | 12.5000 | 0.1058 | 0.0007 | 4.6976 | -5.4315 | 0.0969 | 12287015 |
| BUY | put | 23650.0000 | 14.7000 | -0.1094 | 0.0007 | 4.8123 | -6.1688 | 0.1074 | 4433910 |


**Exit:** _No matching exit row — position may still be open or exit failed to record. Check positions.status for 87054612-717e-481c-b269-4e1c76a06832._


**Recomputed cost breakdown (independent cross-check):**

- **stt**: 8.26
- **exchange**: 2.68
- **brokerage**: 80.0
- **sebi**: 0.0075
- **stamp**: 0.0612
- **gst**: 14.88
- **total**: 105.89

- Recorded entry costs (Rs): 104.357986324

- Cost discrepancy (Rs): None

## 14. Exit Reason Analysis

_No exits recorded today._


**Exit reason benchmarks for NIFTY intraday options:** CLOSE_TARGET (profit at 30-35% of credit) = ideal outcome. CLOSE_STOP (premium doubled) = loss, review entry conditions. CLOSE_TIME (hard exit 15:00) = neutral, time decay captured. CLOSE_VWAP = directional risk management. CLOSE_ADX = trend risk management. CLOSE_DELTA = gamma risk management. EOD_CLOSE = position held too long, review entry timing.

## 15. Signal Conditions at Entry

- **vrp_at_entry**: {'values': [3.4621765580371298], 'mean': 3.462}
- **vix_at_entry**: {'values': [10.84], 'mean': 10.84}
- **adx_at_entry**: {'values': [0.0], 'mean': 0.0}
- **volatility_condition_at_entry**: {'RICH': 1}
- **trend_condition_at_entry**: {'TRENDING': 1}
- **direction_at_entry**: {'NEUTRAL': 1}


**Optimal entry conditions for NIFTY intraday premium selling:** VRP > 3pp, VIX 13-20, ADX < 25, OR condition NARROW or VERY_NARROW, volatility_condition RICH or VERY_RICH, iv_behavior STABLE or DECLINING, direction NEUTRAL or aligned with spread side, time 10:30-13:00 IST.

## 16. Position and Leg Raw Detail

| position_id | strategy_name | entry_time | final_lots | entry_credit | entry_debit | status | exit_time | exit_reason | net_pnl_rupees | paper_trade |
|---|---|---|---|---|---|---|---|---|---|---|
| 87054612-717e-481c-b269-4e1c76a06832 | IRON_CONDOR | 2026-09-04T10:56:25.998669+05:30 | 1 | 44.4695 |  | OPEN |  |  |  | 1 |


### All Legs

| position_id | strike | option_type | action | qty | entry_price | exit_price | entry_delta | entry_gamma | entry_vega | entry_iv | leg_status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 87054612-717e-481c-b269-4e1c76a06832 | 24300.0000 | call | BUY | 65 | 12.5000 |  | 0.1058 | 0.0007 | 4.6976 | 0.0969 | OPEN |
| 87054612-717e-481c-b269-4e1c76a06832 | 23650.0000 | put | BUY | 65 | 14.7000 |  | -0.1094 | 0.0007 | 4.8123 | 0.1074 | OPEN |
| 87054612-717e-481c-b269-4e1c76a06832 | 24150.0000 | call | SELL | 65 | 38.9000 |  | 0.2589 | 0.0013 | 8.3177 | 0.0978 | OPEN |
| 87054612-717e-481c-b269-4e1c76a06832 | 23800.0000 | put | SELL | 65 | 34.5000 |  | -0.2308 | 0.0012 | 7.8187 | 0.0992 | OPEN |

## 17. P&L Curve (intraday)

| time | pnl | spot | vrp | adx |
|---|---|---|---|---|
| 2026-09-04T10:56:25.958827+05:30 | 0.0000 | 23961.5500 | 3.4622 | 0.0000 |

## 18. Option Chain Statistics

- **total_rows**: 174
- **unique_capture_times**: 1
- **unique_strikes**: 87
- **strike_range**: [22100.0, 26400.0]
- **zero_bid_ask_count**: 0
- **zero_bid_ask_pct**: 0.0
- **avg_spread**: 41.174
- **max_spread**: 329.3
- **avg_iv_pct**: 19.07
- **iv_range_pct**: [5.98, 67.5]
- **total_call_oi**: 230650680
- **total_put_oi**: 228596615
- **chain_pcr**: 0.991
- **first_capture**: 2026-09-04T10:56:25.955280+05:30
- **last_capture**: 2026-09-04T10:56:25.955280+05:30


_Full chain (174 rows) in raw JSON export._

## 19. ATM IV Intraday History

| capture_time | strike | option_type | bid | ask | ltp | iv | delta | oi |
|---|---|---|---|---|---|---|---|---|
| 2026-09-04T10:56:25.955280+05:30 | 23900.0000 | call | 159.5500 | 159.8500 | 159.8500 | 0.1093 | 0.6222 | 9242155 |
| 2026-09-04T10:56:25.955280+05:30 | 23900.0000 | put | 59.1500 | 59.3000 | 59.1500 | 0.0938 | -0.3591 | 24454495 |
| 2026-09-04T10:56:25.955280+05:30 | 23950.0000 | call | 127.0000 | 127.1500 | 127.1500 | 0.1062 | 0.5541 | 10378745 |
| 2026-09-04T10:56:25.955280+05:30 | 23950.0000 | put | 76.8000 | 76.9000 | 76.9000 | 0.0912 | -0.4377 | 16476720 |
| 2026-09-04T10:56:25.955280+05:30 | 24000.0000 | call | 98.6000 | 98.9000 | 98.8500 | 0.1035 | 0.4806 | 21574800 |
| 2026-09-04T10:56:25.955280+05:30 | 24000.0000 | put | 98.3000 | 98.4000 | 98.4000 | 0.0884 | -0.5234 | 15266615 |
| 2026-09-04T10:56:25.955280+05:30 | 24050.0000 | call | 74.6000 | 74.8000 | 74.6000 | 0.1010 | 0.4042 | 9786335 |
| 2026-09-04T10:56:25.955280+05:30 | 24050.0000 | put | 123.8500 | 124.0000 | 124.0000 | 0.0858 | -0.6131 | 3719755 |


### Wing IV History (25-delta strikes)

| capture_time | strike | option_type | bid | ask | ltp | iv | delta | oi |
|---|---|---|---|---|---|---|---|---|
| 2026-09-04T10:56:25.955280+05:30 | 23750.0000 | put | 25.6500 | 25.7500 | 25.6000 | 0.1013 | -0.1802 | 6643650 |
| 2026-09-04T10:56:25.955280+05:30 | 23800.0000 | put | 34.5000 | 34.5500 | 34.5500 | 0.0992 | -0.2308 | 15852070 |
| 2026-09-04T10:56:25.955280+05:30 | 23850.0000 | put | 45.1000 | 45.2500 | 45.2500 | 0.0963 | -0.2896 | 9953190 |
| 2026-09-04T10:56:25.955280+05:30 | 24100.0000 | call | 54.5000 | 54.6500 | 54.5500 | 0.0992 | 0.3290 | 14162135 |
| 2026-09-04T10:56:25.955280+05:30 | 24150.0000 | call | 38.9000 | 39.0000 | 38.9000 | 0.0978 | 0.2589 | 7483970 |
| 2026-09-04T10:56:25.955280+05:30 | 24200.0000 | call | 27.3000 | 27.3500 | 27.3000 | 0.0972 | 0.1978 | 14992315 |

## 20. API Call Health

- **total_calls**: 5
- **by_category**: {'default': 1, 'quote': 1, 'historical': 1, 'chain': 2}
- **error_count**: 0
- **rate_limited_count**: 0
- **avg_response_ms**: 118.0
- **p95_response_ms**: None
- **max_response_ms**: 394.9
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
- **Cost discrepancies detected**: 0

## 22. Historical Performance (30-day lookback)

_No historical exit data available in 30-day lookback window._

## 23. Prior Days Comparison

| trading_date | day_label | trades_executed | win_rate_pct | net_pnl_rupees | net_pnl_pct_capital | vrp_mean | or_condition | stops_fired | profit_factor | capital_end |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-03 | THURSDAY | 0 | 0.0000 | 0.0000 | 0.0000 | 2.8760 | MODERATE | 0 | 0.0000 | 1000000.0000 |


### Prior Days Signal Averages

| trading_date | avg_vrp | avg_adx | avg_vix | avg_spot | cycle_count |
|---|---|---|---|---|---|
| 2026-09-03 | 2.8760 | 12.0949 | 11.2252 | 23914.9685 | 27 |

## 24. NIFTY Intraday Benchmarks

- **actual_day_range_pts**: 0.0
- **actual_day_range_pct**: 0.0
- **expected_daily_range_pct_from_vix**: 0.683
- **range_vs_expected_ratio**: 0.0
- **day_classification**: LOW_MOVE
- **avg_hold_minutes_today**: None
- **nifty_intraday_note**: NIFTY 50 weekly expiry is Tuesday. 0DTE gamma risk highest on Tuesday afternoon. Normal daily range for NIFTY in 2026 is 0.5-1.2% based on VIX 12-18 regime.

## 25. Audit Log — Warnings and Errors

Total WARNING/ERROR/CRITICAL lines: 5 of 123 total (15 in DB table)


```
2026-09-04 10:53:04 | WARNING  | nifty_algo               | PRE_TRADE_NO_GO: portfolio_vega_-294.1_exceeds_limit_150.0
2026-09-04 10:54:58 | ERROR    | nifty_algo               | UNHANDLED ERROR in main loop: name 'ipft' is not defined
2026-09-04 10:54:58 | ERROR    | nifty_algo               | Traceback (most recent call last):
2026-09-04 10:55:29 | ERROR    | nifty_algo               | UNHANDLED ERROR in main loop: name 'ipft' is not defined
2026-09-04 10:55:29 | ERROR    | nifty_algo               | Traceback (most recent call last):
```

## 26. Unified Master Timeline

| time | type | detail |
|---|---|---|
| 2026-09-04 10:53:04 | LOG:WARNING | nifty_algo               \| PRE_TRADE_NO_GO: portfolio_vega_-294.1_exceeds_limit_150.0 |
| 2026-09-04 10:54:58 | LOG:ERROR | nifty_algo               \| UNHANDLED ERROR in main loop: name 'ipft' is not defined |
| 2026-09-04 10:54:58 | LOG:ERROR | nifty_algo               \| Traceback (most recent call last): |
| 2026-09-04 10:55:29 | LOG:ERROR | nifty_algo               \| UNHANDLED ERROR in main loop: name 'ipft' is not defined |
| 2026-09-04 10:55:29 | LOG:ERROR | nifty_algo               \| Traceback (most recent call last): |
| 2026-09-04T10:56:25.958827+05:30 | CYCLE | spot=23961.55 vix=10.84 vrp=3.4621765580371298 vol=RICH trend=TRENDING dir=NEUTRAL adx=0.0 pcr=1.206876379899232 skew=1. |
| 2026-09-04T10:56:25.994580+05:30 | DECISION | STRATEGY_SELECTED IRON_CONDOR — trending_neutral_condor_half_size+RICH_vrp |
| 2026-09-04T10:56:25.999994+05:30 | ENTRY | IRON_CONDOR lots=1 credit/debit=44.46949251809231 vrp=3.4621765580371298 vix=10.84 vol_cond=RICH trend=TRENDING dir=NEUT |

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
