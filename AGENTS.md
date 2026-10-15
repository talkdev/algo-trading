# Repository Guidelines

## Project Structure & Module Organization

This is a Python NIFTY intraday options engine. Root modules include `main.py` (live cycle), `backtest_engine.py` (replays that cycle), `data_engine.py`, `regime_engine.py`, `strategy_engine.py`, and `execution_engine.py`. `core.py` owns configuration, persistence, and broker access; `bot_controller.py` and `telegram_reporter.py` handle Telegram. `data/` holds databases and replay shards; `logs/` has runtime output; `stock-data/` has cached market data. Treat generated files as runtime artifacts, not source.

## Build, Test, and Development Commands

Run commands from the repository root. Configure local settings before connecting to a broker.

- `python main.py` starts the live engine and may place real orders.
- `python backtest_engine.py --db data/per_day/nifty_algo_YYYY-MM-DD.db --force-lots 0 --verbose` replays a day with natural sizing.
- `python verify_all.py` runs module self-checks and static preflight; its preflight currently references a missing `clean-db.py`, so review that failure separately.
- `python tape_state_engine.py` runs the tape-state module self-test.

Restart after engine changes. Use per-day shards for replay while `main.py` has the primary database open.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and uppercase configuration constants. Keep decisions in the appropriate engine module and preserve shared `MainEngine.run_one_cycle()` behavior. Comment safety invariants and non-obvious gates. No formatter or linter is configured.

## Testing Guidelines

There is no separate test directory or declared pytest suite. Modules expose self-tests; use `python verify_all.py` for broad checks and the relevant module self-test for focused changes. For strategy or execution changes, replay per-day shards and inspect decision reasons and closed-position records. Never use a live run as a test.

## Commit & Pull Request Guidelines

Recent commits use short, unprefixed summaries rather than a strict Conventional Commits format. Keep subjects concise and action-oriented. Pull requests should explain the affected decision path, summarize behavior changes, include verification/replay commands and results, and link relevant issues. For trading-rule changes, include representative replay dates and the observed decision or exit reasons.

## Security & Configuration

Keep broker tokens, Telegram credentials, and other secrets out of commits. Do not publish `env.txt`, account data, or databases containing sensitive information. Use replay mode for development and verify broker-facing changes carefully before any live session.
