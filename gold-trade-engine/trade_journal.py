"""
Persistent trade journal — signals, news assessments, decisions, orders, fills.

Separate from commodity-events gold_signals.db (news collector store).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import ENGINE_DIR

UTC = timezone.utc
log = logging.getLogger("gold_news.trade_journal")

DEFAULT_JOURNAL = ENGINE_DIR / "trade_journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    signal_id TEXT,
    direction TEXT,
    trend TEXT,
    breakout TEXT,
    regime TEXT,
    atr REAL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS news_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    bias TEXT,
    score REAL,
    fresh INTEGER,
    conflicting INTEGER,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    action TEXT,
    reason TEXT,
    side TEXT,
    signal_id TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    intent_tag TEXT,
    order_id TEXT,
    side TEXT,
    quantity INTEGER,
    status TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    order_id TEXT,
    side TEXT,
    quantity INTEGER,
    price REAL,
    tag TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    event TEXT,
    side TEXT,
    quantity INTEGER,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    event TEXT,
    detail TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts_utc);
CREATE INDEX IF NOT EXISTS idx_signals_sid ON signals(signal_id);
"""


class TradeJournal:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_JOURNAL
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # DELETE journal mode avoids lingering WAL handles that block Windows temp cleanup.
        con = sqlite3.connect(str(self.path), timeout=15)
        con.execute("PRAGMA journal_mode=DELETE")
        return con

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    def _insert(self, table: str, cols: dict[str, Any]) -> None:
        keys = list(cols.keys())
        placeholders = ",".join("?" for _ in keys)
        sql = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({placeholders})"
        try:
            con = self._connect()
            try:
                con.execute(sql, [cols[k] for k in keys])
                con.commit()
            finally:
                con.close()
        except sqlite3.Error as exc:
            log.warning("journal write %s failed: %s", table, exc)

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=UTC).isoformat()

    def log_signal(self, signal: Any, ts_utc: Optional[str] = None) -> None:
        d = signal.to_dict() if hasattr(signal, "to_dict") else dict(signal)
        self._insert("signals", {
            "ts_utc": ts_utc or self._now(),
            "signal_id": d.get("signal_id"),
            "direction": d.get("direction"),
            "trend": d.get("trend"),
            "breakout": d.get("breakout"),
            "regime": d.get("regime"),
            "atr": d.get("atr"),
            "payload": json.dumps(d, default=str),
        })

    def log_news(self, assessment: Any, ts_utc: Optional[str] = None) -> None:
        d = assessment.to_dict() if hasattr(assessment, "to_dict") else dict(assessment)
        self._insert("news_assessments", {
            "ts_utc": ts_utc or self._now(),
            "bias": d.get("bias"),
            "score": d.get("score"),
            "fresh": 1 if d.get("fresh") else 0,
            "conflicting": 1 if d.get("conflicting") else 0,
            "payload": json.dumps(d, default=str),
        })

    def log_decision(self, decision: Any, ts_utc: Optional[str] = None) -> None:
        d = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        self._insert("decisions", {
            "ts_utc": ts_utc or d.get("timestamp_utc") or self._now(),
            "action": d.get("action"),
            "reason": d.get("reason"),
            "side": d.get("side"),
            "signal_id": d.get("signal_id"),
            "payload": json.dumps(d, default=str),
        })

    def log_order(self, *, intent_tag: str = "", order_id: str = "",
                  side: str = "", quantity: int = 0, status: str = "",
                  payload: Optional[dict] = None) -> None:
        self._insert("orders", {
            "ts_utc": self._now(),
            "intent_tag": intent_tag,
            "order_id": order_id,
            "side": side,
            "quantity": int(quantity),
            "status": status,
            "payload": json.dumps(payload or {}, default=str),
        })

    def log_fill(self, *, order_id: str = "", side: str = "", quantity: int = 0,
                 price: float = 0.0, tag: str = "", payload: Optional[dict] = None) -> None:
        self._insert("fills", {
            "ts_utc": self._now(),
            "order_id": order_id,
            "side": side,
            "quantity": int(quantity),
            "price": float(price),
            "tag": tag,
            "payload": json.dumps(payload or {}, default=str),
        })

    def log_position(self, event: str, pos: Any = None, payload: Optional[dict] = None) -> None:
        body: dict[str, Any] = payload or {}
        if pos is not None:
            body.update({
                "side": getattr(pos, "side", None),
                "quantity": getattr(pos, "quantity", None),
                "entry_price": getattr(pos, "entry_price", None),
                "stop_price": getattr(pos, "stop_price", None),
                "target_price": getattr(pos, "target_price", None),
            })
        self._insert("positions", {
            "ts_utc": self._now(),
            "event": event,
            "side": body.get("side"),
            "quantity": body.get("quantity"),
            "entry_price": body.get("entry_price"),
            "stop_price": body.get("stop_price"),
            "target_price": body.get("target_price"),
            "payload": json.dumps(body, default=str),
        })

    def log_risk(self, event: str, detail: str = "", payload: Optional[dict] = None) -> None:
        self._insert("risk_events", {
            "ts_utc": self._now(),
            "event": event,
            "detail": detail,
            "payload": json.dumps(payload or {}, default=str),
        })
