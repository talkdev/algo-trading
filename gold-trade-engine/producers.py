"""
Embedded producers — run monitor-gold + commodity-events inside main.py.

Trading candles/quotes still come from the engine Upstox client.
- monitor-gold: context snapshot JSON + contract health (background thread)
- commodity-events: news scrape + AI → gold_signals.db (background thread)

Neither producer places orders. Failures degrade producer health; they do not
crash the trading loop.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import random
import sys
import threading
import time
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from config import ENGINE_DIR, ENV_FILE, ROOT_DIR

UTC = timezone.utc
log = logging.getLogger("gold_news.producers")

MONITOR_JSON = ENGINE_DIR / "monitor_snapshot.json"
NEWS_DB_DEFAULT = ENGINE_DIR / "gold_signals.db"


def _load_hyphen_module(filename: str, mod_name: str) -> ModuleType:
    path = ENGINE_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"producer module missing: {path}")
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses look up sys.modules[cls.__module__] during class body exec —
    # must register before exec_module or InstrumentCache/@dataclass crashes.
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return mod


def _obs_to_dict(obs: Any) -> dict[str, Any]:
    if is_dataclass(obs):
        return asdict(obs)
    return {
        "name": getattr(obs, "name", ""),
        "value": getattr(obs, "value", ""),
        "change": getattr(obs, "change", ""),
        "source": getattr(obs, "source", ""),
        "source_ts": getattr(obs, "source_ts", ""),
        "error": getattr(obs, "error", ""),
        "numeric": getattr(obs, "numeric", None),
        "kind": getattr(obs, "kind", ""),
        "age_seconds": getattr(obs, "age_seconds", None),
        "valid": getattr(obs, "valid", False),
        "reject_reason": getattr(obs, "reject_reason", ""),
        "meta": dict(getattr(obs, "meta", {}) or {}),
    }


def snapshot_to_public_dict(snap: Any) -> dict[str, Any]:
    return {
        "ts_utc": getattr(snap, "ts_utc", ""),
        "observations": [_obs_to_dict(o) for o in (getattr(snap, "observations", None) or [])],
        "notes": list(getattr(snap, "notes", None) or []),
    }


def prefer_mcx_gold_symbol(monitor_mod: ModuleType, cache: Any, routing: Any, symbol: str) -> Any:
    """Prefer GOLDM (etc.) over generic GOLD front-month when resolving monitor routes."""
    sym = str(symbol or "GOLDM").upper().strip()
    if not sym:
        return routing
    chain = cache.find_futures(sym, segment="MCX_FO", instrument_type="FUT", max_n=3)
    if not chain:
        return routing
    for route in routing.routes:
        if route.name == "MCX_GOLD":
            route.upstox_chain = chain
            exp = monitor_mod._fmt_expiry_ms(chain[0].get("expiry"))
            tsym = chain[0].get("trading_symbol", "?")
            routing.notes.append(
                f"MCX_GOLD preferred {sym}: front={tsym} exp={exp} key={chain[0].get('instrument_key')}"
            )
            break
    return routing


def mcx_instrument_key_from_snap(snap: Any) -> str:
    for obs in getattr(snap, "observations", None) or []:
        name = str(getattr(obs, "name", "") or "")
        if name.upper() in ("MCX_GOLD", "MCX_GOLDM"):
            meta = getattr(obs, "meta", None) or {}
            key = str(meta.get("instrument_key") or "")
            if key:
                return key
            src = str(getattr(obs, "source", "") or "")
            if src.startswith("upstox:"):
                return src.split(":", 1)[1]
    return ""


class ProducerHub:
    """Background monitor + news producers owned by the trading process."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._monitor_mod: Optional[ModuleType] = None
        self._news_mod: Optional[ModuleType] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._news_thread: Optional[threading.Thread] = None
        self._latest_snap: Any = None
        self._latest_snap_dict: Optional[dict] = None
        self._monitor_error: str = ""
        self._news_error: str = ""
        self._monitor_cycles: int = 0
        self._news_cycles: int = 0
        self._monitor_last_ok: Optional[str] = None
        self._news_last_ok: Optional[str] = None
        self._started = False
        self.engine_instrument_key: str = ""

    @property
    def started(self) -> bool:
        return self._started

    def latest_monitor_snap(self) -> Any:
        with self._lock:
            return self._latest_snap

    def status(self) -> dict[str, Any]:
        with self._lock:
            key = mcx_instrument_key_from_snap(self._latest_snap) if self._latest_snap else ""
            mismatch = bool(
                self.engine_instrument_key
                and key
                and key != self.engine_instrument_key
            )
            return {
                "started": self._started,
                "monitor_cycles": self._monitor_cycles,
                "news_cycles": self._news_cycles,
                "monitor_error": self._monitor_error,
                "news_error": self._news_error,
                "monitor_last_ok": self._monitor_last_ok,
                "news_last_ok": self._news_last_ok,
                "monitor_mcx_key": key,
                "engine_key": self.engine_instrument_key,
                "contract_mismatch": mismatch,
                "monitor_json": str(MONITOR_JSON),
            }

    def start(
        self,
        *,
        access_token: str,
        gold_symbol: str = "GOLDM",
        instrument_key: str = "",
        news_db: str | Path | None = None,
        monitor_refresh_seconds: float = 60.0,
        news_interval_seconds: float = 60.0,
        enable_monitor: bool = True,
        enable_news: bool = True,
        news_no_telegram: bool = False,
        news_headed: bool = False,
    ) -> None:
        if self._started:
            return
        self.engine_instrument_key = str(instrument_key or "")
        self._stop.clear()
        self._started = True

        if enable_monitor:
            if not access_token:
                self._monitor_error = "UPSTOX_ACCESS_TOKEN missing — monitor disabled"
                log.warning(self._monitor_error)
            else:
                self._monitor_thread = threading.Thread(
                    target=self._monitor_loop,
                    name="producer-monitor",
                    kwargs={
                        "access_token": access_token,
                        "gold_symbol": gold_symbol,
                        "refresh_s": float(monitor_refresh_seconds),
                    },
                    daemon=True,
                )
                self._monitor_thread.start()
                log.info(
                    "Embedded monitor-gold started (refresh=%.0fs) → %s",
                    monitor_refresh_seconds, MONITOR_JSON,
                )

        if enable_news:
            self._news_thread = threading.Thread(
                target=self._news_loop,
                name="producer-news",
                kwargs={
                    "db_path": str(news_db or NEWS_DB_DEFAULT),
                    "interval_s": float(news_interval_seconds),
                    "no_telegram": bool(news_no_telegram),
                    "headed": bool(news_headed),
                },
                daemon=True,
            )
            self._news_thread.start()
            log.info(
                "Embedded commodity-events started (interval~%.0fs) → %s",
                news_interval_seconds, news_db or NEWS_DB_DEFAULT,
            )

    def request_stop(self) -> None:
        """Signal producers to wind down (safe to call before join / from SIGINT)."""
        self._stop.set()

    def stop(self, timeout: float = 20.0) -> None:
        self.request_stop()
        if not self._started:
            return
        for th in (self._monitor_thread, self._news_thread):
            if th is not None and th.is_alive():
                th.join(timeout=timeout)
        self._started = False
        log.info("Embedded producers stopped")

    def _monitor_loop(self, *, access_token: str, gold_symbol: str, refresh_s: float) -> None:
        try:
            mon = _load_hyphen_module("monitor-gold.py", "gold_monitor_embedded")
            self._monitor_mod = mon
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._monitor_error = f"import_failed:{exc}"
            log.exception("monitor-gold import failed")
            return

        try:
            # Prefer repo-root env if monitor's local env is empty of token
            cache = mon.InstrumentCache()
            cache.load()
            routing = mon.resolve(cache)
            routing = prefer_mcx_gold_symbol(mon, cache, routing, gold_symbol)
            client = mon.UpstoxClient(access_token)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._monitor_error = f"init_failed:{exc}"
            log.exception("monitor-gold init failed")
            return

        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                snap = mon.build_snapshot(client, routing)
                public = snapshot_to_public_dict(snap)
                MONITOR_JSON.write_text(json.dumps(public, indent=2), encoding="utf-8")
                with self._lock:
                    self._latest_snap = snap
                    self._latest_snap_dict = public
                    self._monitor_cycles += 1
                    self._monitor_last_ok = datetime.now(tz=UTC).isoformat()
                    self._monitor_error = ""
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._monitor_error = f"{type(exc).__name__}:{exc}"
                log.warning("monitor cycle failed: %s", exc)

            elapsed = time.monotonic() - t0
            wait = max(1.0, float(refresh_s) - elapsed)
            self._stop.wait(wait)

    def _news_loop(
        self,
        *,
        db_path: str,
        interval_s: float,
        no_telegram: bool,
        headed: bool,
    ) -> None:
        try:
            ce = _load_hyphen_module("commodity-events.py", "gold_news_embedded")
            self._news_mod = ce
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._news_error = f"import_failed:{exc}"
            log.exception("commodity-events import failed")
            return

        store = scraper = worker = None
        stop = self._stop
        wake = threading.Event()
        try:
            # Do NOT call ce.setup_logging here — it steals the gold logger,
            # duplicates console lines, and undoes main's quiet filters.

            env_path = str(ENV_FILE if ENV_FILE.exists() else ROOT_DIR / "env.txt")
            try:
                env = ce.load_env(env_path)
            except FileNotFoundError as exc:
                with self._lock:
                    self._news_error = str(exc)
                log.error("news producer: %s", exc)
                return

            api_key = env.get("NVIDIA_API_KEY") or ""
            if not api_key:
                with self._lock:
                    self._news_error = "NVIDIA_API_KEY missing — news producer idle"
                log.warning(self._news_error)
                while not stop.is_set():
                    stop.wait(30.0)
                return

            telegram = None
            if not no_telegram:
                token, chat = env.get("TELEGRAM_STOCK_BOT_TOKEN"), env.get("TELEGRAM_STOCK_CHAT_ID")
                if token and chat:
                    telegram = ce.Telegram(token, chat)

            store = ce.Store(db_path)
            router = ce.LLMRouter(api_key, ce.MODELS, stop)
            worker = ce.Worker(store, router, telegram, stop, wake, alert_all=False)
            worker.start()
            wake.set()
            scraper = ce.Scraper(headed=headed, stop=stop)
            state: dict = {}
            cycle = 0

            while not stop.is_set():
                cycle += 1
                t0 = time.time()
                try:
                    ce.run_cycle(cycle, scraper, store, wake, state)
                    with self._lock:
                        self._news_cycles += 1
                        self._news_last_ok = datetime.now(tz=UTC).isoformat()
                        self._news_error = ""
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._news_error = f"{type(exc).__name__}:{exc}"
                    log.warning("news cycle %d failed: %s", cycle, exc)
                    log.debug("%s", traceback.format_exc())

                if stop.is_set():
                    break
                jitter = getattr(ce, "POLL_JITTER_S", (0.75, 1.6))
                target = float(interval_s) * random.uniform(*jitter)
                wait = max(5.0, target - (time.time() - t0))
                stop.wait(wait)
        finally:
            wake.set()
            if worker is not None:
                try:
                    worker.join(timeout=float(getattr(ce, "REQUEST_TIMEOUT_S", 90)) + 5)
                except Exception:  # noqa: BLE001
                    pass
            if scraper is not None:
                try:
                    scraper.close()
                except Exception:  # noqa: BLE001
                    pass
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    pass


# Process-wide hub used by main.py
HUB = ProducerHub()
