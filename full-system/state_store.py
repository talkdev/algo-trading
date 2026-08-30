# -*- coding: utf-8 -*-
"""
state_store.py — SQLite persistence & crash recovery (spec §9).

* state.db keeps: last spot/VIX, module scores, regime history, open legs
  (instrument, qty, entry, order_id), skew history, flow snapshots,
  persistence buffers and confirmed scores, plus a heartbeat timestamp.
* Checkpoints are written after every fill, every regime change and every
  `CHECKPOINT_HEARTBEAT_S` (heartbeat) — main.py schedules those.
* On restart the engine loads this state and continues; if state.db is
  missing the engine treats it as a fresh start.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime

from config import STATE_DB, CHECKPOINT_HEARTBEAT_S

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS regime_history(
    ts TEXT PRIMARY KEY,
    composite REAL, regime TEXT,
    vol REAL, edge REAL, trend REAL, flow REAL,
    spot REAL, vix REAL, override INTEGER, confirmed_regime TEXT
);
CREATE TABLE IF NOT EXISTS skew_history(date TEXT PRIMARY KEY, skew REAL);
CREATE TABLE IF NOT EXISTS snapshots(ts TEXT PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS buffers(module TEXT PRIMARY KEY, val TEXT);
CREATE TABLE IF NOT EXISTS confirmed(module TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS positions(
    trade_id TEXT PRIMARY KEY,
    strategy_name TEXT, regime_at_entry TEXT, status TEXT,
    legs_json TEXT, meta_json TEXT,
    entry_ts TEXT, entry_spot REAL, entry_vix REAL,
    expiry_date TEXT, days_to_expiry REAL, lot_size REAL
);
CREATE TABLE IF NOT EXISTS orders(
    order_id TEXT PRIMARY KEY,
    trade_id TEXT, leg_key TEXT, instrument_key TEXT,
    side TEXT, qty INTEGER, filled_qty INTEGER, avg_price REAL,
    status TEXT, placed_ts TEXT, updated_ts TEXT
);
CREATE TABLE IF NOT EXISTS trades(
    trade_id TEXT PRIMARY KEY,
    strategy_name TEXT, regime_at_entry TEXT, regime_at_exit TEXT,
    entry_ts TEXT, exit_ts TEXT, holding_days REAL,
    entry_spot REAL, exit_spot REAL, entry_vix REAL, exit_vix REAL,
    legs_summary TEXT, total_credit_received REAL, total_debit_paid REAL,
    net_premium REAL, max_risk REAL, realized_pnl REAL, realized_pnl_percent REAL,
    exit_reason TEXT, slippage_total_points REAL, transaction_costs REAL,
    composite_score_at_entry REAL, vol_score REAL, edge_score REAL,
    trend_score REAL, flow_score REAL,
    days_to_expiry_at_entry REAL, expiry_date TEXT, paper_trade INTEGER
);
"""


def _now_iso():
    return datetime.now().isoformat()


