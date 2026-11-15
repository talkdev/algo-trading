"""One-off: print ENTER/exit summary for listed days via backtest."""
from __future__ import annotations

import sys
from datetime import date

from backtest_engine import BacktestEngine


def main(days: list[str]) -> None:
    for ds in days:
        y, m, d = map(int, ds.split("-"))
        eng = BacktestEngine(
            start=date(y, m, d),
            end=date(y, m, d),
            force_lots=0,
            trade_report="on_change",
        )
        res = eng.run()
        print("=" * 60, ds, "pnl", getattr(res, "total_pnl", None) or getattr(res, "pnl", None))
        trades = getattr(res, "trades", None) or []
        if not trades and hasattr(eng, "reporter"):
            trades = getattr(eng.reporter, "trades", None) or []
        for t in trades:
            if isinstance(t, dict):
                print(
                    t.get("entry_time") or t.get("enter_time") or t.get("t"),
                    t.get("strategy") or t.get("strategy_name"),
                    t.get("reason") or t.get("entry_reason"),
                    "pnl=", t.get("pnl") or t.get("realized_pnl"),
                    "exit=", t.get("exit_reason") or t.get("close_reason"),
                )
            else:
                print(t)


if __name__ == "__main__":
    main(sys.argv[1:] or ["2026-11-09"])
