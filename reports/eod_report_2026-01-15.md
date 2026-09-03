# NIFTY Intraday Options Engine — EOD Forensic Report
**Target Date:** 2026-01-15  |  **Report Generated:** 2026-09-03T11:34:23.816432

## 0. How to Read This Report (context for an LLM/analyst with no prior context)

This report describes one trading day of a NIFTY-50 intraday options-selling/buying algo engine. The engine architecture is 5 files: (1) core infra — SQLite DB + Upstox API client + logging, (2) market data engine — computes VRP (implied vol minus realized vol), Parkinson realized volatility, ADX/trend condition, opening range, VWAP/PCR/skew directional bias, (3) strategy engine — selects a strategy (Iron Condor/Butterfly, Bull Put/Bear Call spreads, debit spreads, straddles, or NO_TRADE) based on a VRP x Trend x Direction decision matrix, computes strikes/credit/sizing/stops, (4) execution engine — pre-trade validation, paper/live order routing, per-cycle position monitoring (stop/target/VWAP/ADX/delta breach exits), (5) main engine — orchestration loop (5-min cycles, 10:00-15:00 trading window), EOD summary. Every table referenced below comes directly from the engine's SQLite database (`session_state`, `cycle_log`, `strategy_decisions`, `positions`, `position_legs`, `trade_entries`, `trade_exits`, `daily_summary`, `option_chain_snapshot`, `api_call_log`, `audit_log`) plus the file-based audit log. Full untruncated raw data for every table is exported alongside this report in `eod_report_2026-01-15_raw/`.

## 1. Table of Contents

2. Executive Summary
3. Auto-Detected Anomalies / Flags
4. Session Configuration Snapshot
5. Market Data Timeline (cycle_log)
6. Strategy Decision Log
7. No-Trade Reason Frequency
8. Trade-by-Trade Deep Dive
9. Position & Leg Raw Detail
10. Option Chain Snapshot Statistics
11. API Call Health
12. Data Quality Checks
13. Audit Log — Warnings & Errors
14. Unified Master Timeline
15. Daily Summary (as computed by the engine itself)
16. Raw Data Export Manifest

## 2. Executive Summary

- **Day label**: N/A
- **Day mode**: N/A
- **VIX regime (final)**: N/A
- **OR condition / width**: N/A
- **Trades attempted (decisions)**: 0
- **Trades executed**: 0
- **Trades closed**: 0
- **Wins / Losses**: 0 / 0
- **Net P&L (Rs, from trade_exits)**: 0
- **Realized daily_pnl (session_state)**: N/A
- **Current capital (session_state)**: N/A
- **Daily halted**: N/A
- **Consecutive stops**: N/A
- **Cycles logged**: 0
- **Option chain snapshot rows**: 0
- **API calls made**: 0
- **Audit log lines (file)**: 0

## 3. Auto-Detected Anomalies / Flags

- [OK] No major anomalies auto-detected.

## 4. Session Configuration Snapshot (full session_state row)

_No session_state row found for this date — the engine likely never ran on this date._

## 5. Market Data Timeline (cycle_log — every 5-minute cycle)

Total cycles recorded: 0. Timing gaps (>10 min between consecutive cycles) detected: 0.

_(no data)_

## 6. Strategy Decision Log (every decision, chronological)

_(no data)_

## 7. No-Trade Reason Frequency

_No NO_TRADE decisions recorded (or no decisions at all)._

## 8. Trade-by-Trade Deep Dive

_No trades were entered on this date._

## 9. Position & Leg Raw Detail

_(no data)_


### All Legs

_(no data)_

## 10. Option Chain Snapshot Statistics

- **total_rows**: 0


_Full option chain snapshot (0 rows) exported to raw JSON — too large to inline here._

## 11. API Call Health

- **total_calls**: 0

## 12. Data Quality Checks

- **Cycles with missing spot**: 0
- **Cycles with missing VIX**: 0
- **Cycles with missing VRP**: 0
- **Cycles with volatility_condition=UNKNOWN**: 0
- **Cycles with trend_condition=OR_PENDING**: 0
- **Option chain rows with zero bid/ask**: 0
- **Timing gaps (>10min) in cycle_log**: 0
- **Trades with no matching exit row**: 0

## 13. Audit Log — Warnings & Errors (file-based log, filtered)

Total WARNING/ERROR/CRITICAL lines today: 0 (of 0 total log lines; 0 rows in audit_log DB table)


_No warnings or errors logged today._

## 14. Unified Master Timeline (all events merged chronologically)

_(no data)_

## 15. Daily Summary (as computed by the engine's own EOD process)

_No daily_summary row found — EOD tasks may not have run yet for this date (e.g., if the engine is still mid-session or was killed before EOD).

## 16. Raw Data Export Manifest

All tables for 2026-01-15 were exported in full (untruncated) to: `eod_report_2026-01-15_raw/`