class StateStore:
    """Thread-safe SQLite store. All writes go through a lock; WAL journal."""

    def __init__(self, path=STATE_DB):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ kv
    def set_kv(self, key, value):
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value) if not isinstance(value, str) else value))
            self.conn.commit()

    def get_kv(self, key, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return row["value"]

    # ------------------------------------------------------------ checkpoints
    def checkpoint(self, snapshot: dict, reason: str = "heartbeat"):
        """snapshot keys: spot, vix, scores {vol,edge,trend,flow}, composite,
        regime (confirmed), override, cycle_ts"""
        with self._lock:
            for k, v in snapshot.items():
                if isinstance(v, (dict, list)):
                    v = json.dumps(v)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"cp:{k}", v))
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("cp:last_reason", reason))
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("cp:heartbeat_ts", _now_iso()))
            self.conn.commit()

    def heartbeat_age_seconds(self) -> float | None:
        ts = self.get_kv("cp:heartbeat_ts")
        if not ts:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            return None

    # --------------------------------------------------------- regime history
    def record_regime(self, row: dict):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO regime_history VALUES("
                ":ts,:composite,:regime,:vol,:edge,:trend,:flow,:spot,:vix,"
                ":override,:confirmed_regime)", row)
            self.conn.commit()

    def last_regime(self):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM regime_history ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------- skew history
    def record_skew(self, skew, day_iso):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO skew_history(date,skew) VALUES(?,?)",
                (day_iso, float(skew)))
            # keep last 90 days
            self.conn.execute(
                "DELETE FROM skew_history WHERE date NOT IN "
                "(SELECT date FROM skew_history ORDER BY date DESC LIMIT 90)")
            self.conn.commit()

    def skew_series(self, exclude_day=None, limit=30):
        with self._lock:
            rows = self.conn.execute(
                "SELECT date,skew FROM skew_history "
                "WHERE date != ? ORDER BY date DESC LIMIT ?",
                (exclude_day or "", limit)).fetchall()
        return [float(r["skew"]) for r in rows][::-1]

    # ---------------------------------------------------------- flow snapshots
    def add_snapshot(self, ts, payload: dict):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO snapshots(ts,payload) VALUES(?,?)",
                (ts, json.dumps(payload)))
            self.conn.execute(
                "DELETE FROM snapshots WHERE ts NOT IN "
                "(SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 60)")
            self.conn.commit()

    def snapshots_since(self, ts_iso):
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,payload FROM snapshots WHERE ts>=? ORDER BY ts",
                (ts_iso,)).fetchall()
        return [(r["ts"], json.loads(r["payload"])) for r in rows]

    # ------------------------------------------------- persistence buffers
    def get_buffers(self):
        with self._lock:
            rows = self.conn.execute("SELECT module,val FROM buffers").fetchall()
        return {r["module"]: json.loads(r["val"]) for r in rows}

    def set_buffers(self, buffers: dict):
        with self._lock:
            for m, v in buffers.items():
                self.conn.execute(
                    "INSERT OR REPLACE INTO buffers(module,val) VALUES(?,?)",
                    (m, json.dumps(v[-3:])))
            self.conn.commit()

    def get_confirmed(self):
        with self._lock:
            rows = self.conn.execute("SELECT module,value FROM confirmed").fetchall()
        return {r["module"]: int(r["value"]) for r in rows}

    def set_confirmed(self, confirmed: dict):
        with self._lock:
            for m, v in confirmed.items():
                self.conn.execute(
                    "INSERT OR REPLACE INTO confirmed(module,value) VALUES(?,?)",
                    (m, int(v)))
            self.conn.commit()

    # ------------------------------------------------------------- positions
    def upsert_position(self, pos: dict):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO positions VALUES("
                ":trade_id,:strategy_name,:regime_at_entry,:status,:legs_json,"
                ":meta_json,:entry_ts,:entry_spot,:entry_vix,:expiry_date,"
                ":days_to_expiry,:lot_size)", pos)
            self.conn.commit()

    def get_open_positions(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status IN ('OPEN','HEDGED','REDUCED') "
                "ORDER BY entry_ts").fetchall()
        return [dict(r) for r in rows]

    def get_position(self, trade_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM positions WHERE trade_id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- orders
    def upsert_order(self, o: dict):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO orders VALUES("
                ":order_id,:trade_id,:leg_key,:instrument_key,:side,:qty,"
                ":filled_qty,:avg_price,:status,:placed_ts,:updated_ts)", o)
            self.conn.commit()

    def get_orders(self, trade_id):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE trade_id=? ORDER BY placed_ts",
                (trade_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- trades
    def insert_trade(self, t: dict):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO trades VALUES("
                ":trade_id,:strategy_name,:regime_at_entry,:regime_at_exit,"
                ":entry_ts,:exit_ts,:holding_days,:entry_spot,:exit_spot,"
                ":entry_vix,:exit_vix,:legs_summary,:total_credit_received,"
                ":total_debit_paid,:net_premium,:max_risk,:realized_pnl,"
                ":realized_pnl_percent,:exit_reason,:slippage_total_points,"
                ":transaction_costs,:composite_score_at_entry,:vol_score,"
                ":edge_score,:trend_score,:flow_score,:days_to_expiry_at_entry,"
                ":expiry_date,:paper_trade)", t)
            self.conn.commit()

    # ------------------------------------------------------------- cleanup
    def close(self):
        with self._lock:
            try:
                self.conn.commit()
                self.conn.close()
            except sqlite3.Error:
                pass
