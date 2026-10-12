"""End-of-day report writer — archive decisions / P&L from the trade journal."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import ENGINE_DIR

UTC = timezone.utc
log = logging.getLogger("gold_news.eod_report")
REPORTS_DIR = ENGINE_DIR / "reports"


def write_eod_report(
    *,
    journal_path: Path | str,
    trading_day: date | str,
    equity: float,
    realized_pnl: float,
    trades_today: int,
    blocked: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    day = str(trading_day)
    path = REPORTS_DIR / f"eod_{day}.json"
    jp = Path(journal_path)
    decisions: list[dict] = []
    fills = 0
    if jp.exists():
        try:
            con = sqlite3.connect(str(jp), timeout=10)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT ts_utc, action, reason, side, signal_id FROM decisions "
                    "WHERE ts_utc LIKE ? ORDER BY id",
                    (f"{day}%",),
                ).fetchall()
                # Also match ISO dates that include time on that UTC day
                if not rows:
                    rows = con.execute(
                        "SELECT ts_utc, action, reason, side, signal_id FROM decisions "
                        "WHERE substr(ts_utc,1,10)=? ORDER BY id",
                        (day,),
                    ).fetchall()
                decisions = [dict(r) for r in rows]
                fills = int(con.execute(
                    "SELECT COUNT(*) FROM fills WHERE substr(ts_utc,1,10)=?", (day,),
                ).fetchone()[0])
            finally:
                con.close()
        except sqlite3.Error as exc:
            log.warning("eod journal read failed: %s", exc)

    body = {
        "trading_day": day,
        "generated_utc": datetime.now(tz=UTC).isoformat(),
        "equity": equity,
        "realized_pnl": realized_pnl,
        "trades_today": trades_today,
        "fills": fills,
        "blocked": blocked,
        "decision_count": len(decisions),
        "decisions": decisions[-200:],
        "extra": extra or {},
    }
    path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    log.info("EOD report written %s (decisions=%d fills=%d)", path, len(decisions), fills)
    return path
