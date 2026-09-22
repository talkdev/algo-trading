#!/usr/bin/env python3
"""v8: Telegram updates for the NIFTY intraday options engine.

Five things are worth a phone notification, and this module sends exactly
those five:

  1. the engine STARTED - manually from a terminal or automatically under a
     supervisor, and the message says which,
  2. a HEARTBEAT every TELEGRAM_HEARTBEAT_MIN minutes (default 15) for as
     long as the process runs, market open or not,
  3. a trade order was PLACED,
  4. a trade order was CLOSED,
  5. the engine STOPPED, and why: end of day, Ctrl+C, SIGTERM, a fatal
     error, or a start that never got off the ground.

Rules this module is built to obey, in the order they bite:

  * It cannot hurt trading. Every public method is fenced: nothing raises
    into the caller, a dead network costs one warning in the log, and the
    trading loop never waits on Telegram - it enqueues and moves on.
  * Telegram allows roughly 20 messages a minute into one group chat. So
    sends are spaced by TELEGRAM_MIN_GAP_SEC (default 3.5s), a 429 is
    obeyed via its retry_after, and when the bounded queue overflows the
    oldest HEARTBEAT is dropped first - a trade event is never thrown away
    to make room for a status update.
  * The numbers come from the persisted book and the engine's own state,
    never from a parallel calculation. The trade block is the v7 console
    block rendered by the same TradeConsoleReporter, so Telegram and the
    console cannot tell two different stories about the same trade.
  * HTML parse mode, escaped, chunked at Telegram's 4096-character limit on
    block boundaries so a <pre> section is never cut in half.
  * Disabled quietly when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are absent:
    one log line at startup, zero cost per cycle.

Configuration (env.txt; the bot token and chat id are the v6 alert ones):

    TELEGRAM_BOT_TOKEN            = "123456:ABC-..."
    TELEGRAM_CHAT_ID              = "-1001234567890"
    TELEGRAM_REPORT_CHAT_ID       = ""        # optional: reports elsewhere
    TELEGRAM_UPDATES_ENABLED      = true
    TELEGRAM_HEARTBEAT_MIN        = 15
    TELEGRAM_MIN_GAP_SEC          = 3.5
    TELEGRAM_TIMEOUT_SEC          = 8
    TELEGRAM_MAX_QUEUE            = 60
    TELEGRAM_PARSE_MODE           = HTML      # HTML | MARKDOWN | "" (plain)
    TELEGRAM_TRADE_BLOCK_STYLE    = console   # console | compact

Self-test (no network, deterministic transport and clock):

    python3 telegram_reporter.py --test
"""
import collections
import os
import sys
import threading
import time
from datetime import datetime, date, time as dtime
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import requests
except Exception:                                   # pragma: no cover
    requests = None                                 # type: ignore

