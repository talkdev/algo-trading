# Gold Trade Engine — Operator notes

## Day-to-day (single process)

```text
cd gold-trade-engine
python main.py
```

`main.py` starts **embedded** background producers:

| Thread | Module | Output |
|--------|--------|--------|
| Trading loop | `main.py` | Upstox candles/quotes → gates → risk → paper/live orders |
| Monitor | `monitor-gold.py` | `monitor_snapshot.json` (context + contract health) |
| News | `commodity-events.py` | `gold_signals.db` (AI scores; never places orders) |

Disable producers: `python main.py --no-producers` or `"run_embedded_producers": false` in `config.json`.

Console shows a compact dashboard each cycle (gold, data OK/NO, news, entry checklist, SL/TP preview, decision). Use `python main.py --verbose` for the old step-by-step audit log.

## Modes

- `mode=paper`, `live_armed=false` — default; no live orders.
- Do not set `live_armed=true` until paper forward tests and broker reconcile checks pass.
- Strategy default: `strategy_mode=trend_breakout` (15m EMA20/50 + 5m 20-bar breakout + ATR).
- Legacy DXY/yield path: `strategy_mode=legacy_macro` (disabled unless set).

## Contract

- Default symbol: **GOLDM** (100g). Multiplier and lot/tick come from the Upstox instrument master.
- COMEX `GC=F` / Yahoo gold are **context only** — never used as the executable MCX price.
- If embedded monitor MCX key ≠ engine key → entries blocked (`HALT_DATA`).

## Decisions

`BUY` / `SELL` / `WAIT` / `HOLD` / `EXIT` / `HALT_DATA` / `HALT_RISK` (see cycle board + `trade_journal.db`).

## Kill switch

1. Create empty file: `gold-trade-engine/KILL_SWITCH`
2. Engine blocks new entries (`HALT_RISK`).
3. Open positions are not auto-flattened by kill switch alone — use session flatten or manual/broker tools.
4. Remove the file only after broker state is understood.

## Session flatten + EOD

- Entries stop at `entry_cutoff`.
- Flatten `flatten_before_close_minutes` before `market_close`.
- Daily archive: `reports/eod_YYYY-MM-DD.json`.

## Calendar / news keys

- Put Upstox + optional TE/FMP + NVIDIA/Telegram keys in **repo-root** `env.txt`.
- News producer needs `NVIDIA_API_KEY` (and Telegram vars unless `news_producer_no_telegram`).

## Limits

- `risk_per_trade_pct` 0.25%, `maximum_daily_loss_pct` 1%
- `max_trades_per_day` 5, `max_consecutive_losses` 3 (configurable)