from core import (
    Config, Database, TradeConsoleReporter,
    load_config, setup_logging, print_section,
    now_ist, today_ist, parse_ist_timestamp,
    # v7's own formatters, imported rather than reimplemented so a Telegram
    # message and the console block cannot drift apart in how they round,
    # sign or label the same number.
    TRADE_REPORT_RULE, TRADE_REPORT_SUB,
    _tr_num, _tr_money, _tr_signed, _tr_price, _tr_strike,
    _tr_side, _tr_closing_side, _tr_symbol, _tr_hhmm,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram rejects anything longer than this per message.
MAX_MESSAGE_CHARS = 4096
# Room for the "(part 2/3)" marker and the tags around a <pre> section.
_CHUNK_RESERVE = 48

DEFAULT_HEARTBEAT_MIN = 15.0
DEFAULT_MIN_GAP_SEC = 3.5
DEFAULT_TIMEOUT_SEC = 8.0
DEFAULT_MAX_QUEUE = 60
# Width of the compact trade block. A phone in Telegram's monospace face
# fits roughly 40-48 characters before it wraps a line of its own accord.
_COMPACT_WIDTH = 48
# Telegram answers a flooded bot with 429 + retry_after; obey it, but never
# park the sender thread for longer than this.
RETRY_AFTER_CAP_SEC = 60.0
MAX_SEND_ATTEMPTS = 3

KIND_START       = "START"
KIND_STOP        = "STOP"
KIND_HEARTBEAT   = "HEARTBEAT"
KIND_TRADE_OPEN  = "TRADE_OPEN"
KIND_TRADE_CLOSE = "TRADE_CLOSE"
KIND_INFO        = "INFO"

# Lower is more worth keeping when the queue is full. Heartbeats are the only
# messages that repeat themselves by design, so they are the ones sacrificed.
_PRIORITY = {
    KIND_TRADE_OPEN:  0,
    KIND_TRADE_CLOSE: 0,
    KIND_START:       1,
    KIND_STOP:        1,
    KIND_INFO:        2,
    KIND_HEARTBEAT:   3,
}

_TITLE = {
    KIND_START:       "started",
    KIND_STOP:        "stopped",
    KIND_HEARTBEAT:   "update",
    KIND_TRADE_OPEN:  "trade order placed",
    KIND_TRADE_CLOSE: "trade order closed",
    KIND_INFO:        "update",
}


# ─────────────────────────────────────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: Any) -> str:
    """HTML-escape dynamic text. Telegram returns 400 on a stray '<'."""
    return (
        str("" if text is None else text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _b(text: Any) -> str:
    return f"<b>{_esc(text)}</b>"


def _opt(value: Any, suffix: str = "") -> str:
    """'12.40' or 'n/a' - never a bare None on an operator's phone."""
    if value is None or value == "":
        return "n/a"
    return f"{value}{suffix}"


def _num_opt(value: Any, nd: int = 2) -> str:
    try:
        if value is None or value == "":
            return "n/a"
        return f"{float(value):,.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct_opt(value: Any, nd: int = 2, sign: bool = True) -> str:
    try:
        if value is None or value == "":
            return "n/a"
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{v:+.{nd}f}%" if sign else f"{v:.{nd}f}%"


def _hhmmss(value: Any) -> str:
    """Anything time-shaped -> 'HH:MM:SS'."""
    if value is None or value == "":
        return "n/a"
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    if isinstance(value, dtime):
        return value.strftime("%H:%M:%S")
    parsed = parse_ist_timestamp(value)
    if parsed is not None:
        return parsed.strftime("%H:%M:%S")
    return str(value)


def _uptime(seconds: Any) -> str:
    try:
        s = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        return "n/a"
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def start_mode() -> str:
    """Manual (a human at a terminal) or auto (supervisor, cron, nohup).

    stdin being a tty is the only honest signal available without reading
    /proc, and it is the one that matters to the operator: it says whether
    somebody typed `python main.py` or whether a service manager did.
    """
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return "manual (interactive terminal)"
    except Exception:
        pass
    return "auto (no terminal - supervised or backgrounded)"


def _first(*values: Any) -> Any:
    """First value that is present and not zero-ish-empty."""
    for v in values:
        if v is None or v == "":
            continue
        return v
    return None


def _regime_line(vol: Any, price: Any, positioning: Any, final: Any) -> str:
    parts = []
    if vol:
        parts.append(f"vol {vol}")
    if price:
        parts.append(f"price {price}")
    if positioning:
        parts.append(f"positioning {positioning}")
    if final:
        parts.append(f"final {final}")
    return " | ".join(parts) if parts else "n/a"


# Telegram's parse_mode is case-sensitive; an env.txt entry need not be.
_PARSE_MODES = {
    "":           "",
    "none":       "",
    "plain":      "",
    "text":       "",
    "html":       "HTML",
    "markdown":   "Markdown",
    "markdownv2": "MarkdownV2",
}


def _canonical_parse_mode(value: Any) -> str:
    """'html' -> 'HTML', 'plain' -> '', and an unknown word -> 'HTML'.

    Falling back to HTML rather than to the unknown word matters: every
    dynamic string this module sends is escaped for HTML, so a mode it does
    not escape for would turn a premium like '23100.5_2' into a 400.
    """
    key = str(value if value is not None else "HTML").strip().lower()
    return _PARSE_MODES.get(key, "HTML")


# ─────────────────────────────────────────────────────────────────────────────
# REPORTER
# ─────────────────────────────────────────────────────────────────────────────

class TelegramReporter:
    """Sends the five lifecycle updates, and nothing that can break a cycle.

    Construction is cheap and side-effect free apart from starting the sender
    thread when a channel is configured. `close()` flushes and joins; the
    thread is a daemon, so a hard kill cannot hang the process on it.
    """

    def __init__(
        self,
        db: Optional[Database],
        config: Config,
        logger=None,
        console: Optional[TradeConsoleReporter] = None,
        transport: Optional[Callable[[dict], Tuple[bool, Optional[float], str]]] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        worker: Optional[bool] = None,
        source: str = "TRADE ENGINE",
    ):
        self.db     = db
        self.config = config
        self.logger = logger
        self.source = str(source or "TRADE ENGINE")

        # The v7 console reporter renders the trade block. Sharing the
        # engine's instance keeps Trade-<n> numbering identical on the
        # console and on the phone.
        try:
            self.console = console or (
                TradeConsoleReporter(db, config, logger, source=self.source)
                if db is not None else None
            )
        except Exception:
            self.console = None

        self.token   = str(getattr(config, "alert_telegram_bot_token", "") or "").strip()
        self.chat_id = str(
            _first(getattr(config, "telegram_report_chat_id", ""),
                   getattr(config, "alert_telegram_chat_id", "")) or ""
        ).strip()
        self.webhook = str(getattr(config, "alert_webhook_url", "") or "").strip()

        self.updates_enabled = bool(getattr(config, "telegram_updates_enabled", True))
        heartbeat_min = self._bounded(
            getattr(config, "telegram_heartbeat_min", DEFAULT_HEARTBEAT_MIN),
            DEFAULT_HEARTBEAT_MIN, 0.0,
        )
        if heartbeat_min <= 0:
            # 0 would mean a heartbeat on every call, which is not a heartbeat
            # but a flood; load_config() clamps env.txt to >= 1 minute and a
            # non-positive Config value falls back to the documented default.
            heartbeat_min = DEFAULT_HEARTBEAT_MIN
        self.heartbeat_sec = heartbeat_min * 60.0
        self.min_gap = self._bounded(
            getattr(config, "telegram_min_gap_sec", DEFAULT_MIN_GAP_SEC),
            DEFAULT_MIN_GAP_SEC, 0.0,
        )
        self.timeout = self._bounded(
            getattr(config, "telegram_timeout_sec", DEFAULT_TIMEOUT_SEC),
            DEFAULT_TIMEOUT_SEC, 1.0,
        )
        self.max_queue = int(self._bounded(
            getattr(config, "telegram_max_queue", DEFAULT_MAX_QUEUE),
            DEFAULT_MAX_QUEUE, 4.0,
        ))
        self.parse_mode = _canonical_parse_mode(
            getattr(config, "telegram_parse_mode", "HTML")
        )
        if self.parse_mode not in ("", "HTML"):
            # This reporter escapes for HTML. Markdown and MarkdownV2 have
            # their own, much larger escape sets, and getting one wrong is a
            # 400 from Telegram and a lost update - so anything that is not
            # HTML is sent as plain text instead of being half-escaped.
            self._log(
                "info",
                f"Telegram parse_mode {self.parse_mode!r} is not supported "
                f"here (this reporter escapes for HTML); sending plain text",
            )
            self.parse_mode = ""
        self.block_style = str(
            getattr(config, "telegram_trade_block_style", "console") or "console"
        ).strip().lower()
        if self.block_style not in ("console", "compact"):
            self.block_style = "console"
        self.include_open_blocks = bool(
            getattr(config, "telegram_include_open_blocks", True)
        )

        # Injectable for the self-test: the transport is the only thing that
        # touches the network, and the clock/sleep pair makes throttling and
        # the heartbeat schedule testable without waiting for either.
        self.transport = transport or self._http_transport
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep

        self._items: collections.deque = collections.deque()
        self._cond = threading.Condition(threading.Lock())
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_send_mono = 0.0
        self._last_heartbeat_mono = 0.0
        self._started_sent = False
        self._stopped_sent = False
        self._day: Optional[str] = None
        self._seen: Dict[str, str] = {}
        self.stats: Dict[str, int] = {
            "queued": 0, "sent": 0, "failed": 0, "dropped": 0,
            "retries": 0, "throttled": 0,
        }
        self.last_error: str = ""

        self._worker_wanted = (
            self.enabled if worker is None else bool(worker)
        )
        if self._worker_wanted:
            self._start_worker()

    # ── enablement ────────────────────────────────────────────────────

    @staticmethod
    def _bounded(value: Any, default: float, floor: float) -> float:
        """float(value) clamped to a floor, or the default if it is not a
        number. Zero is a legal value wherever the floor allows it: a
        min_gap of 0 means 'do not space the sends', which is what a test
        and a low-volume private chat both want."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = float(default)
        if v != v:                      # NaN
            v = float(default)
        return max(float(floor), v)

    @property
    def enabled(self) -> bool:
        """A channel is configured and updates were not switched off."""
        return bool(self.updates_enabled and self.token and self.chat_id)

    @property
    def masked_token(self) -> str:
        if not self.token:
            return "(unset)"
        head = self.token.split(":", 1)[0]
        return f"{head[:6]}:...{self.token[-3:]}" if len(self.token) > 12 else "***"

    def describe(self) -> str:
        """One log line at startup, so silence is never a mystery."""
        if not self.updates_enabled:
            return "Telegram updates DISABLED (TELEGRAM_UPDATES_ENABLED=false)"
        if not (self.token and self.chat_id):
            return (
                "Telegram updates DISABLED (set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in env.txt)"
            )
        return (
            f"Telegram updates ON -> chat {self.chat_id}, bot "
            f"{self.masked_token}, heartbeat every "
            f"{self.heartbeat_sec / 60.0:.0f} min, block style "
            f"{self.block_style}"
        )

    def _log(self, level: str, message: str) -> None:
        try:
            if self.logger is None:
                return
            getattr(self.logger, level, self.logger.info)(message)
        except Exception:
            pass

    def _delivery_word(self, ok: bool) -> str:
        """What a True from send() actually means, in log wording.

        With the daemon thread running it means the message is on the queue
        and the trading loop is free again - delivery happens later, and a
        failure is logged then ("... update not delivered: <reason>"). Saying
        "sent" at enqueue time would tell the operator a message arrived when
        all that happened is that it left the caller's hands. Inline (no
        worker - the self-test, --send-test) True really does mean delivered.
        """
        if not ok:
            return "not delivered"
        return "queued for delivery" if self._worker_wanted else "sent"

    # ── transport ─────────────────────────────────────────────────────

    def _http_transport(self, payload: dict) -> Tuple[bool, Optional[float], str]:
        """POST one message. Returns (ok, retry_after_seconds, error)."""
        if requests is None:
            return False, None, "the requests library is not installed"
        url = TELEGRAM_API.format(token=self.token, method="sendMessage")
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except Exception as exc:
            return False, None, f"{exc.__class__.__name__}: {exc}"
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code == 429:
            retry_after = None
            try:
                retry_after = float(
                    (body or {}).get("parameters", {}).get("retry_after") or 0
                ) or None
            except Exception:
                retry_after = None
            return False, retry_after, f"HTTP 429 rate limited by Telegram"
        ok = bool(resp.ok and str((body or {}).get("ok")).lower() in ("true", "1"))
        if not ok:
            desc = (body or {}).get("description") or resp.text[:160]
            return False, None, f"HTTP {resp.status_code}: {desc}"
        return True, None, ""

    # ── queue and sender thread ───────────────────────────────────────

    def _start_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="telegram-reporter",
            daemon=True,
        )
        try:
            self._thread.start()
        except Exception as exc:
            # No thread, no async delivery: fall back to inline sends rather
            # than silently dropping every update.
            self._thread = None
            self._worker_wanted = False
            self._log("warning", f"Telegram sender thread not started ({exc}); "
                                 f"sending inline instead")

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._stop_evt.is_set():
                    self._cond.wait(timeout=0.25)
                if not self._items:
                    if self._stop_evt.is_set():
                        return
                    continue
                kind, text = self._items.popleft()
            try:
                self._deliver(kind, text)
            except Exception as exc:                  # never die on a message
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                self._log("warning", f"Telegram delivery error: {self.last_error}")
            if self._stop_evt.is_set():
                with self._cond:
                    if not self._items:
                        return

    def send(self, text: str, kind: str = KIND_INFO) -> bool:
        """Queue (or, with no worker, deliver) one message. Never raises."""
        if not self.enabled:
            return False
        if not text or not str(text).strip():
            return False
        kind = str(kind or KIND_INFO).upper()
        try:
            if self._worker_wanted and not self._stop_evt.is_set():
                if self._thread is None or not self._thread.is_alive():
                    # Self-heal: a sender thread that is not running would
                    # otherwise turn every later update into a blocking send
                    # on the trading loop.
                    self._start_worker()
                if self._thread is not None and self._thread.is_alive():
                    self._enqueue(kind, str(text))
                    return True
            # v59: never sync-send on the trading thread (blocks cycle →
            # watchdog flatten). Drop + CRITICAL when the worker is down.
            self._log(
                "critical",
                "Telegram worker unavailable — dropping update "
                f"({kind}); not sending inline on trading thread",
            )
            self.stats["dropped"] = int(self.stats.get("dropped") or 0) + 1
            return False
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            self._log("warning", f"Telegram send failed: {self.last_error}")
            return False

    def _enqueue(self, kind: str, text: str) -> None:
        with self._cond:
            if len(self._items) >= self.max_queue:
                victim = None
                for idx, item in enumerate(self._items):
                    if _PRIORITY.get(item[0], 2) >= _PRIORITY[KIND_HEARTBEAT]:
                        victim = idx
                        break
                if victim is None:
                    victim = 0
                dropped = self._items[victim]
                del self._items[victim]
                self.stats["dropped"] += 1
                self._log(
                    "warning",
                    f"Telegram queue full ({self.max_queue}); dropped a "
                    f"{dropped[0]} update to make room for a {kind} update",
                )
            self._items.append((kind, text))
            self.stats["queued"] += 1
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._items)

    def close(self, timeout: float = 8.0) -> int:
        """Flush and stop the sender thread. Idempotent; never raises."""
        remaining = self.pending()
        try:
            if self._thread is not None and self._thread.is_alive():
                self._stop_evt.set()
                with self._cond:
                    self._cond.notify_all()
                self._thread.join(timeout=max(0.5, float(timeout)))
                if self._thread.is_alive():
                    self._log(
                        "warning",
                        f"Telegram sender thread did not finish within "
                        f"{timeout:.0f}s; {self.pending()} update(s) may be "
                        f"unsent",
                    )
        except Exception as exc:
            self._log("warning", f"Telegram close error: {exc}")
        finally:
            self._thread = None
        return remaining

    def _deliver(self, kind: str, text: str) -> bool:
        """Throttle, chunk and POST. Returns True if every part went out."""
        if not self.enabled:
            return False
        # Space the sends: Telegram's per-chat limit is about 20/minute, and
        # a burst of trade events must not cost the channel for the next
        # minute. The wait happens on the sender thread, never on the loop.
        if self.min_gap > 0:
            wait = self.min_gap - (self.clock() - self._last_send_mono)
            if wait > 0:
                self.stats["throttled"] += 1
                try:
                    self.sleep(wait)
                except Exception:
                    pass
        parts = self._pack(text)
        delivered = True
        for part in parts:
            delivered = self._post(part, kind) and delivered
        self._last_send_mono = self.clock()
        return delivered

    def _post(self, text: str, kind: str) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "disable_web_page_preview": True,
        }
        if self.parse_mode:
            payload["parse_mode"] = self.parse_mode
        attempt = 0
        while attempt < MAX_SEND_ATTEMPTS:
            attempt += 1
            try:
                ok, retry_after, err = self.transport(payload)
            except Exception as exc:
                ok, retry_after, err = False, None, f"{exc.__class__.__name__}: {exc}"
            if ok:
                self.stats["sent"] += 1
                return True
            self.last_error = err or "unknown transport failure"
            if retry_after and retry_after > 0:
                self.stats["retries"] += 1
                pause = min(float(retry_after), RETRY_AFTER_CAP_SEC)
                self._log(
                    "warning",
                    f"Telegram rate limit ({kind}); waiting {pause:.0f}s",
                )
                try:
                    self.sleep(pause)
                except Exception:
                    pass
                continue
            if attempt < MAX_SEND_ATTEMPTS:
                self.stats["retries"] += 1
                try:
                    self.sleep(min(2.0 * attempt, 5.0))
                except Exception:
                    pass
                continue
            break
        self.stats["failed"] += 1
        self._log("warning", f"Telegram {kind} update not delivered: {self.last_error}")
        return False

    # ── chunking ──────────────────────────────────────────────────────

    def _pack(self, text: str) -> List[str]:
        """Split a composed message on line boundaries, tags kept balanced.

        The composer marks monospace sections with a <pre> tag of its own, so
        a section is closed before a split and reopened after it: Telegram
        rejects unbalanced entities, and a half-cut block is unreadable.
        """
        limit = MAX_MESSAGE_CHARS - _CHUNK_RESERVE
        raw_parts = self._split_long(text, limit)
        if len(raw_parts) <= 1:
            return raw_parts
        return [
            f"<b>(part {idx}/{len(raw_parts)})</b>\n{part}"
            for idx, part in enumerate(raw_parts, start=1)
        ]

    def _split_long(self, text: str, limit: int) -> List[str]:
        out: List[str] = []
        cur: List[str] = []
        cur_len = 0
        in_pre = False
        for line in text.split("\n"):
            piece = line + "\n"
            if len(piece) > limit:
                # A single line longer than a whole message: hard-cut it.
                if cur:
                    out.append(self._close_pre("".join(cur), in_pre))
                    cur, cur_len, in_pre = [], 0, False
                for i in range(0, len(piece), limit):
                    out.append(piece[i:i + limit])
                continue
            if cur_len + len(piece) > limit and cur:
                out.append(self._close_pre("".join(cur), in_pre))
                cur, cur_len = [], 0
                if in_pre:
                    cur.append("<pre>")
                    cur_len += len("<pre>")
            cur.append(piece)
            cur_len += len(piece)
            if "<pre>" in line:
                in_pre = True
            if "</pre>" in line:
                in_pre = False
        if cur:
            out.append(self._close_pre("".join(cur), in_pre))
        return [p.rstrip("\n") for p in out if p.strip()]

    @staticmethod
    def _close_pre(text: str, in_pre: bool) -> str:
        return text + "\n</pre>" if in_pre else text

    # ── session snapshot ──────────────────────────────────────────────

    def _scalar(self, sql: str, params: tuple = (), default: Any = None) -> Any:
        try:
            if self.db is None:
                return default
            row = self.db.query_one(sql, params)
            if not row:
                return default
            return list(row.values())[0]
        except Exception:
            return default

    def _rows(self, sql: str, params: tuple = ()) -> List[dict]:
        try:
            if self.db is None:
                return []
            return list(self.db.query(sql, params) or [])
        except Exception:
            return []

    def session_snapshot(
        self,
        state: Optional[dict] = None,
        signals: Optional[dict] = None,
        cycles: Optional[int] = None,
        started_at: Any = None,
        start_mode: Optional[str] = None,
        realized_pnl: Optional[float] = None,
        unrealized_pnl: Optional[float] = None,
        total_pnl: Optional[float] = None,
        capital: Optional[float] = None,
        halted: Optional[bool] = None,
        entries: Optional[int] = None,
        consecutive_stops: Optional[int] = None,
        open_positions: Optional[int] = None,
        trading_date: Optional[str] = None,
        as_of: Any = None,
        reason: str = "",
        uptime_sec: Optional[float] = None,
        chain: Optional[dict] = None,
    ) -> dict:
        """Everything a message may say, in one dict.

        Values handed in by the engine win; anything missing is read back out
        of the book, so the same call works from a live loop (which has the
        in-memory signals) and from the self-test (which has only a
        database). Nothing here raises: an unavailable field is None and the
        composer prints 'n/a'.
        """
        state = state or {}
        signals = signals or {}
        as_of = as_of or now_ist()
        # The date follows the timestamp being described, not the wall clock:
        # a message composed at 00:05 for the session that just ended belongs
        # to that session, and every query below is keyed on it.
        if trading_date is None:
            trading_date = (
                as_of.date().isoformat() if isinstance(as_of, datetime)
                else today_ist().isoformat()
            )
        trading_date = str(trading_date)

        def _sig(*keys):
            for k in keys:
                v = signals.get(k)
                if v not in (None, ""):
                    return v
            return None

        def _st(*keys):
            for k in keys:
                v = state.get(k)
                if v not in (None, ""):
                    return v
            return None

        day_open = _first(
            _sig("day_open"),
            _st("day_open"),
            self._scalar(
                "SELECT open FROM intraday_candles WHERE trading_date=? "
                "ORDER BY candle_time ASC, candle_id ASC LIMIT 1",
                (trading_date,),
            ),
            self._scalar(
                "SELECT spot FROM cycle_log WHERE trading_date=? AND spot IS NOT NULL "
                "ORDER BY cycle_id ASC LIMIT 1", (trading_date,),
            ),
            self._scalar(
                "SELECT spot FROM market_snapshots WHERE date=? AND spot IS NOT NULL "
                "ORDER BY id ASC LIMIT 1", (trading_date,),
            ),
            self._scalar(
                "SELECT nifty_open FROM daily_summary WHERE trading_date=?",
                (trading_date,),
            ),
        )

        prev_close = _first(
            _sig("prev_close") or None,
            _st("prev_close"),
            self._scalar(
                "SELECT nifty_close FROM daily_summary WHERE trading_date<? "
                "AND nifty_close IS NOT NULL ORDER BY trading_date DESC LIMIT 1",
                (trading_date,),
            ),
            self._scalar(
                "SELECT spot FROM cycle_log WHERE trading_date<? AND spot IS NOT NULL "
                "ORDER BY trading_date DESC, cycle_id DESC LIMIT 1", (trading_date,),
            ),
        )
        if _tr_num(prev_close, 0.0) <= 0:
            prev_close = None

        # "Nifty closed at" is today's close once the session is over, and
        # yesterday's until then - labelled, because an unlabelled close on a
        # phone screen at 11:00 is a lie waiting to happen.
        today_close = _first(
            self._scalar(
                "SELECT nifty_close FROM daily_summary WHERE trading_date=?",
                (trading_date,),
            ),
            self._scalar(
                "SELECT spot FROM cycle_log WHERE trading_date=? AND spot IS NOT NULL "
                "ORDER BY cycle_id DESC LIMIT 1", (trading_date,),
            ),
        )
        session_over = False
        try:
            session_over = as_of.time() >= dtime(15, 30)
        except Exception:
            session_over = False
        day_close = today_close if session_over else None
        day_close_label = "today" if session_over else "previous day"
        closed_value = _first(day_close, prev_close)

        spot = _first(_sig("spot"), _st("prev_spot"), today_close)
        vix = _first(_sig("vix"), _st("prev_vix"))
        day_high = _first(
            _sig("day_high") or None,
            self._scalar(
                "SELECT MAX(spot) FROM cycle_log WHERE trading_date=? "
                "AND spot IS NOT NULL", (trading_date,),
            ),
        )
        day_low = _first(
            _sig("day_low") or None,
            self._scalar(
                "SELECT MIN(spot) FROM cycle_log WHERE trading_date=? "
                "AND spot IS NOT NULL", (trading_date,),
            ),
        )
        if _tr_num(day_high, 0.0) <= 0:
            day_high = None
        if _tr_num(day_low, 0.0) <= 0:
            day_low = None

        db_cycles = self._scalar(
            "SELECT COUNT(*) FROM cycle_log WHERE trading_date=?", (trading_date,), 0
        )
        last_cycle = self._rows(
            "SELECT vol_regime, price_regime, positioning_regime, final_regime, "
            "spot, vix FROM cycle_log WHERE trading_date=? "
            "ORDER BY cycle_id DESC LIMIT 1", (trading_date,),
        )
        last_cycle = last_cycle[0] if last_cycle else {}

        dominant, regime_cycles = self._dominant_regime(trading_date)

        db_realized = self._scalar(
            "SELECT SUM(net_pnl_rupees) FROM positions "
            "WHERE trading_date=? AND status='CLOSED'", (trading_date,), None,
        )
        closed_trades = self._scalar(
            "SELECT COUNT(*) FROM positions WHERE trading_date=? "
            "AND status='CLOSED'", (trading_date,), 0,
        )
        db_open = self._scalar(
            "SELECT COUNT(*) FROM positions WHERE trading_date=? "
            "AND status='OPEN'", (trading_date,), 0,
        )

        if uptime_sec is None and started_at is not None:
            try:
                started_dt = started_at if isinstance(started_at, datetime) \
                    else parse_ist_timestamp(started_at)
                if started_dt is not None:
                    uptime_sec = max(0.0, (as_of - started_dt).total_seconds())
            except Exception:
                uptime_sec = None

        spot_change_pct = None
        try:
            if spot is not None and prev_close:
                spot_change_pct = (
                    (float(spot) - float(prev_close)) / float(prev_close) * 100.0
                )
        except (TypeError, ValueError, ZeroDivisionError):
            spot_change_pct = None

        return {
            "as_of":             as_of,
            "trading_date":      trading_date,
            "start_mode":        start_mode,
            "started_at":        started_at,
            "uptime_sec":        uptime_sec,
            "reason":            reason or "",
            "cycles":            int(_first(cycles, db_cycles) or 0),
            "spot":              spot,
            "day_open":          day_open,
            "prev_close":        prev_close,
            "day_close":         day_close,
            "closed_value":      closed_value,
            "day_close_label":   day_close_label,
            "day_high":          day_high,
            "day_low":           day_low,
            "spot_change_pct":   spot_change_pct,
            "vix":               vix,
            "vol_regime":        _first(_sig("vol_regime"), last_cycle.get("vol_regime")),
            "price_regime":      _first(_sig("price_regime"), last_cycle.get("price_regime")),
            "positioning_regime": _first(
                _sig("positioning_regime"), last_cycle.get("positioning_regime")),
            "final_regime":      _first(_sig("final_regime"), last_cycle.get("final_regime")),
            "dominant_regime":   dominant,
            "regime_cycles":     regime_cycles,
            "realized_pnl":      _first(realized_pnl, _st("daily_pnl"), db_realized),
            "unrealized_pnl":    unrealized_pnl,
            "total_pnl":         _first(total_pnl, _st("total_pnl")),
            "capital":           _first(capital, _st("current_capital")),
            "halted":            _first(halted, _st("daily_halted")),
            "entries":           _first(entries, _st("entry_count")),
            "consecutive_stops": _first(consecutive_stops, _st("consecutive_stops")),
            "open_positions":    _first(open_positions, db_open),
            "closed_trades":     closed_trades or 0,
            "session_over":      session_over,
            "chain":             chain or {},
            "mode":              "PAPER TRADE" if getattr(
                                     self.config, "paper_trade_mode", True)
                                 else "LIVE TRADING",
            "lot_size":          getattr(self.config, "lot_size", None),
            "max_daily_loss_pct": getattr(self.config, "max_daily_loss_pct", None),
            "pid":               os.getpid(),
        }

    def _dominant_regime(self, trading_date: str) -> Tuple[Optional[str], int]:
        """The regime the day has spent most of its cycles in.

        Mirrors MainEngine._get_dominant_regime(): the verdicts that describe
        an action rather than a market (SIGNAL_ONLY, NO_TRADE) are not
        regimes and are left out of the count.
        """
        rows = self._rows(
            "SELECT final_regime, action_taken FROM cycle_log WHERE trading_date=?",
            (trading_date,),
        )
        counts: Dict[str, int] = {}
        for row in rows:
            regime = row.get("final_regime") or row.get("action_taken") or "UNKNOWN"
            if regime in ("SIGNAL_ONLY", "NO_TRADE", None, ""):
                continue
            counts[str(regime)] = counts.get(str(regime), 0) + 1
        if not counts:
            # Cycles ran but every one of them ended in a non-verdict: say
            # so rather than naming a regime the day never had.
            return ("NO_TRADE" if rows else None), 0
        return max(counts, key=lambda k: counts[k]), sum(counts.values())

    # ── message composition ───────────────────────────────────────────

    def _status_units(self, kind: str, snap: dict) -> List[Tuple[str, str]]:
        """(mode, text) units: 'html' is trusted markup, 'pre' is raw."""
        as_of = snap.get("as_of") or now_ist()
        when = _hhmmss(as_of)
        title = f"Nifty options trade engine {_TITLE.get(kind, 'update')} at {when} IST"
        head = [_b(title)]

        subtitle: List[str] = []
        if kind == KIND_HEARTBEAT:
            subtitle.append(f"{self.heartbeat_sec / 60.0:.0f}-minute heartbeat")
        if snap.get("reason"):
            subtitle.append(str(snap["reason"]))
        subtitle.append(str(snap.get("trading_date") or ""))
        subtitle.append(str(snap.get("mode") or ""))
        if kind == KIND_START:
            subtitle.append(str(snap.get("start_mode") or start_mode()))
            subtitle.append(f"pid {snap.get('pid')}")
        if kind == KIND_STOP:
            subtitle.append(f"uptime {_uptime(snap.get('uptime_sec'))}")
            subtitle.append(f"pid {snap.get('pid')}")
        head.append(_esc(" · ".join(p for p in subtitle if p)))
        head.append("")

        # The operator's own field list, in the operator's own order.
        head.append(
            _esc(f"Nifty opened at: {_num_opt(snap.get('day_open'))}")
        )
        spot_bits = [f"Current Nifty Spot at: {_num_opt(snap.get('spot'))}"]
        if snap.get("spot_change_pct") is not None:
            spot_bits.append(_pct_opt(snap["spot_change_pct"]) + " vs prev close")
        head.append(_esc("  ".join(spot_bits)))
        head.append(_esc(
            f"Nifty closed at: {_num_opt(snap.get('closed_value'))} "
            f"({snap.get('day_close_label') or 'previous day'})"
        ))
        head.append(_esc(
            f"Current nifty regime: {_regime_line(snap.get('vol_regime'), snap.get('price_regime'), snap.get('positioning_regime'), snap.get('final_regime'))}"
        ))
        dominant = snap.get("dominant_regime") or "n/a"
        regime_cycles = snap.get("regime_cycles") or 0
        head.append(_esc(
            f"Overall nifty regime: {dominant}"
            + (f" (dominant of {regime_cycles} cycle(s))" if regime_cycles else "")
        ))
        head.append(_esc(f"Total cycles completed: {snap.get('cycles', 0)}"))
        head.append("")

        money: List[str] = []
        if snap.get("realized_pnl") is not None:
            money.append(f"realised {_tr_signed(snap['realized_pnl'])}")
        if snap.get("unrealized_pnl") is not None:
            money.append(f"unrealised {_tr_signed(snap['unrealized_pnl'])}")
        if snap.get("total_pnl") is not None:
            money.append(f"total {_tr_signed(snap['total_pnl'])}")
        if money:
            head.append(_b("Day P&L") + _esc(": " + " · ".join(money)))

        book: List[str] = [
            f"open trades {snap.get('open_positions') or 0}",
            f"closed today {snap.get('closed_trades') or 0}",
        ]
        if snap.get("entries") is not None:
            book.append(f"entries {snap['entries']}")
        if snap.get("consecutive_stops") is not None:
            book.append(f"consecutive stops {snap['consecutive_stops']}")
        book.append(
            "daily halt ON" if snap.get("halted") else "daily halt OFF"
        )
        head.append(_esc(" · ".join(book)))

        market: List[str] = []
        if snap.get("vix") is not None:
            market.append(f"VIX {_num_opt(snap.get('vix'))}")
        if snap.get("day_high") is not None or snap.get("day_low") is not None:
            market.append(
                f"day range {_num_opt(snap.get('day_low'))} - "
                f"{_num_opt(snap.get('day_high'))}"
            )
        if snap.get("capital") is not None:
            market.append(f"capital {_tr_money(snap['capital'])}")
        if snap.get("lot_size"):
            market.append(f"lot size {snap['lot_size']}")
        if market:
            head.append(_esc(" · ".join(market)))

        units: List[Tuple[str, str]] = [("html", "\n".join(head))]
        return units

    def compose_status(self, kind: str, snap: dict) -> str:
        """A start / heartbeat / stop message, trade blocks included."""
        units = self._status_units(kind, snap)
        blocks = self._status_blocks(kind, snap)
        if blocks:
            units.append(("pre", "\n\n".join(blocks)))
        return self._compose(units)

    def _status_blocks(self, kind: str, snap: dict) -> List[str]:
        """Which trades belong in a status message.

        A heartbeat carries the trades still in progress (a closed one has
        already had its own message). A stop carries the whole day, in its
        final state - that is the record the operator keeps.
        """
        try:
            if kind == KIND_HEARTBEAT and not self.include_open_blocks:
                return []
            as_of_snap = snap.get("as_of")
            trading_date = str(
                snap.get("trading_date")
                or (as_of_snap.date().isoformat()
                    if isinstance(as_of_snap, datetime) else None)
                or today_ist().isoformat()
            )
            chain = snap.get("chain") or {}
            as_of = snap.get("as_of") or now_ist()
            rows = self._positions(trading_date)
            out: List[str] = []
            for index, position in enumerate(rows, start=1):
                closed = str(position.get("status") or "OPEN").upper().startswith("CLOSE")
                if kind == KIND_HEARTBEAT and closed:
                    continue
                legs = self._legs(position)
                lines = self.block_lines(index, position, legs, chain, as_of)
                if lines:
                    out.append("\n".join(lines))
            return out
        except Exception as exc:
            self._log("debug", f"Telegram status blocks skipped: {exc}")
            return []

    def compose_trade(self, kind: str, index: int, position: dict,
                      legs: List[dict], snap: dict) -> str:
        """A placed / closed message: headline, context, then the block."""
        as_of = snap.get("as_of") or now_ist()
        chain = snap.get("chain") or {}
        lines = self.block_lines(index, position, legs, chain, as_of)
        name = position.get("strategy_name") or "UNKNOWN"
        closed = str(position.get("status") or "OPEN").upper().startswith("CLOSE")
        when = _tr_hhmm(position.get("exit_time") if closed
                        else position.get("entry_time"))

        head: List[str] = []
        title = f"Trade order {'closed' if closed else 'placed'} - Trade-{index} {name} at {when}"
        head.append(_b(title))
        ctx: List[str] = []
        if closed and position.get("exit_reason"):
            ctx.append(f"reason {position['exit_reason']}")
        if snap.get("spot") is not None:
            ctx.append(f"spot {_num_opt(snap['spot'])}")
        if snap.get("final_regime"):
            ctx.append(f"regime {snap['final_regime']}")
        ctx.append(f"cycle {snap.get('cycles', 0)}")
        if snap.get("total_pnl") is not None:
            ctx.append(f"day P&L {_tr_signed(snap['total_pnl'])}")
        elif snap.get("realized_pnl") is not None:
            ctx.append(f"day realised {_tr_signed(snap['realized_pnl'])}")
        head.append(_esc(" · ".join(str(c) for c in ctx)))
        net = position.get("net_pnl_rupees")
        if closed and net not in (None, ""):
            head.append(_esc(
                f"Net {_tr_signed(net)} realised · charges "
                f"{_tr_money(_tr_num(position.get('entry_costs_rupees')) + _tr_num(position.get('exit_costs_rupees')))}"
            ))

        units: List[Tuple[str, str]] = [("html", "\n".join(head))]
        if lines:
            units.append(("pre", "\n".join(lines)))
        return self._compose(units)

    def _compose(self, units: List[Tuple[str, str]]) -> str:
        out: List[str] = []
        for mode, text in units:
            if not text:
                continue
            if mode == "pre":
                if self.parse_mode.upper() == "HTML":
                    out.append(f"<pre>{_esc(text)}</pre>")
                else:
                    out.append("```" + str(text) + "```")
            else:
                out.append(str(text))
        return "\n".join(out)

    # ── trade blocks ──────────────────────────────────────────────────

    def _positions(self, trading_date: str) -> List[dict]:
        if self.console is not None:
            return self.console.positions_for(trading_date)
        return self._rows(
            "SELECT * FROM positions WHERE trading_date=? "
            "ORDER BY entry_time ASC, created_at ASC, rowid ASC", (trading_date,),
        )

    def _legs(self, position: dict) -> List[dict]:
        position_id = str(position.get("position_id") or "")
        if self.console is not None:
            return self.console.legs_for(position_id)
        return self._rows(
            "SELECT * FROM position_legs WHERE position_id=? ORDER BY leg_id ASC",
            (position_id,),
        )

    def block_lines(self, index: int, position: dict, legs: List[dict],
                    chain: Optional[dict], as_of: Any) -> List[str]:
        """The trade block: the console's, or a narrow-screen variant of it."""
        if self.block_style == "compact":
            try:
                return self._compact_block(index, position, legs, chain, as_of)
            except Exception as exc:
                self._log("debug", f"compact block failed ({exc}); using console")
        if self.console is None:
            return []
        try:
            return list(self.console.render(index, position, legs, chain or {}, as_of))
        except Exception as exc:
            self._log("debug", f"block render failed for {position.get('position_id')}: {exc}")
            return []

    def _compact_block(self, index: int, position: dict, legs: List[dict],
                       chain: Optional[dict], as_of: Any) -> List[str]:
        """Same fields as the console block, 44 columns wide.

        Telegram on a phone wraps an 84-column block into unreadability, so
        TELEGRAM_TRADE_BLOCK_STYLE=compact trades the rules for indentation
        and drops the arithmetic footnotes. Nothing that identifies the trade
        or its money is lost.
        """
        chain = chain or {}
        status = str(position.get("status") or "OPEN").upper()
        closed = status.startswith("CLOSE")
        lots = 0
        try:
            lots = int(position.get("final_lots") or 0)
        except (TypeError, ValueError):
            lots = 0
        if not lots and legs:
            try:
                lots = max(1, int(_tr_num(legs[0].get("qty")) //
                                  max(1.0, _tr_num(getattr(self.config, "lot_size", 1), 1.0))))
            except Exception:
                lots = 1

        out = [
            f"Trade-{index} | {position.get('strategy_name') or 'UNKNOWN'}"
            f" | {'Closed' if closed else 'Open'}",
            f"  Start {_tr_hhmm(position.get('entry_time'))}",
        ]
        for leg in legs:
            out.append(
                f"   {_tr_side(leg.get('action'))} {lots} lot "
                f"{_tr_symbol(leg.get('option_type'))} @ "
                f"{_tr_price(leg.get('entry_price'))} "
                f"(strike {_tr_strike(leg.get('strike'))})"
            )
        reason = position.get("exit_reason") if closed else None
        out.append(
            f"  End {_tr_hhmm(position.get('exit_time') if closed else as_of)}"
            + (f" ({reason})" if reason else "")
        )
        for leg in legs:
            leg_closed = str(leg.get("leg_status") or "").upper() == "CLOSED"
            if closed and leg_closed and leg.get("exit_price") not in (None, ""):
                price = leg.get("exit_price")
            else:
                price = self._mark(leg, chain)
                if price is None:
                    price = leg.get("entry_price")
            out.append(
                f"   {_tr_closing_side(leg.get('action'))} {lots} lot "
                f"{_tr_symbol(leg.get('option_type'))} @ {_tr_price(price)} "
                f"(strike {_tr_strike(leg.get('strike'))})"
            )
        if not legs:
            out.append("   no leg rows persisted for this position")

        committed, committed_basis = (0.0, [])
        net, realised, profit_basis = (0.0, False, [])
        if self.console is not None:
            try:
                committed, committed_basis = self.console.investment(position, legs)
                net, realised, profit_basis = self.console.profit(
                    position, legs, chain)
            except Exception as exc:
                self._log("debug", f"compact money failed: {exc}")
        out.append(f"  Position Status: {'Close' if closed else 'Open'}")
        out.append(f"  Total Investment: {_tr_money(committed)}")
        out.append(f"  Total Profit: {_tr_signed(net)} "
                   f"{'realised' if realised else 'unrealised'}")
        # One footnote each, wrapped: they explain the arithmetic and are the
        # only free-text lines in the block, so they are the only ones that
        # need it.
        import textwrap
        for line in list(committed_basis or [])[:1] + list(profit_basis or [])[:1]:
            out.append(textwrap.fill(
                f"    {line}", width=_COMPACT_WIDTH, subsequent_indent="      "))
        return out

    def _mark(self, leg: dict, chain: dict) -> Optional[float]:
        """What closing this leg now would cost, from the chain if possible."""
        if self.console is not None:
            try:
                return self.console._mark_leg(leg, chain, True)
            except Exception:
                return None
        return None

    # ── the five events ───────────────────────────────────────────────

    @staticmethod
    def _resolve(snap: Any) -> dict:
        """A snapshot, or a callable that builds one only when needed."""
        if snap is None:
            return {}
        if callable(snap):
            try:
                return dict(snap() or {})
            except Exception:
                return {}
        try:
            return dict(snap)
        except Exception:
            return {}

    def notify_started(self, snap: Any = None) -> bool:
        """Requirement 1: one message when the engine starts."""
        if not self.enabled:
            return False
        if self._started_sent:
            return False
        try:
            data = self._resolve(snap)
            self._started_sent = True
            self._stopped_sent = False
            # Arm the heartbeat from the start message: the first periodic
            # update is due one interval after the engine came up, not one
            # interval after the process did.
            self._last_heartbeat_mono = self.clock()
            text = self.compose_status(KIND_START, data)
            ok = self.send(text, KIND_START)
            self._log("info", f"Telegram: engine start update "
                              f"{self._delivery_word(ok)}")
            return ok
        except Exception as exc:
            self._log("warning", f"Telegram start update failed: {exc}")
            return False

    def notify_start_failed(self, reason: str = "", snap: Any = None) -> bool:
        """A start that never reached the loop still gets one message."""
        if not self.enabled:
            return False
        try:
            data = self._resolve(snap)
            data["reason"] = f"start failed: {reason}" if reason else "start failed"
            text = self.compose_status(KIND_STOP, data)
            self._stopped_sent = True
            return self.send(text, KIND_STOP)
        except Exception as exc:
            self._log("warning", f"Telegram start-failure update failed: {exc}")
            return False

    def heartbeat_if_due(self, snap: Any = None) -> bool:
        """Requirement 2: one message every TELEGRAM_HEARTBEAT_MIN minutes.

        Called from the top of the main loop, so it must be cheap when it is
        not due: a monotonic comparison, and no snapshot, no query, no text.
        """
        if not self.enabled:
            return False
        try:
            now = self.clock()
            if self._last_heartbeat_mono and \
                    (now - self._last_heartbeat_mono) < self.heartbeat_sec:
                return False
            self._last_heartbeat_mono = now
            data = self._resolve(snap)
            text = self.compose_status(KIND_HEARTBEAT, data)
            return self.send(text, KIND_HEARTBEAT)
        except Exception as exc:
            self._log("warning", f"Telegram heartbeat failed: {exc}")
            return False

    def notify_stopped(self, snap: Any = None, reason: str = "") -> bool:
        """Requirement 5: one message when the engine stops, once per run."""
        if not self.enabled:
            return False
        if self._stopped_sent:
            return False
        try:
            data = self._resolve(snap)
            if reason:
                data["reason"] = reason
            self._stopped_sent = True
            text = self.compose_status(KIND_STOP, data)
            ok = self.send(text, KIND_STOP)
            self._log("info", f"Telegram: engine stop update "
                              f"{self._delivery_word(ok)} "
                              f"({data.get('reason') or 'no reason given'})")
            return ok
        except Exception as exc:
            self._log("warning", f"Telegram stop update failed: {exc}")
            return False

    def sync_trades(self, snap: Any = None, chain: Optional[dict] = None,
                    as_of: Any = None, trading_date: Optional[str] = None,
                    ) -> int:
        """Requirements 3 and 4: one message per trade placed and per close.

        The book is the source of events. A position the reporter has not
        seen before was placed since the last call; one whose status turned
        CLOSED was closed since the last call. Because the events come from
        the persisted positions rather than from a hook inside the order
        path, they also cover trades closed by the hard-exit sweep, by the
        watchdog flatten and by the EOD square-off - paths that do not go
        through the same call site.
        """
        if not self.enabled:
            return 0
        sent = 0
        try:
            # A caller may hand the date, the timestamp and the chain over as
            # keyword arguments or inside the snapshot; both are honoured, and
            # a snapshot given as a callable is still not called until there
            # is an event worth reporting.
            peek = snap if isinstance(snap, dict) else {}
            as_of = as_of or peek.get("as_of") or now_ist()
            if trading_date is None:
                trading_date = peek.get("trading_date") or (
                    as_of.date().isoformat() if isinstance(as_of, datetime)
                    else today_ist().isoformat()
                )
            if chain is None:
                chain = peek.get("chain")
            trading_date = str(trading_date)
            if self._day != trading_date:
                self.reset_day(trading_date)
            rows = self._positions(trading_date)
            if not rows:
                return 0

            events: List[Tuple[int, dict, List[dict], str]] = []
            for index, position in enumerate(rows, start=1):
                position_id = str(position.get("position_id") or f"#{index}")
                status = "CLOSED" if str(
                    position.get("status") or "OPEN").upper().startswith("CLOSE") \
                    else "OPEN"
                previous = self._seen.get(position_id)
                if previous == status:
                    continue
                self._seen[position_id] = status
                if previous is None and status == "OPEN":
                    events.append((index, position, self._legs(position), KIND_TRADE_OPEN))
                elif status == "CLOSED":
                    events.append((index, position, self._legs(position), KIND_TRADE_CLOSE))
            if not events:
                return 0

            # One snapshot for the whole batch, and only now that there is
            # something to say: a quiet cycle costs no query at all.
            data = self._resolve(snap)
            data.setdefault("as_of", as_of)
            data.setdefault("trading_date", trading_date)
            if chain is not None:
                data["chain"] = chain
            for index, position, legs, kind in events:
                try:
                    text = self.compose_trade(kind, index, position, legs, data)
                    if self.send(text, kind):
                        sent += 1
                except Exception as exc:
                    self._log(
                        "warning",
                        f"Telegram trade update failed for "
                        f"{position.get('position_id')}: {exc}",
                    )
            return sent
        except Exception as exc:
            self._log("warning", f"Telegram trade sync failed: {exc}")
            return sent

    def reset_day(self, trading_date: Optional[str] = None) -> None:
        """Drop the day's latches on the day roll, and only those.

        The heartbeat clock and the started/stopped latches belong to the
        process, not to the session: an engine left running overnight must
        keep beating and must not announce a second start.
        """
        self._day = str(trading_date or today_ist().isoformat())
        self._seen.clear()


# ─────────────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────────────
#
# No network and no waiting: the transport records what would have been sent
# and can be told to fail, to 429 or to explode, and the clock only moves when
# the test moves it. That makes the heartbeat schedule, the send spacing and
# the retry behaviour ordinary assertions instead of sleeps.

class _FakeTransport:
    """Stands in for the HTTPS call. Records payloads, fails on request."""

    def __init__(self, fail_times: int = 0, retry_after: Optional[float] = None,
                 explode: bool = False, hang: Optional[threading.Event] = None):
        self.calls: List[dict] = []
        self.fail_times = int(fail_times)
        self.retry_after = retry_after
        self.explode = bool(explode)
        self.hang = hang

    def __call__(self, payload: dict) -> Tuple[bool, Optional[float], str]:
        self.calls.append(payload)
        if self.hang is not None:
            self.hang.wait(5.0)
        if self.explode:
            raise RuntimeError("transport exploded")
        if self.fail_times > 0:
            self.fail_times -= 1
            if self.retry_after:
                return False, float(self.retry_after), \
                    "HTTP 429 rate limited by Telegram"
            return False, None, "HTTP 500: upstream boom"
        return True, None, ""

    @property
    def texts(self) -> List[str]:
        return [str(c.get("text") or "") for c in self.calls]


class _FakeClock:
    """A monotonic clock and a sleep the test drives by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)
        self.slept: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self.now += float(seconds)

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _StubLogger:
    """Records every log call: a swallowed exception becomes an assertion."""

    def __init__(self):
        self.records: List[Tuple[str, str]] = []

    def _rec(self, level: str, msg: Any) -> None:
        self.records.append((level, str(msg)))

    def debug(self, msg: Any, *a, **k) -> None:
        self._rec("debug", msg)

    def info(self, msg: Any, *a, **k) -> None:
        self._rec("info", msg)

    def warning(self, msg: Any, *a, **k) -> None:
        self._rec("warning", msg)

    def error(self, msg: Any, *a, **k) -> None:
        self._rec("error", msg)

    def critical(self, msg: Any, *a, **k) -> None:
        self._rec("critical", msg)

    @property
    def problems(self) -> List[str]:
        return [m for level, m in self.records if level in ("warning", "error")]


def _unesc(text: str) -> str:
    return (str(text).replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


def _pre_bodies(text: str) -> List[str]:
    """The raw monospace sections of a sent message, unescaped."""
    out = []
    for chunk in str(text).split("<pre>")[1:]:
        out.append(_unesc(chunk.split("</pre>")[0]))
    return out


def _scratch_db() -> Database:
    import tempfile
    from pathlib import Path
    return Database(Path(tempfile.mkdtemp(prefix="tg_selftest_")) / "telegram.db")


def _config(**overrides) -> Config:
    import dataclasses
    fields = dict(
        alert_telegram_bot_token="123456789:AAH-selftest-token-xyz",
        alert_telegram_chat_id="-1001234567890",
        telegram_updates_enabled=True,
        telegram_heartbeat_min=15.0,
        telegram_min_gap_sec=0.0,
        telegram_max_queue=8,
        telegram_parse_mode="HTML",
        telegram_trade_block_style="console",
        telegram_include_open_blocks=True,
    )
    fields.update(overrides)
    return dataclasses.replace(load_config(), **fields)


_DAY = "2026-09-15"
_ENTRY = "2026-09-15T12:13:00+05:30"
_EXIT = "2026-09-15T14:02:00+05:30"
# The 2026-09-11 paper trade, so the numbers in these assertions are numbers
# the engine has already been seen to produce.
_LEGS = ((23150.0, "put", "SELL", 47.65), (23050.0, "put", "BUY", 28.60))
_CHAIN = {
    23150.0: {"put": {"bid": 40.00, "ask": 40.10, "ltp": 40.05, "delta": -0.30}},
    23050.0: {"put": {"bid": 24.60, "ask": 24.70, "ltp": 24.65, "delta": -0.22}},
}


def _add_position(db: Database, pid: str, entry: str = _ENTRY,
                  strategy: str = "BULL_PUT_SPREAD", status: str = "OPEN",
                  lots: int = 2, legs=_LEGS, day: str = _DAY,
                  **extra) -> None:
    row = {
        "position_id": pid, "trading_date": day, "strategy_name": strategy,
        "strategy_type": "SELL", "entry_time": entry, "final_lots": lots,
        "status": status, "entry_credit": 18.58, "gross_credit": 19.05,
        "total_slippage": 0.026, "entry_costs_rupees": 57.67,
        "estimated_margin": 15730.0, "total_max_risk": 5693.0,
    }
    row.update(extra)
    db.insert("positions", row)
    for strike, opt, action, price in legs:
        db.insert("position_legs", {
            "position_id": pid, "strike": strike, "option_type": opt,
            "action": action, "qty": lots * 65, "entry_price": price,
            "leg_status": "OPEN",
        })


def _close_position(db: Database, pid: str, exit_time: str = _EXIT,
                    reason: str = "CLOSE_TARGET",
                    exit_prices=(40.10, 24.60)) -> None:
    db.update("positions", {
        "status": "CLOSED", "exit_time": exit_time, "exit_reason": reason,
        "exit_priority": 6, "exit_premium": 15.50,
        "gross_pnl_rupees": 461.50, "exit_costs_rupees": 55.69,
        "net_pnl_rupees": 348.14, "entry_credit_realised": 19.05,
        "last_known_premium": 15.50,
    }, {"position_id": pid})
    legs = db.query(
        "SELECT * FROM position_legs WHERE position_id=? ORDER BY leg_id ASC",
        (pid,),
    )
    for leg, price in zip(legs, exit_prices):
        db.update("position_legs", {
            "exit_price": price, "leg_status": "CLOSED",
            "quoted_mid_at_exit": round(price - 0.05, 2),
        }, {"leg_id": leg["leg_id"]})


def _add_cycles(db: Database, day: str, regimes, first_spot=24380.50,
                last_spot=24412.35) -> None:
    for idx, regime in enumerate(regimes):
        spot = first_spot if idx == 0 else (
            last_spot if idx == len(regimes) - 1 else first_spot + idx * 0.5)
        db.insert("cycle_log", {
            "cycle_time": f"{day}T09:{15 + idx}:00+05:30", "trading_date": day,
            "spot": spot, "vix": 12.40, "vol_regime": "VOL_SELL",
            "price_regime": "TREND_UP", "positioning_regime": "PCR_BULL",
            "final_regime": regime, "action_taken": "NO_TRADE",
            "open_positions": 0, "daily_pnl_net": 0.0,
        })


def _reporter(db=None, config=None, transport=None, clock=None, worker=False,
              **cfg_over):
    cfg = config or _config(**cfg_over)
    transport = transport or _FakeTransport()
    clock = clock or _FakeClock()
    rep = TelegramReporter(
        db if db is not None else _scratch_db(), cfg, None,
        transport=transport, clock=clock, sleep=clock.sleep, worker=worker,
    )
    return rep, transport, clock


def _self_test() -> None:
    print_section("TELEGRAM REPORTER SELF-TEST (v8)", char="#")
    as_of = datetime(2026, 9, 15, 11, 0, 0)

    # ── 1. Silence when there is no channel ───────────────────────────
    for label, cfg in (
        ("no bot token", _config(alert_telegram_bot_token="")),
        ("no chat id", _config(alert_telegram_chat_id="")),
        ("switched off", _config(telegram_updates_enabled=False)),
    ):
        transport = _FakeTransport()
        rep = TelegramReporter(_scratch_db(), cfg, None, transport=transport,
                               clock=_FakeClock(), sleep=lambda s: None,
                               worker=False)
        assert not rep.enabled, f"reporter should be disabled with {label}"
        assert rep.notify_started({}) is False
        assert rep.heartbeat_if_due({}) is False
        assert rep.notify_stopped({}, reason="x") is False
        assert rep.sync_trades({}) == 0
        assert transport.calls == [], f"a disabled reporter sent with {label}"
        assert "DISABLED" in rep.describe()
    print("  [OK] disabled without a token/chat id, and when switched off")

    # ── 2. The start message ──────────────────────────────────────────
    db = _scratch_db()
    _add_cycles(db, _DAY, ["TREND_SELL", "TREND_SELL", "CHOPPY"])
    rep, transport, clock = _reporter(db=db)
    started = datetime(2026, 9, 15, 9, 2, 11)
    snap = rep.session_snapshot(
        cycles=0, started_at=started, as_of=as_of,
        start_mode="manual (interactive terminal)", chain=_CHAIN,
        signals={"spot": 24412.35, "vix": 12.40, "prev_close": 24353.20,
                 "final_regime": "TREND_SELL", "vol_regime": "VOL_SELL",
                 "price_regime": "TREND_UP"},
    )
    assert rep.notify_started(snap) is True
    assert len(transport.calls) == 1, "start must be exactly one message"
    payload = transport.calls[0]
    assert payload["chat_id"] == "-1001234567890"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True
    text = payload["text"]
    for frag in (
        "Nifty options trade engine started at 11:00:00 IST",
        "Nifty opened at: 24,380.50",
        "Current Nifty Spot at: 24,412.35",
        "Nifty closed at: 24,353.20 (previous day)",
        "Current nifty regime: vol VOL_SELL | price TREND_UP | "
        "positioning PCR_BULL | final TREND_SELL",
        "Overall nifty regime: TREND_SELL (dominant of 3 cycle(s))",
        "Total cycles completed: 0",
        "PAPER TRADE",
        "manual (interactive terminal)",
    ):
        assert frag in _unesc(text), f"start message is missing {frag!r}"
    assert len(text) <= MAX_MESSAGE_CHARS
    # ...and it does not repeat itself
    assert rep.notify_started(snap) is False
    assert len(transport.calls) == 1, "a second start message was sent"
    print("  [OK] start message carries every requested field, exactly once")

    # ── 3. Nothing dynamic can break the markup ───────────────────────
    db3 = _scratch_db()
    _add_position(db3, "p3", strategy="<script>&\"quote\"</script>",
                  day=_DAY, entry=_ENTRY)
    rep3, tr3, _c3 = _reporter(db=db3)
    assert rep3.sync_trades({"as_of": as_of, "trading_date": _DAY,
                             "chain": _CHAIN}) == 1
    body = tr3.texts[-1]
    assert "<script>" not in body, "raw markup reached Telegram"
    assert "&lt;script&gt;" in body
    assert "&amp;" in body
    print("  [OK] HTML escaping: no dynamic text can break parse_mode")

    # ── 4. The heartbeat is a schedule, not a hope ────────────────────
    db4 = _scratch_db()
    rep4, tr4, clk4 = _reporter(db=db4, telegram_heartbeat_min=15.0)
    snap4 = {"as_of": as_of, "trading_date": _DAY, "cycles": 7, "chain": {}}
    assert rep4.notify_started(snap4) is True
    assert len(tr4.calls) == 1
    clk4.advance(14 * 60 + 59)
    assert rep4.heartbeat_if_due(snap4) is False, "fired before 15 minutes"
    assert len(tr4.calls) == 1
    clk4.advance(1)
    assert rep4.heartbeat_if_due(snap4) is True, "did not fire at 15 minutes"
    assert len(tr4.calls) == 2
    assert "15-minute heartbeat" in _unesc(tr4.texts[-1])
    assert rep4.heartbeat_if_due(snap4) is False, "fired twice in a row"
    clk4.advance(15 * 60)
    assert rep4.heartbeat_if_due(snap4) is True, "did not re-arm"
    assert len(tr4.calls) == 3
    rep4b, tr4b, clk4b = _reporter(db=_scratch_db(), telegram_heartbeat_min=0.5)
    rep4b.notify_started(snap4)
    clk4b.advance(29)
    assert rep4b.heartbeat_if_due(snap4) is False
    clk4b.advance(2)
    assert rep4b.heartbeat_if_due(snap4) is True, "interval is not configurable"
    print("  [OK] heartbeat every TELEGRAM_HEARTBEAT_MIN, armed by the start")

    # ── 5. A trade order placed ───────────────────────────────────────
    db5 = _scratch_db()
    _add_position(db5, "p5", day=_DAY)
    rep5, tr5, _c5 = _reporter(db=db5)
    snap5 = {"as_of": as_of, "trading_date": _DAY, "chain": _CHAIN,
             "cycles": 12, "spot": 24412.35, "final_regime": "TREND_SELL",
             "total_pnl": 0.0}
    assert rep5.sync_trades(snap5) == 1
    sent = tr5.texts[-1]
    assert "Trade order placed - Trade-1 BULL_PUT_SPREAD at 12:13" in _unesc(sent)
    assert "Position Status: Open" in _unesc(sent)
    assert "unrealised" in _unesc(sent)
    # the block is the console's block, byte for byte
    pos5 = db5.query_one("SELECT * FROM positions WHERE position_id='p5'")
    legs5 = rep5.console.legs_for("p5")
    expected = "\n".join(rep5.console.render(1, pos5, legs5, _CHAIN, as_of))
    assert _pre_bodies(sent)[0] == expected, \
        "the Telegram block is not the console block"
    assert "Sold: 2 lot of PE with premium: 47.65 at strike: 23150" in expected
    assert "Bought: 2 lot of PE with premium: 28.60 at strike: 23050" in expected
    # latched: the same open trade is not announced again
    assert rep5.sync_trades(snap5) == 0
    assert len(tr5.calls) == 1
    print("  [OK] trade placed: one message, the console block, no repeats")

    # ── 6. A trade order closed ───────────────────────────────────────
    _close_position(db5, "p5")
    assert rep5.sync_trades(snap5) == 1
    sent = _unesc(tr5.texts[-1])
    assert "Trade order closed - Trade-1 BULL_PUT_SPREAD at 14:02" in sent
    assert "reason CLOSE_TARGET" in sent
    assert "Net Rs +348.14 realised" in sent
    assert "Position Status: Close" in sent
    assert "Total Profit: Rs +348.14 realised" in sent
    assert "Closed - CLOSE_TARGET" in sent
    assert rep5.sync_trades(snap5) == 0, "a closed trade was re-announced"
    print("  [OK] trade closed: realised money, reason, no repeats")

    # ── 7. Opened AND closed between two cycles ───────────────────────
    db7 = _scratch_db()
    rep7, tr7, _c7 = _reporter(db=db7)
    snap7 = {"as_of": as_of, "trading_date": _DAY, "chain": _CHAIN}
    assert rep7.sync_trades(snap7) == 0
    _add_position(db7, "p7", day=_DAY)
    _close_position(db7, "p7")
    assert rep7.sync_trades(snap7) == 1, \
        "a trade that opened and closed between cycles needs exactly one message"
    assert "Trade order closed" in _unesc(tr7.texts[-1])
    print("  [OK] a round trip inside one cycle is one message, not two")

    # ── 8. The day roll clears the trade latch and nothing else ───────
    db8 = _scratch_db()
    _add_position(db8, "p8", day=_DAY)
    rep8, tr8, clk8 = _reporter(db=db8)
    day16 = "2026-09-16"
    as_of16 = datetime(2026, 9, 16, 10, 0, 0)
    assert rep8.notify_started({"as_of": as_of, "trading_date": _DAY,
                                "chain": {}}) is True
    after_start = len(tr8.calls)
    assert rep8.sync_trades({"as_of": as_of, "trading_date": _DAY,
                             "chain": {}}) == 1
    _add_position(db8, "p8b", day=day16, entry="2026-09-16T09:20:00+05:30")
    assert rep8.sync_trades({"as_of": as_of16, "trading_date": day16,
                             "chain": {}}) == 1, "the new day's trade was missed"
    assert rep8.sync_trades({"as_of": as_of16, "trading_date": day16,
                             "chain": {}}) == 0
    # the process-level latches belong to the process, not to the session
    assert rep8._started_sent, "a day roll cleared the start latch"
    assert rep8.notify_started({"as_of": as_of16}) is False
    assert len(tr8.calls) == after_start + 2, \
        "a day roll made the engine announce a second start"
    assert not rep8._stopped_sent, "a day roll marked the engine stopped"
    print("  [OK] day roll re-arms trade events but not start/stop/heartbeat")

    # ── 9. Numbering agrees with the console ──────────────────────────
    db9 = _scratch_db()
    _add_position(db9, "p9a", day=_DAY, entry="2026-09-15T09:20:00+05:30",
                  strategy="LONG_CALL",
                  legs=((23300.0, "call", "BUY", 109.75),))
    _add_position(db9, "p9b", day=_DAY, entry=_ENTRY)
    rep9, tr9, _c9 = _reporter(db=db9)
    assert rep9.sync_trades({"as_of": as_of, "trading_date": _DAY,
                             "chain": _CHAIN}) == 2
    titles = [_unesc(t).splitlines()[0] for t in tr9.texts]
    assert "Trade-1 LONG_CALL" in titles[0], titles
    assert "Trade-2 BULL_PUT_SPREAD" in titles[1], titles
    rows9 = rep9.console.positions_for(_DAY)
    assert [r["position_id"] for r in rows9] == ["p9a", "p9b"]
    print("  [OK] Trade-<n> numbering is the console's, not the reporter's")

    # ── 10. The narrow-screen block ───────────────────────────────────
    db10 = _scratch_db()
    _add_position(db10, "p10", day=_DAY)
    _close_position(db10, "p10")
    rep10, tr10, _c10 = _reporter(db=db10, telegram_trade_block_style="compact")
    assert rep10.sync_trades({"as_of": as_of, "trading_date": _DAY,
                              "chain": _CHAIN}) == 1
    body10 = _pre_bodies(tr10.texts[-1])[0]
    for frag in ("Trade-1 | BULL_PUT_SPREAD | Closed",
                 "Start 12:13", "End 14:02 (CLOSE_TARGET)",
                 "Sold 2 lot PE @ 47.65 (strike 23150)",
                 "Bought 2 lot PE @ 28.60 (strike 23050)",
                 "Bought 2 lot PE @ 40.10 (strike 23150)",
                 "Sold 2 lot PE @ 24.60 (strike 23050)",
                 "Position Status: Close",
                 "Total Investment: Rs ",
                 "Total Profit: Rs +348.14 realised"):
        assert frag in body10, f"compact block is missing {frag!r}"
    assert TRADE_REPORT_RULE not in body10, "compact block kept the 84-column rule"
    assert max(len(line) for line in body10.splitlines()) < 60, \
        "compact block is still too wide for a phone"
    print("  [OK] compact style: same facts, 44 columns, no lost field")

    # ── 11. A long day is split, never truncated ──────────────────────
    db11 = _scratch_db()
    for i in range(14):
        _add_position(db11, f"p11-{i:02d}", day=_DAY,
                      entry=f"2026-09-15T09:{20 + i}:00+05:30")
    rep11, tr11, _c11 = _reporter(db=db11)
    snap11 = rep11.session_snapshot(as_of=datetime(2026, 9, 15, 15, 31, 0),
                                    trading_date=_DAY, chain=_CHAIN)
    assert rep11.notify_stopped(snap11, reason="end of day") is True
    parts = tr11.calls
    assert len(parts) >= 2, f"a 14-trade day should need parts, got {len(parts)}"
    joined = ""
    for idx, payload in enumerate(parts, start=1):
        text = payload["text"]
        assert len(text) <= MAX_MESSAGE_CHARS, \
            f"part {idx} is {len(text)} chars, over Telegram's limit"
        assert f"(part {idx}/{len(parts)})" in _unesc(text)
        assert text.count("<pre>") == text.count("</pre>"), \
            f"part {idx} has unbalanced <pre> tags"
        joined += "\n".join(_pre_bodies(text))
    for i in range(14):
        assert f"Trade-{i + 1}" in joined, f"Trade-{i + 1} was lost in the split"
    print(f"  [OK] chunking: 14 trades -> {len(parts)} parts, all under 4096, "
          f"tags balanced")

    # ── 12. Sends are spaced inside Telegram's flood limit ────────────
    rep12, tr12, clk12 = _reporter(db=_scratch_db(), telegram_min_gap_sec=3.5)
    assert rep12.send("first", KIND_INFO) is True
    assert clk12.slept == [], "the first send should not wait"
    assert rep12.send("second", KIND_INFO) is True
    assert len(clk12.slept) == 1 and abs(clk12.slept[0] - 3.5) < 1e-6, \
        f"expected a 3.5s gap, slept {clk12.slept}"
    assert rep12.stats["throttled"] == 1
    print("  [OK] min gap between sends (Telegram allows ~20/min per chat)")

    # ── 13. A 429 is obeyed, not fought ───────────────────────────────
    tr13 = _FakeTransport(fail_times=1, retry_after=7.0)
    rep13, _t, clk13 = _reporter(db=_scratch_db(), transport=tr13)
    assert rep13.send("rate limited", KIND_INFO) is True
    assert len(tr13.calls) == 2, "the message was not retried after the 429"
    assert 7.0 in clk13.slept, f"retry_after was ignored: {clk13.slept}"
    assert rep13.stats["retries"] == 1 and rep13.stats["sent"] == 1
    print("  [OK] HTTP 429: waits retry_after, then delivers")

    # ── 14/15. A dead network costs a log line, never a cycle ─────────
    tr14 = _FakeTransport(fail_times=99)
    rep14, _t14, clk14 = _reporter(db=_scratch_db(), transport=tr14)
    assert rep14.send("doomed", KIND_INFO) is False
    assert len(tr14.calls) == MAX_SEND_ATTEMPTS
    assert rep14.stats["failed"] == 1 and rep14.stats["sent"] == 0
    assert "HTTP 500" in rep14.last_error
    tr15 = _FakeTransport(explode=True)
    rep15, _t15, _c15 = _reporter(db=_scratch_db(), transport=tr15)
    assert rep15.send("exploding", KIND_INFO) is False
    assert rep15.notify_started({"as_of": as_of}) is False
    assert rep15.sync_trades({"as_of": as_of, "trading_date": _DAY}) == 0
    assert "exploded" in rep15.last_error
    print("  [OK] failures and exceptions are contained: %d attempts, no raise"
          % MAX_SEND_ATTEMPTS)

    # ── 16. A full queue sacrifices heartbeats, never a trade ─────────
    rep16, _t16, _c16 = _reporter(db=_scratch_db(), telegram_max_queue=4)
    for _ in range(4):
        rep16._enqueue(KIND_HEARTBEAT, "heartbeat")
    rep16._enqueue(KIND_TRADE_CLOSE, "a trade closed")
    kinds = [item[0] for item in rep16._items]
    assert len(kinds) == 4, f"queue grew past its bound: {kinds}"
    assert KIND_TRADE_CLOSE in kinds, "a trade event was dropped for a heartbeat"
    assert kinds.count(KIND_HEARTBEAT) == 3, kinds
    assert rep16.stats["dropped"] == 1
    rep16b, _t, _c = _reporter(db=_scratch_db(), telegram_max_queue=4)
    for kind in (KIND_TRADE_OPEN, KIND_HEARTBEAT, KIND_TRADE_CLOSE, KIND_HEARTBEAT):
        rep16b._enqueue(kind, kind)
    rep16b._enqueue(KIND_HEARTBEAT, "newest heartbeat")
    kinds = [item[0] for item in rep16b._items]
    assert kinds == [KIND_TRADE_OPEN, KIND_TRADE_CLOSE, KIND_HEARTBEAT,
                     KIND_HEARTBEAT], kinds
    print("  [OK] queue overflow drops the oldest heartbeat, keeps trade events")

    # ── 17. The sender thread delivers, and close() drains it ─────────
    tr17 = _FakeTransport()
    rep17 = TelegramReporter(_scratch_db(), _config(), None, transport=tr17,
                             worker=True)
    assert rep17._thread is not None and rep17._thread.is_alive()
    for i in range(3):
        assert rep17.send(f"threaded {i}", KIND_INFO) is True
    assert rep17.stats["queued"] == 3
    rep17.close(timeout=5.0)
    assert rep17.pending() == 0, "close() left updates in the queue"
    assert tr17.texts == ["threaded 0", "threaded 1", "threaded 2"], tr17.texts
    assert rep17.stats["sent"] == 3
    assert not (rep17._thread is not None and rep17._thread.is_alive())
    rep17.close(timeout=1.0)          # idempotent
    print("  [OK] sender thread: in order, drained by close(), no network on "
          "the loop")

    # ── 18. What each status message carries ──────────────────────────
    db18 = _scratch_db()
    _add_position(db18, "p18-open", day=_DAY, entry="2026-09-15T09:20:00+05:30",
                  strategy="LONG_CALL", legs=((23300.0, "call", "BUY", 109.75),))
    _add_position(db18, "p18-closed", day=_DAY)
    _close_position(db18, "p18-closed")
    rep18, tr18, clk18 = _reporter(db=db18)
    snap18 = {"as_of": as_of, "trading_date": _DAY, "chain": _CHAIN, "cycles": 40}
    assert rep18.notify_started(snap18) is True
    clk18.advance(15 * 60)
    assert rep18.heartbeat_if_due(snap18) is True
    hb = "\n".join(_pre_bodies(tr18.texts[-1]))
    assert "Trade-1" in hb and "LONG_CALL" in hb, "the open trade is missing"
    assert "Trade-2" not in hb, \
        "a heartbeat re-sent a trade that already had its own close message"
    assert "Position Status: Open" in hb
    assert rep18.notify_stopped(snap18, reason="end of day") is True
    stop = "\n".join(_pre_bodies(tr18.texts[-1]))
    assert "Trade-1" in stop and "Trade-2" in stop, \
        "the stop message must carry the whole day"
    assert "stopped at" in _unesc(tr18.texts[-1])
    assert "end of day" in _unesc(tr18.texts[-1])
    assert rep18.notify_stopped(snap18, reason="again") is False
    assert len([c for c in tr18.calls]) == 3, "the stop message repeated"
    rep18b, tr18b, clk18b = _reporter(db=db18, telegram_include_open_blocks=False)
    rep18b.notify_started(snap18)
    clk18b.advance(15 * 60)
    assert rep18b.heartbeat_if_due(snap18) is True
    assert "<pre>" not in tr18b.texts[-1], \
        "TELEGRAM_INCLUDE_OPEN_BLOCKS=false still attached trade blocks"
    print("  [OK] heartbeat carries the open trades, stop carries the day")

    # ── 19. The book alone is enough ──────────────────────────────────
    db19 = _scratch_db()
    db19.insert("intraday_candles", {
        "trading_date": _DAY, "candle_time": f"{_DAY}T09:15:00+05:30",
        "interval_min": 1, "open": 24380.50, "high": 24385.0,
        "low": 24378.0, "close": 24383.0,
    })
    db19.insert("daily_summary", {
        "trading_date": "2026-09-12", "nifty_open": 24300.0,
        "nifty_close": 24353.20, "net_pnl_rupees": 0.0,
    })
    _add_cycles(db19, _DAY, ["TREND_SELL", "CHOPPY", "TREND_SELL",
                             "SIGNAL_ONLY", "TREND_SELL"])
    _add_position(db19, "p19", day=_DAY)
    _close_position(db19, "p19")
    rep19, _t19, _c19 = _reporter(db=db19)
    snap19 = rep19.session_snapshot(as_of=datetime(2026, 9, 15, 11, 0, 0))
    assert snap19["day_open"] == 24380.50, snap19["day_open"]
    assert snap19["prev_close"] == 24353.20, snap19["prev_close"]
    assert snap19["dominant_regime"] == "TREND_SELL", snap19["dominant_regime"]
    assert snap19["regime_cycles"] == 4, \
        f"SIGNAL_ONLY is not a regime: {snap19['regime_cycles']}"
    assert snap19["cycles"] == 5, snap19["cycles"]
    assert abs(snap19["realized_pnl"] - 348.14) < 1e-9, snap19["realized_pnl"]
    assert snap19["closed_trades"] == 1 and snap19["open_positions"] == 0
    assert snap19["day_high"] == 24412.35 and snap19["day_low"] == 24380.50
    print("  [OK] snapshot rebuilds the day from the book with no engine state")

    # ── 20. "Nifty closed at" says which close it means ───────────────
    snap20a = rep19.session_snapshot(as_of=datetime(2026, 9, 15, 11, 0, 0))
    assert snap20a["day_close"] is None
    assert snap20a["day_close_label"] == "previous day"
    assert snap20a["closed_value"] == 24353.20
    snap20b = rep19.session_snapshot(as_of=datetime(2026, 9, 15, 15, 31, 0))
    assert snap20b["day_close"] == 24412.35, snap20b["day_close"]
    assert snap20b["day_close_label"] == "today"
    assert snap20b["closed_value"] == 24412.35
    rep20, tr20, _c20 = _reporter(db=db19)
    rep20.notify_stopped(snap20b, reason="end of day")
    assert "Nifty closed at: 24,412.35 (today)" in _unesc(tr20.texts[-1])
    print("  [OK] the closing price is labelled today or previous day")

    # ── 21. No database, no chain, no signals: still one message ──────
    rep21, tr21, _c21 = _reporter(db=None)
    rep21.db = None
    rep21.console = None
    assert rep21.notify_started({"as_of": as_of}) is True
    text21 = _unesc(tr21.texts[-1])
    assert "Nifty opened at: n/a" in text21
    assert "Current Nifty Spot at: n/a" in text21
    assert "Overall nifty regime: n/a" in text21
    assert rep21.sync_trades({"as_of": as_of}) == 0
    assert rep21.notify_stopped({"as_of": as_of}, reason="no book") is True
    print("  [OK] with no database it still reports, and says n/a not nothing")

    # ── 22. A start that never got off the ground ─────────────────────
    rep22, tr22, _c22 = _reporter(db=_scratch_db())
    assert rep22.notify_start_failed("Upstox access token is invalid/expired") is True
    text22 = _unesc(tr22.texts[-1])
    assert "stopped at" in text22
    assert "start failed: Upstox access token is invalid/expired" in text22
    assert rep22.notify_stopped({"as_of": as_of}, reason="late") is False, \
        "a failed start must not be followed by a second stop message"
    print("  [OK] a failed start is reported once, as a stop with the reason")

    # ── 23. The token never leaves the process ────────────────────────
    rep23, tr23, _c23 = _reporter(db=_scratch_db())
    token = _config().alert_telegram_bot_token
    assert token not in rep23.describe()
    assert rep23.masked_token != token and token[-3:] in rep23.masked_token
    rep23.notify_started({"as_of": as_of})
    assert all(token not in c["text"] for c in tr23.calls)
    assert all(token not in str(v) for c in tr23.calls for v in c.values())
    assert rep23.describe().startswith("Telegram updates ON")
    print("  [OK] the bot token is masked in every log line and message")

    # ── 24. The five requirements, through main.py's own methods ──────
    # Everything above tests this module. This drives the methods MainEngine
    # actually calls - _telegram_snapshot(), _telegram_after_cycle(),
    # _telegram_heartbeat(), _telegram_notify_stopped() - on a real
    # MainEngine instance with the broker-facing engines stubbed, so the
    # wiring is verified end to end without a broker or a Telegram account.
    print_section("Main Engine Wiring (the five requirements)")
    import signal as _signal24
    try:
        import main as _main24
    except Exception as exc24:
        print(f"  [SKIP] main.py could not be imported ({exc24})")
        _main24 = None
    if _main24 is not None:
        # MainEngine's snapshot builder stamps a message with the real clock,
        # so this section dates its fixtures today rather than on the fixed
        # sample date the sections above use.
        day24 = today_ist().isoformat()
        db24 = _scratch_db()
        _add_cycles(db24, day24, ["TREND_SELL", "TREND_SELL", "CHOPPY"])
        cfg24 = _config()
        tr24 = _FakeTransport()
        clk24 = _FakeClock()
        log24 = _StubLogger()

        class _StubMarket:
            def __init__(self, state, chain):
                self.state = state
                self.last_chain = chain

        class _StubExecution:
            def __init__(self, db, day):
                self.db, self.day = db, day

            def _get_open_positions(self):
                return self.db.query(
                    "SELECT * FROM positions WHERE trading_date=? "
                    "AND status='OPEN'", (self.day,))

            def _get_position_legs(self, position_id):
                return self.db.query(
                    "SELECT * FROM position_legs WHERE position_id=? "
                    "ORDER BY leg_id ASC", (position_id,))

        eng = _main24.MainEngine.__new__(_main24.MainEngine)
        eng.config           = cfg24
        eng.db               = db24
        eng.logger           = log24
        eng.market_engine    = _StubMarket(
            {"trading_date": day24, "daily_pnl": 348.14,
             "current_capital": 1000348.14, "daily_halted": False,
             "entry_count": 1, "consecutive_stops": 0,
             "prev_spot": 24412.35, "prev_vix": 12.40},
            _CHAIN,
        )
        eng.execution_engine = _StubExecution(db24, day24)
        eng.telegram = TelegramReporter(
            db24, cfg24, log24,
            console=TradeConsoleReporter(db24, cfg24, log24),
            transport=tr24, clock=clk24, sleep=clk24.sleep, worker=False,
        )
        eng.loop_count       = 12
        eng._started_at      = datetime(2026, 9, 15, 9, 2, 11)
        eng._start_mode      = "manual (interactive terminal)"
        eng._last_signals    = {}
        eng._eod_done        = False
        eng._signal_received = None
        eng.running          = True

        sig24 = {"spot": 24412.35, "vix": 12.40, "prev_close": 24353.20,
                 "vol_regime": "VOL_SELL", "price_regime": "TREND_UP",
                 "final_regime": "TREND_SELL"}

        # (1) the engine started
        assert eng.telegram.notify_started(eng._telegram_snapshot(sig24)) is True
        assert len(tr24.calls) == 1
        start24 = _unesc(tr24.texts[-1])
        assert "Nifty options trade engine started at" in start24
        assert "Total cycles completed: 12" in start24
        assert "manual (interactive terminal)" in start24
        assert "Overall nifty regime: TREND_SELL" in start24

        # (3) a trade order placed - and only once
        _add_position(db24, "p24", day=day24)
        eng._telegram_after_cycle(sig24, 348.14)
        assert len(tr24.calls) == 2, "a placed trade did not produce a message"
        assert "Trade order placed - Trade-1 BULL_PUT_SPREAD" in _unesc(tr24.texts[-1])
        eng._telegram_after_cycle(sig24, 348.14)
        assert len(tr24.calls) == 2, "the same open trade was announced twice"

        # (4) a trade order closed
        _close_position(db24, "p24")
        eng._telegram_after_cycle(sig24, 696.28)
        assert len(tr24.calls) == 3, "a closed trade did not produce a message"
        closed24 = _unesc(tr24.texts[-1])
        assert "Trade order closed - Trade-1 BULL_PUT_SPREAD" in closed24
        assert "Net Rs +348.14 realised" in closed24

        # A second trade, left open, so the heartbeat has something to carry
        # and compute_unrealized_pnl() has something to mark.
        _add_position(db24, "p24b", day=day24, entry="2026-09-15T13:05:00+05:30",
                      last_known_premium=15.50, last_liquidation_premium=15.50)
        eng._telegram_after_cycle(sig24, 348.14)
        assert len(tr24.calls) == 4
        assert "Trade order placed - Trade-2" in _unesc(tr24.texts[-1])

        # (2) the heartbeat: nothing before 15 minutes, one at 15
        eng._telegram_heartbeat()
        assert len(tr24.calls) == 4, "a heartbeat fired early"
        clk24.advance(15 * 60)
        eng._telegram_heartbeat()
        assert len(tr24.calls) == 5, "the 15-minute heartbeat did not fire"
        hb24 = _unesc(tr24.texts[-1])
        assert "15-minute heartbeat" in hb24
        expected_unrealized = (
            (19.05 - 15.50) * cfg24.lot_size * 2) - 57.67 - (57.67 * 0.95)
        snap24 = eng._telegram_snapshot(sig24)
        assert abs(snap24["unrealized_pnl"] - expected_unrealized) < 0.01, \
            f"unrealised P&L {snap24['unrealized_pnl']} != {expected_unrealized}"
        assert abs(snap24["total_pnl"] - (348.14 + expected_unrealized)) < 0.01
        assert f"unrealised {_tr_signed(expected_unrealized)}" in hb24, hb24
        assert "Trade-2" in "\n".join(_pre_bodies(tr24.texts[-1])), \
            "the heartbeat did not carry the open trade"
        assert "Trade-1" not in "\n".join(_pre_bodies(tr24.texts[-1])), \
            "the heartbeat re-sent a trade that already had its own message"

        # (5) the engine stopped, with the reason, once
        assert eng._telegram_stop_reason() == "main loop ended"
        eng._signal_received = _signal24.SIGINT
        assert eng._telegram_stop_reason() == "signal SIGINT (Ctrl+C)"
        eng._signal_received = _signal24.SIGTERM
        assert eng._telegram_stop_reason() == "signal SIGTERM"
        eng._eod_done = True
        assert eng._telegram_stop_reason() == "end of day", \
            "the end-of-day reason must win over the signal that ended it"
        eng._eod_done = False
        eng._signal_received = _signal24.SIGINT
        eng._telegram_notify_stopped()
        assert len(tr24.calls) == 6, "the stop message was not sent"
        stop24 = _unesc(tr24.texts[-1])
        assert "Nifty options trade engine stopped at" in stop24
        assert "signal SIGINT (Ctrl+C)" in stop24
        stop_blocks = "\n".join(_pre_bodies(tr24.texts[-1]))
        assert "Trade-1" in stop_blocks and "Trade-2" in stop_blocks, \
            "the stop message must carry the whole day"
        eng._telegram_notify_stopped()
        assert len(tr24.calls) == 6, "the engine announced its stop twice"

        assert not log24.problems, f"the wiring logged problems: {log24.problems}"
        print("  [OK] start, heartbeat, placed, closed and stop all fire from")
        print("       MainEngine's own methods, once each, with the engine's")
        print("       own P&L and regime numbers")

    print_section("TELEGRAM REPORTER SELF-TEST COMPLETE", char="#")
    print("  All tests passed")
    print("  Live probe (needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in env.txt):")
    print("      python3 telegram_reporter.py --send-test")


def _print_status() -> int:
    """Show what the reporter would do with the current env.txt. Sends nothing."""
    config = load_config()
    rep = TelegramReporter(None, config, None, worker=False)
    print_section("TELEGRAM REPORTER - RESOLVED CONFIGURATION")
    for label, value in (
        ("enabled",            rep.enabled),
        ("bot token",          rep.masked_token),
        ("chat id",            rep.chat_id or "(unset)"),
        ("report chat id",     getattr(config, "telegram_report_chat_id", "") or "(same as alerts)"),
        ("heartbeat",          f"every {rep.heartbeat_sec / 60.0:.1f} min"),
        ("min gap between sends", f"{rep.min_gap:.2f}s"),
        ("timeout",            f"{rep.timeout:.1f}s"),
        ("max queued updates", rep.max_queue),
        ("parse mode",         rep.parse_mode or "(plain text)"),
        ("trade block style",  rep.block_style),
        ("open blocks in heartbeat", rep.include_open_blocks),
        ("requests available", requests is not None),
    ):
        print(f"  {label:<26}: {value}")
    print()
    print(f"  {rep.describe()}")
    return 0 if rep.enabled else 1


def _live_send_test() -> int:
    """Send one real message. The only test that needs the network."""
    config = load_config()
    rep = TelegramReporter(None, config, None, worker=False)
    print_section("TELEGRAM REPORTER - LIVE SEND TEST")
    print(f"  {rep.describe()}")
    if not rep.enabled:
        print("  Nothing to send: configure TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID in env.txt")
        return 1
    snap = rep.session_snapshot(cycles=0)
    text = rep.compose_status(KIND_START, snap)
    print(f"  sending {len(text)} character(s) to chat {rep.chat_id} ...")
    ok = rep.send(text, KIND_START)
    print(f"  delivered: {ok}")
    if not ok:
        print(f"  last error: {rep.last_error}")
        print("  A 401 means the bot token is wrong; a 400 usually means the "
              "chat id is; a 403 means the bot was never added to that chat.")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--send-test" in argv:
        return _live_send_test()
    if "--status" in argv:
        return _print_status()
    _self_test()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
