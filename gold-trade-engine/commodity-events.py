#!/usr/bin/env python3
"""
Gold signal monitor
===================
Polls Trading Economics (calendar + /stream + /stream?c=commodity) every minute,
stores every NEW item in SQLite, asks an NVIDIA-hosted LLM (3 models, round robin
with fallback) for a gold impact / recommendation, stores the answer, prints a
short line on the console and sends a Telegram alert.

Setup:
    pip install playwright openai requests
    playwright install chromium
    repo-root env.txt must contain:
        NVIDIA_API_KEY=...
        TELEGRAM_STOCK_BOT_TOKEN=...
        TELEGRAM_STOCK_CHAT_ID=...

Usage (from repo root or this folder):
    python gold-trade-engine/commodity-events.py --check
    python gold-trade-engine/commodity-events.py --once
    python gold-trade-engine/commodity-events.py              # Ctrl-C for clean shutdown
    python gold-trade-engine/commodity-events.py --no-telegram
    python gold-trade-engine/commodity-events.py --alert-all

Files created next to this script: gold_signals.db (SQLite) and gold_signals.log.
Credentials always come from the repo-root env.txt. Scraped times stay UTC in the DB;
Telegram alerts display times in IST (UTC+5:30).
"""
import argparse
import html
import json
import logging
import os
import random
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import openai
import requests
from openai import OpenAI
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# =====================================================================
#  CONFIG
# =====================================================================
SCRIPT_DIR = Path(__file__).resolve().parent          # gold-trade-engine/
ROOT_DIR = SCRIPT_DIR.parent                          # algo-trading/

BASE = "https://tradingeconomics.com"
# Rotate among common desktop Chrome UAs so requests look less automated.
USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
]
NAV_TIMEOUT_MS = 25000
RETRIES = 3
# Randomized human-like pauses (seconds) between site hits.
DELAY_EVENT_S = (2.5, 8.0)         # between calendar event-page fetches
DELAY_STREAM_S = (3.0, 9.0)        # between news stream views
DELAY_CAL_RANGE_S = (2.0, 6.0)     # between calendar range cookies/pages
DELAY_RETRY_S = (2.0, 6.0)         # base retry backoff (scaled by attempt)
POLL_INTERVAL_S = 60               # nominal; actual wait is randomized around this
POLL_JITTER_S = (0.75, 1.6)        # multiply --interval by uniform(lo, hi)

DB_PATH = str(SCRIPT_DIR / "gold_signals.db")
LOG_PATH = str(SCRIPT_DIR / "gold_signals.log")
ENV_PATH = str(ROOT_DIR / "env.txt")
IST = timezone(timedelta(hours=5, minutes=30))        # India Standard Time (no DST)

# --- LLM (NVIDIA build.nvidia.com, OpenAI-compatible endpoint) ---
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
REQUEST_TIMEOUT_S = 90
MAX_AI_ATTEMPTS = 5                # per item; each attempt already tries up to 3 models
MAX_COOLDOWN_WAIT_S = 30
MODELS = [   # round-robin order; each one is also the fallback for the others
    {"name": "nvidia/nemotron-3-super-120b-a12b", "temperature": 0.2, "top_p": 1, "max_tokens": 4096},
    {"name": "openai/gpt-oss-20b",                "temperature": 0.2, "top_p": 1, "max_tokens": 4096},
    {"name": "meta/llama-3.2-90b-vision-instruct", "temperature": 0.2, "top_p": 1, "max_tokens": 1024},
]
SYSTEM_PROMPT = ("You are a precise gold-market news analyst. "
                 "Reply with one valid JSON object only: no markdown, no commentary.")
INCLUDE_NOW_IN_PROMPT = True       # adds "Current time (UTC)" under the news item so 'outdated' can be judged

# --- Telegram ---
ALERT_ON = {"Strong Buy", "Buy", "Sell", "Strong Sell"}   # --alert-all sends everything
ALERT_MAX_AGE_MIN = 120            # do not alert for news older than this (e.g. first-run backlog)
TELEGRAM_MAX_ATTEMPTS = 5

# --- News stream ---
# DOM pages are brittle after calendar visits; the site's own JSON feed is reliable.
STREAM_API = {
    "all": f"{BASE}/ws/stream.ashx",
    "commodity": f"{BASE}/ws/stream.ashx",
}
STREAM_API_EXTRA = {"all": {}, "commodity": {"c": "commodity"}}
STREAM_PAGE_SIZE = 20
MAX_STREAM_PAGES = 40
NEWS_WINDOW = timedelta(hours=24)
# Kept for optional DOM fallback
STREAM_VIEWS = {"all": f"{BASE}/stream", "commodity": f"{BASE}/stream?c=commodity"}
MAX_SCROLLS = 40
SCROLL_WAIT_MS = 8000
CAL_URL = f"{BASE}/calendar"
CAL_SETTLE_MS = 4500

PROMPT_TEMPLATE = """Analyze the supplied gold-market news item and return exactly these four fields:

1. **timestamp**: Original publication date and time, including timezone if available. Do not invent missing timestamps.
2. **summary**: One short sentence describing the news and its relevance to gold.
3. **impact**: Positive, Negative, or Neutral for gold *at this moment*, based on the supplied news.
4. **recommendation**: Strong Buy, Buy, Sell, Strong Sell, Neutral, or No Impact.

Base your assessment on the news and its likely impact on gold prices. Distinguish Positive/Negative news impact from an actual trading signal. If information is insufficient, contradictory, outdated, or lacks current market confirmation, return Neutral rather than inventing certainty. Use Strong Buy or Strong Sell only when evidence is exceptionally strong and corroborated by current market data. Use No Impact when the news has no meaningful effect on gold.

Return valid JSON only, with exactly these four fields:
{
  "timestamp": "",
  "summary": "",
  "impact": "Positive | Negative | Neutral",
  "recommendation": "Strong Buy | Buy | Sell | Strong Sell | Neutral | No Impact"
}

News item:
{{NEWS_ITEM}}"""

log = logging.getLogger("gold")

# =====================================================================
#  SMALL HELPERS
# =====================================================================
def utcnow():
    return datetime.now(timezone.utc)


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_ist(dt):
    """Format an aware/naive-UTC datetime as 'YYYY-MM-DD HH:MM:SS IST'."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def parse_dt(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def utc_text_to_ist(text):
    """Best-effort convert a UTC timestamp string to IST for Telegram display."""
    if not text:
        return "timestamp not provided"
    s = str(text).strip()
    if re.search(r"\bIST\b", s, re.I):
        return s
    cleaned = re.sub(r"\bUTC\b", "", s, flags=re.I).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    dt = parse_dt(cleaned)
    if dt is None:
        for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
                    "%d %b %Y %H:%M", "%d %B %Y %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if dt is None:
        return f"{s} (UTC)" if "UTC" not in s.upper() else s
    return fmt_ist(dt)


def telegram_timestamp(item, result):
    """Prefer normalised event_dt (UTC in DB) → IST; else convert LLM timestamp."""
    dt = parse_dt(item.get("event_dt"))
    if dt is not None:
        return fmt_ist(dt)
    return utc_text_to_ist(result.get("timestamp"))


def human_delay(lo_hi, stop=None, scale=1.0):
    """Sleep a random duration in [lo, hi] * scale; interruptible via stop Event."""
    lo, hi = lo_hi
    secs = max(0.1, random.uniform(lo, hi) * scale)
    if stop is not None:
        stop.wait(secs)
    else:
        time.sleep(secs)
    return secs


def mask(secret):
    if not secret:
        return "<missing>"
    return secret[:6] + "..." + f"({len(secret)} chars)"


def setup_logging(level):
    for stream in (sys.stdout, sys.stderr):      # emoji / non-ASCII safe on Windows consoles
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", "%H:%M:%S")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    console.setFormatter(fmt)
    fh = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
    log.addHandler(console)
    log.addHandler(fh)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def load_env(path):
    """Parse env.txt: KEY=value, KEY = "value", '#' comments, optional 'export '."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"env file not found: {p.resolve()}")
    env = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v.strip()
    return env


# =====================================================================
#  DATABASE
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source               TEXT NOT NULL CHECK (source IN ('calendar','news')),
    item_key             TEXT NOT NULL UNIQUE,      -- dedupe key (calendar: date|time|country|heading, news: site id)
    -- fields that feed the LLM prompt: {{date}} {{time}} | {{country}} | {{heading}} | {{summary}} | {{url}}
    date                 TEXT,                      -- UTC YYYY-MM-DD
    time                 TEXT,                      -- calendar: '06:00 AM'; news: 'HH:MM:SS' (UTC)
    country              TEXT,
    heading              TEXT NOT NULL,
    summary              TEXT,
    url                  TEXT,
    -- extra scrape data
    event_dt             TEXT,                      -- normalised UTC datetime, used for age checks / ordering
    published_at         TEXT,                      -- news only (estimated from 'x minutes ago')
    age_text             TEXT,
    streams              TEXT,                      -- news only: all / commodity / all,commodity
    importance           INTEGER,                   -- news only: 1-3
    topic                TEXT,
    news_id              TEXT,
    scraped_at           TEXT NOT NULL,
    -- AI analysis
    ai_status            TEXT NOT NULL DEFAULT 'pending' CHECK (ai_status IN ('pending','done','failed')),
    ai_analysis          TEXT,                      -- validated JSON returned by the LLM (4 fields)
    ai_timestamp         TEXT,
    ai_summary           TEXT,
    ai_impact            TEXT,                      -- Positive / Negative / Neutral
    ai_recommendation    TEXT,                      -- Strong Buy / Buy / Sell / Strong Sell / Neutral / No Impact
    ai_model             TEXT,
    ai_attempts          INTEGER NOT NULL DEFAULT 0,
    ai_error             TEXT,
    ai_next_try_at       TEXT,
    analyzed_at          TEXT,
    -- Telegram
    telegram_status      TEXT,                      -- sent / retry / failed / skipped_filter / skipped_old / skipped_disabled
    telegram_attempts    INTEGER NOT NULL DEFAULT 0,
    telegram_next_try_at TEXT,
    telegram_sent_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_ai      ON items(ai_status, ai_next_try_at);
CREATE INDEX IF NOT EXISTS idx_items_dt      ON items(event_dt);
CREATE INDEX IF NOT EXISTS idx_items_reco    ON items(ai_recommendation);
CREATE INDEX IF NOT EXISTS idx_items_tg      ON items(telegram_status, telegram_next_try_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_news_id
    ON items(news_id) WHERE source='news' AND news_id IS NOT NULL AND news_id != '';

-- one row per LLM call (success or failure): for troubleshooting and model statistics
CREATE TABLE IF NOT EXISTS ai_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    status       TEXT NOT NULL,                     -- ok / rate_limited / timeout / bad_output / ...
    latency_ms   INTEGER,
    error        TEXT,
    raw_response TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_item ON ai_runs(item_id);
"""

INSERT_COLS = ["source", "item_key", "date", "time", "country", "heading", "summary", "url",
               "event_dt", "published_at", "age_text", "streams", "importance", "topic", "news_id"]


class Store:
    """SQLite wrapper; one connection per thread (scraper thread + AI worker thread)."""

    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self._conns = set()
        self._lock = threading.Lock()
        self._closed = False
        c = self.conn()
        c.executescript(SCHEMA)
        c.commit()

    def conn(self):
        if self._closed:
            raise RuntimeError("Store is closed")
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            with self._lock:
                self._conns.add(c)
            self._local.c = c
        return c

    def close_thread(self):
        """Commit and close this thread's connection (call from worker on exit)."""
        c = getattr(self._local, "c", None)
        if c is None:
            return
        try:
            c.commit()
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass
        with self._lock:
            self._conns.discard(c)
        self._local.c = None

    def close(self):
        """Close every tracked connection cleanly (Ctrl-C / shutdown path)."""
        self._closed = True
        self.close_thread()
        with self._lock:
            leftover = list(self._conns)
            self._conns.clear()
        for c in leftover:
            try:
                c.commit()
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
        log.debug("[DB] all connections closed")

    # Telegram outcomes that must never be repeated on restart / re-fetch.
    TG_FINAL = frozenset({"sent", "skipped_filter", "skipped_old", "skipped_disabled", "failed"})

    def exists(self, key):
        return self.find_by_key(key) is not None

    def find_by_key(self, key):
        if not key:
            return None
        row = self.conn().execute("SELECT * FROM items WHERE item_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def find_existing(self, item):
        """Return the stored row if this scrape corresponds to an item already in the DB.

        Matches by item_key, then news_id (news), then normalised calendar fields so a
        restart re-fetch cannot create a second row / second AI / second Telegram send.
        """
        if not item:
            return None
        key = item.get("item_key")
        hit = self.find_by_key(key)
        if hit:
            return hit

        source = item.get("source")
        news_id = str(item.get("news_id") or "").strip()
        if source == "news" and news_id:
            row = self.conn().execute(
                "SELECT * FROM items WHERE source='news' AND news_id=? AND news_id!='' LIMIT 1",
                (news_id,)).fetchone()
            if row:
                return dict(row)

        if source == "calendar":
            row = self.conn().execute(
                "SELECT * FROM items WHERE source='calendar' AND date=? AND IFNULL(time,'')=? "
                "AND lower(IFNULL(country,''))=lower(?) AND lower(IFNULL(heading,''))=lower(?) LIMIT 1",
                (item.get("date") or "", item.get("time") or "",
                 item.get("country") or "", item.get("heading") or "")).fetchone()
            if row:
                return dict(row)
        return None

    def insert_item(self, item):
        """Insert only if never seen before. Returns new id, or None if duplicate."""
        existing = self.find_existing(item)
        if existing:
            log.debug(
                "[DB] skip duplicate (already #%d ai=%s tg=%s): %s",
                existing["id"], existing.get("ai_status"), existing.get("telegram_status"),
                (existing.get("heading") or item.get("heading") or "")[:70])
            return None
        params = {k: item.get(k) for k in INSERT_COLS}
        params["scraped_at"] = fmt_dt(utcnow())
        sql = (f"INSERT OR IGNORE INTO items ({','.join(INSERT_COLS)},scraped_at) "
               f"VALUES ({','.join(':' + k for k in INSERT_COLS)},:scraped_at)")
        with self.conn() as c:
            cur = c.execute(sql, params)
            if not cur.rowcount:
                # Race: another thread inserted between find_existing and INSERT.
                log.debug("[DB] INSERT OR IGNORE skipped race duplicate: %s", item.get("item_key"))
                return None
            return cur.lastrowid

    def get(self, item_id):
        row = self.conn().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def claim_pending(self, item_id):
        """Lease a pending row for AI so the same id is not analyzed twice concurrently."""
        lease_until = fmt_dt(utcnow() + timedelta(minutes=15))
        with self.conn() as c:
            cur = c.execute(
                "UPDATE items SET ai_next_try_at=? WHERE id=? AND ai_status='pending' "
                "AND (ai_next_try_at IS NULL OR ai_next_try_at<=?)",
                (lease_until, item_id, fmt_dt(utcnow())))
            return cur.rowcount > 0

    def next_pending(self):
        row = self.conn().execute(
            "SELECT * FROM items WHERE ai_status='pending' AND (ai_next_try_at IS NULL OR ai_next_try_at<=?) "
            "ORDER BY COALESCE(event_dt, scraped_at) DESC, id DESC LIMIT 1", (fmt_dt(utcnow()),)).fetchone()
        return dict(row) if row else None

    def next_alert_retry(self):
        row = self.conn().execute(
            "SELECT * FROM items WHERE ai_status='done' AND telegram_status='retry' "
            "AND (telegram_next_try_at IS NULL OR telegram_next_try_at<=?) ORDER BY id LIMIT 1",
            (fmt_dt(utcnow()),)).fetchone()
        return dict(row) if row else None

    def open_work(self):
        c = self.conn()
        a = c.execute("SELECT COUNT(*) FROM items WHERE ai_status='pending'").fetchone()[0]
        b = c.execute("SELECT COUNT(*) FROM items WHERE ai_status='done' AND telegram_status='retry'").fetchone()[0]
        return a + b

    def counts(self):
        rows = self.conn().execute("SELECT ai_status, COUNT(*) n FROM items GROUP BY ai_status").fetchall()
        return {r["ai_status"]: r["n"] for r in rows}

    def mark_ai_done(self, item_id, result, model, attempts):
        """Mark done only if still pending — never overwrite a completed analysis."""
        with self.conn() as c:
            cur = c.execute(
                "UPDATE items SET ai_status='done', ai_analysis=?, ai_timestamp=?, ai_summary=?, ai_impact=?, "
                "ai_recommendation=?, ai_model=?, ai_attempts=?, ai_error=NULL, ai_next_try_at=NULL, analyzed_at=? "
                "WHERE id=? AND ai_status='pending'",
                (json.dumps(result, ensure_ascii=False), result["timestamp"], result["summary"], result["impact"],
                 result["recommendation"], model, attempts, fmt_dt(utcnow()), item_id))
            return cur.rowcount > 0

    def mark_ai_failed(self, item_id, error, attempts, give_up, next_try):
        with self.conn() as c:
            c.execute(
                "UPDATE items SET ai_status=?, ai_error=?, ai_attempts=?, ai_next_try_at=? "
                "WHERE id=? AND ai_status='pending'",
                ("failed" if give_up else "pending", error[:1000], attempts,
                 None if give_up else fmt_dt(next_try), item_id))

    def log_run(self, item_id, run):
        with self.conn() as c:
            c.execute("INSERT INTO ai_runs (item_id,model,status,latency_ms,error,raw_response,created_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (item_id, run["model"], run["status"], run.get("latency_ms"),
                       (run.get("error") or "")[:1000], (run.get("raw") or "")[:8000], fmt_dt(utcnow())))

    def set_telegram(self, item_id, status, attempts, next_try=None, sent=False):
        """Update Telegram state only when not already in a final terminal status."""
        with self.conn() as c:
            cur = c.execute(
                "UPDATE items SET telegram_status=?, telegram_attempts=?, telegram_next_try_at=?, "
                "telegram_sent_at=? WHERE id=? AND (telegram_status IS NULL OR telegram_status='retry')",
                (status, attempts, fmt_dt(next_try) if next_try else None,
                 fmt_dt(utcnow()) if sent else None, item_id))
            return cur.rowcount > 0


# =====================================================================
#  LLM LAYER
# =====================================================================
class LLMFormatError(Exception):
    """Model answered, but not with the required JSON."""


class AllModelsFailed(Exception):
    def __init__(self, runs):
        self.runs = runs
        super().__init__("; ".join(f"{r['model'].split('/')[-1]}: {r['status']} ({(r.get('error') or '')[:80]})"
                                   for r in runs) or "no model available")


IMPACTS = {"positive": "Positive", "negative": "Negative", "neutral": "Neutral"}
RECS = {"strong buy": "Strong Buy", "buy": "Buy", "sell": "Sell", "strong sell": "Strong Sell",
        "neutral": "Neutral", "no impact": "No Impact"}
REQUIRED = ("timestamp", "summary", "impact", "recommendation")


def parse_llm_json(text):
    """Extract and validate the 4-field JSON from a model reply. Raises LLMFormatError."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise LLMFormatError("no JSON object in reply")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMFormatError(f"invalid JSON: {e}")
    if not isinstance(obj, dict):
        raise LLMFormatError("JSON is not an object")
    lower = {str(k).lower(): v for k, v in obj.items()}
    missing = [k for k in REQUIRED if k not in lower]
    if missing:
        raise LLMFormatError(f"missing fields: {missing}")
    impact = IMPACTS.get(str(lower["impact"]).strip().lower())
    rec = RECS.get(str(lower["recommendation"]).strip().lower())
    if impact is None:
        raise LLMFormatError(f"bad impact value: {lower['impact']!r}")
    if rec is None:
        raise LLMFormatError(f"bad recommendation value: {lower['recommendation']!r}")
    extra = set(lower) - set(REQUIRED)
    if extra:
        log.debug("[AI] ignoring extra fields from model: %s", sorted(extra))
    result = {"timestamp": str(lower["timestamp"] or "").strip(),
              "summary": str(lower["summary"] or "").strip(),
              "impact": impact, "recommendation": rec}
    if (impact == "Negative" and rec in ("Buy", "Strong Buy")) or (impact == "Positive" and rec in ("Sell", "Strong Sell")):
        log.warning("[AI] model output looks contradictory: impact=%s recommendation=%s", impact, rec)
    return result


def build_prompt(item):
    t = (item.get("time") or "").strip()
    if t and "UTC" not in t.upper():
        t += " UTC"
    line = " | ".join([
        f"{item.get('date') or ''} {t}".strip(), item.get("country") or "", item.get("heading") or "",
        item.get("summary") or "", item.get("url") or ""])
    line = re.sub(r"\s+", " ", line)
    prompt = PROMPT_TEMPLATE.replace("{{NEWS_ITEM}}", line)
    if INCLUDE_NOW_IN_PROMPT:
        prompt += f"\n\nCurrent time (UTC): {fmt_dt(utcnow())}"
    return prompt


def classify_error(e):
    """-> (status label, cooldown seconds for that model)"""
    if isinstance(e, LLMFormatError):
        return "bad_output", 0
    if isinstance(e, openai.RateLimitError):
        retry = None
        try:
            retry = float(e.response.headers.get("retry-after"))
        except Exception:
            pass
        return "rate_limited", retry or 60
    if isinstance(e, openai.APITimeoutError):
        return "timeout", 20
    if isinstance(e, openai.APIConnectionError):
        return "connection", 20
    if isinstance(e, openai.APIStatusError):
        code = e.status_code
        if code in (401, 403):
            return "auth", 300
        if code == 404:
            return "model_not_found", 3600
        if code >= 500:
            return f"http_{code}", 30
        return f"http_{code}", 20
    return "error", 20


class LLMRouter:
    """Round-robin over MODELS; if a model fails, the next one is tried (fallback)."""

    def __init__(self, api_key, models, stop):
        self.client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, timeout=REQUEST_TIMEOUT_S, max_retries=0)
        self.models = models
        self.stop = stop
        self._i = 0
        self._lock = threading.Lock()
        self.cooldown = {}          # model name -> unix time until which it is skipped

    def _order(self):
        with self._lock:
            start = self._i
            self._i = (self._i + 1) % len(self.models)
        return [self.models[(start + k) % len(self.models)] for k in range(len(self.models))]

    def call_model(self, m, prompt):
        """One chat completion; returns raw content text."""
        comp = self.client.chat.completions.create(
            model=m["name"],
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            temperature=m["temperature"], top_p=m["top_p"], max_tokens=m["max_tokens"], stream=False)
        ch = comp.choices[0]
        content = (ch.message.content or "").strip()
        reasoning = getattr(ch.message, "reasoning_content", None) or ""
        log.debug("[AI] %s finish_reason=%s content_chars=%d reasoning_chars=%d",
                  m["name"], ch.finish_reason, len(content), len(reasoning))
        if not content:
            raise LLMFormatError(f"empty content (finish_reason={ch.finish_reason}, reasoning_chars={len(reasoning)})")
        return content

    def analyze(self, item):
        order = self._order()
        now = time.time()
        ready = [m for m in order if self.cooldown.get(m["name"], 0) <= now]
        skipped = [m["name"] for m in order if m not in ready]
        if skipped:
            log.debug("[AI] skipping models in cooldown: %s", skipped)
        if not ready:
            wait = max(0.0, min(min(self.cooldown[m["name"]] for m in order) - now, MAX_COOLDOWN_WAIT_S))
            log.warning("[AI] every model is cooling down; waiting %.0fs", wait)
            self.stop.wait(wait)
            ready = order
        prompt = build_prompt(item)
        log.debug("[AI] prompt chars=%d | item line: %s", len(prompt), prompt.split("News item:\n")[-1][:200])
        runs = []
        for m in ready:
            if self.stop.is_set():
                break
            t0 = time.time()
            log.debug("[AI] -> calling %s", m["name"])
            raw = ""
            try:
                raw = self.call_model(m, prompt)
                result = parse_llm_json(raw)
            except Exception as e:
                status, cd = classify_error(e)
                if cd:
                    self.cooldown[m["name"]] = time.time() + cd
                ms = int((time.time() - t0) * 1000)
                err = f"{type(e).__name__}: {e}"
                log.warning("[AI] %s failed (%s, %dms): %s%s", m["name"], status, ms, str(e)[:200],
                            f" | cooldown {cd:.0f}s" if cd else "")
                runs.append({"model": m["name"], "status": status, "latency_ms": ms, "error": err, "raw": raw})
                continue
            ms = int((time.time() - t0) * 1000)
            runs.append({"model": m["name"], "status": "ok", "latency_ms": ms, "error": "", "raw": raw})
            return {"result": result, "model": m["name"], "latency_ms": ms, "runs": runs}
        raise AllModelsFailed(runs)


# =====================================================================
#  TELEGRAM
# =====================================================================
class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id
        self.base = f"https://api.telegram.org/bot{token}"

    def _scrub(self, s):
        return str(s).replace(self.token, "<token>")

    def _post(self, method, data):
        try:
            r = requests.post(f"{self.base}/{method}", data=data, timeout=20)
            body = r.json() if r.content else {}
            if r.status_code == 200 and body.get("ok"):
                return True, body
            return False, f"HTTP {r.status_code}: {body.get('description') or r.text[:200]}"
        except Exception as e:
            return False, self._scrub(f"{type(e).__name__}: {e}")

    def send(self, text):
        ok, info = self._post("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                              "disable_web_page_preview": "true"})
        return ok, ("" if ok else info)

    def get_me(self):
        return self._post("getMe", {})


REC_ICON = {"Strong Buy": "🟢🟢", "Buy": "🟢", "Sell": "🔴", "Strong Sell": "🔴🔴", "Neutral": "⚪", "No Impact": "⚫"}


def build_telegram_message(item, result, model):
    e = html.escape
    rec = result["recommendation"]
    ts_ist = telegram_timestamp(item, result)
    lines = [f"{REC_ICON.get(rec, '')} <b>GOLD SIGNAL: {e(rec.upper())}</b>",
             f"Impact on gold: <b>{e(result['impact'])}</b>",
             "",
             e(result["summary"] or "(no summary returned)"),
             "",
             f"🕒 {e(ts_ist)}",
             f"📰 {e(item.get('heading') or '')}"]
    if item.get("country"):
        lines.append(f"🌍 {e(item['country'])} · {e(item.get('source') or '')}")
    if item.get("url"):
        lines.append(f"🔗 <a href=\"{html.escape(item['url'], quote=True)}\">source</a>")
    lines.append(f"🤖 {e(model)}")
    return "\n".join(lines)[:4000]


def short_line(item, result, model, secs):
    s = result["summary"]
    s = s if len(s) <= 110 else s[:107] + "..."
    return (f"#{item['id']} [{result['recommendation'].upper():<11}] {result['impact']:<8} | {s} | "
            f"{(item.get('heading') or '')[:50]} | {model.split('/')[-1]} {secs:.1f}s")


def item_age_minutes(item):
    dt = parse_dt(item.get("event_dt"))
    return 0.0 if dt is None else (utcnow() - dt).total_seconds() / 60


# =====================================================================
#  AI WORKER THREAD  (analysis -> DB -> console -> Telegram)
# =====================================================================
class Worker(threading.Thread):
    def __init__(self, store, router, telegram, stop, wake, alert_all=False):
        super().__init__(name="ai-worker", daemon=True)
        self.store, self.router, self.telegram = store, router, telegram
        self.stop_evt, self.wake, self.alert_all = stop, wake, alert_all

    def run(self):
        log.info("AI worker started")
        try:
            while not self.stop_evt.is_set():
                try:
                    busy = self.process_one()
                except RuntimeError as e:
                    if "Store is closed" in str(e):
                        break
                    log.exception("AI worker crashed on an item; continuing")
                    busy = False
                    self.stop_evt.wait(5)
                except Exception:
                    log.exception("AI worker crashed on an item; continuing")
                    busy = False
                    self.stop_evt.wait(5)
                if not busy:
                    self.wake.wait(5)
                    self.wake.clear()
        finally:
            self.store.close_thread()
            log.info("AI worker stopped")

    def process_one(self):
        item = self.store.next_pending()
        if item:
            self.analyze_item(item)
            return True
        retry = self.store.next_alert_retry()
        if retry:
            # Only genuine send failures are retried — never re-alert final statuses.
            if retry.get("telegram_status") in Store.TG_FINAL:
                return False
            result = json.loads(retry["ai_analysis"])
            self.send_alert(retry, result, retry["ai_model"])
            return True
        return False

    def analyze_item(self, item):
        iid = item["id"]
        fresh = self.store.get(iid) or item
        if fresh.get("ai_status") != "pending":
            log.debug("[AI] skip #%d — already ai_status=%s (no re-analysis)", iid, fresh.get("ai_status"))
            return
        if not self.store.claim_pending(iid):
            log.debug("[AI] skip #%d — could not claim (already leased or done)", iid)
            return
        attempts = int(fresh.get("ai_attempts") or 0) + 1
        log.info("[AI] analyzing #%d (%s, attempt %d/%d): %s", iid, fresh["source"], attempts, MAX_AI_ATTEMPTS,
                 (fresh["heading"] or "")[:80])
        t0 = time.time()
        try:
            out = self.router.analyze(fresh)
        except AllModelsFailed as e:
            for run in e.runs:
                self.store.log_run(iid, run)
            give_up = attempts >= MAX_AI_ATTEMPTS
            nxt = utcnow() + timedelta(seconds=min(300, 30 * attempts))
            self.store.mark_ai_failed(iid, str(e), attempts, give_up, nxt)
            log.error("[AI] #%d all models failed (%s). %s", iid, e,
                      "GIVING UP." if give_up else f"retry after {fmt_dt(nxt)} UTC")
            return
        for run in out["runs"]:
            self.store.log_run(iid, run)
        result, model = out["result"], out["model"]
        if not self.store.mark_ai_done(iid, result, model, attempts):
            log.warning("[AI] #%d analysis discarded — row was no longer pending (duplicate avoided)", iid)
            return
        log.debug("[DB] #%d analysis stored: %s", iid, json.dumps(result, ensure_ascii=False))
        log.info("[SIGNAL] %s", short_line(fresh, result, model, time.time() - t0))
        self.maybe_alert(self.store.get(iid), result, model)

    def maybe_alert(self, item, result, model):
        if not item:
            return
        iid = item["id"]
        fresh = self.store.get(iid) or item
        st = fresh.get("telegram_status")
        if st in Store.TG_FINAL:
            log.debug("[TG] #%d skip — already telegram_status=%s (no duplicate send)", iid, st)
            return
        rec = result["recommendation"]
        if self.telegram is None:
            status = "skipped_disabled"
        elif not self.alert_all and rec not in ALERT_ON:
            status = "skipped_filter"
        elif item_age_minutes(fresh) > ALERT_MAX_AGE_MIN:
            status = "skipped_old"
        else:
            self.send_alert(fresh, result, model)
            return
        if not self.store.set_telegram(iid, status, 0):
            log.debug("[TG] #%d skip status write — already finalised", iid)
            return
        log.debug("[TG] #%d not sent: %s (recommendation=%s, age=%.0f min)", iid, status, rec, item_age_minutes(fresh))

    def send_alert(self, item, result, model):
        iid = item["id"]
        fresh = self.store.get(iid) or item
        st = fresh.get("telegram_status")
        if st in Store.TG_FINAL:
            log.debug("[TG] #%d skip send — already %s", iid, st)
            return
        attempts = int(fresh.get("telegram_attempts") or 0) + 1
        msg = build_telegram_message(fresh, result, model)
        log.debug("[TG] #%d sending (attempt %d), %d chars", iid, attempts, len(msg))
        ok, err = self.telegram.send(msg)
        if ok:
            if self.store.set_telegram(iid, "sent", attempts, sent=True):
                log.info("[TG] #%d alert sent: %s", iid, result["recommendation"])
            else:
                log.debug("[TG] #%d send OK but DB already final — duplicate suppressed", iid)
        elif attempts >= TELEGRAM_MAX_ATTEMPTS:
            self.store.set_telegram(iid, "failed", attempts)
            log.error("[TG] #%d giving up after %d attempts: %s", iid, attempts, err)
        else:
            self.store.set_telegram(iid, "retry", attempts, next_try=utcnow() + timedelta(seconds=30 * attempts))
            log.warning("[TG] #%d send failed (attempt %d): %s", iid, attempts, err)


# =====================================================================
#  SCRAPING
# =====================================================================
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
AGE_RE = re.compile(r"(\d+)\s+(second|minute|hour|day)s?\s+ago", re.I)
UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

JS_PARSE_ROWS = """
() => [...document.querySelectorAll('table#calendar tr[data-url]')].map(tr => {
  const a = tr.querySelector('a.calendar-event');
  const td = tr.querySelector('td');
  const t = tr.querySelector('span[class*="calendar-date"]');
  return {
    url: tr.getAttribute('data-url') || '',
    heading: (a ? a.innerText : '').trim() || (tr.getAttribute('data-event') || '').trim(),
    country: (tr.getAttribute('data-country') || '').trim(),
    time: t ? t.innerText.trim() : '',
    td_class: td ? (td.getAttribute('class') || '') : '',
  };
})
"""

JS_NEWS = """
() => [...document.querySelectorAll('#stream li.te-stream-item')].map(li => {
  const title = li.querySelector('a[class^="te-stream-title"]');
  const q = s => { const e = li.querySelector(s); return e ? e.textContent.trim() : ''; };
  // Normal items keep their text in span.te-stream-item-description. Auto-generated
  // "... Updates" items put it straight inside the <li>, so fall back to that.
  let text = q('span.te-stream-item-description');
  if (!text) {
    const c = li.cloneNode(true);
    c.querySelectorAll('.te-stream-title-div, small.te-stream-date').forEach(e => e.remove());
    text = c.textContent.trim();
  }
  return {
    id: li.id || '',
    cls: li.className || '',
    headline: title ? title.textContent.trim() : '',
    url: title ? (title.getAttribute('href') || '') : '',
    country: q('a.te-stream-country'),
    topic: q('a.te-stream-category'),
    text: text,
    age: q('small.te-stream-date'),
  };
})
"""


def parse_age(text):
    """'98 seconds ago' -> timedelta; ISO dates (>30 days old) -> very old; unknown -> None."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if "few seconds" in s:
        return timedelta(0)
    m = AGE_RE.search(s)
    if m:
        return timedelta(seconds=int(m.group(1)) * UNIT_SECONDS[m.group(2).lower()])
    if DATE_RE.match(s):
        return timedelta(days=999)
    return None


def importance_from_class(cls):
    if "te-stream-item-3" in cls:
        return 3
    if "te-stream-item-2" in cls:
        return 2
    return 1


def news_key(news_id, url, headline):
    return f"news|{news_id}" if news_id else f"news|{url}|{headline}"


def calendar_key(row):
    """Stable calendar dedupe key (normalised case so restarts cannot re-insert)."""
    return (f"cal|{row.get('date') or ''}|"
            f"{(row.get('time') or '').strip()}|"
            f"{(row.get('country') or '').strip().lower()}|"
            f"{(row.get('heading') or '').strip().lower()}")


def calendar_event_dt(date, time_str):
    """Calendar 'date' + '06:00 AM' (UTC) -> 'YYYY-MM-DD HH:MM:SS'. No time -> midnight."""
    try:
        base = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None
    for f in ("%I:%M %p", "%H:%M"):
        try:
            t = datetime.strptime((time_str or "").strip().upper(), f)
            return fmt_dt(base + timedelta(hours=t.hour, minutes=t.minute))
        except ValueError:
            continue
    return fmt_dt(base)


def parse_api_dt(s):
    """Parse Trading Economics stream.ashx date (UTC, usually no tz suffix)."""
    if not s:
        return None
    text = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(str(s).strip().rstrip("Z"), fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_news_items(collected):
    """{view: [api or DOM raw]} -> DB-ready dicts for items in the last 24h, merged across views."""
    merged = {}
    now = utcnow()
    for view, items in collected.items():
        for r in items:
            # API rows carry an aware UTC published_dt; DOM rows carry relative age text.
            if r.get("published_dt") is not None:
                published_dt = r["published_dt"]
                age = now - published_dt
                age_text = r.get("age") or f"{int(max(0, age.total_seconds()))} seconds ago"
                if age > NEWS_WINDOW or age < timedelta(hours=-1):
                    continue
                published = fmt_dt(published_dt)
                importance = int(r.get("importance") or 0) or 1
                country = r.get("country") or ""
                topic = r.get("topic") or r.get("category") or ""
                headline = r.get("headline") or r.get("title") or ""
                text = r.get("text") or r.get("description") or ""
                href = r.get("url") or ""
                news_id = str(r.get("id") or r.get("news_id") or "")
                key = r.get("key") or news_key(news_id, href, headline)
            else:
                age = parse_age(r.get("age"))
                if age is None or age > NEWS_WINDOW:
                    continue
                published = fmt_dt(r["seen_at"] - age)
                age_text = r.get("age") or ""
                importance = importance_from_class(r.get("cls") or "")
                country = r.get("country") or ""
                topic = r.get("topic") or ""
                headline = r.get("headline") or ""
                text = r.get("text") or ""
                href = r.get("url") or ""
                news_id = r.get("id") or ""
                key = r.get("key") or news_key(news_id, href, headline)

            row = merged.get(key)
            if row is None:
                country_disp = country.title() if country.islower() else country
                row = {
                    "source": "news", "item_key": key,
                    "date": published[:10], "time": published[11:19],
                    "country": country_disp, "heading": headline,
                    "summary": " ".join(str(text).split()),
                    "url": href if str(href).startswith("http") else (BASE + href if href else ""),
                    "event_dt": published, "published_at": published, "age_text": age_text,
                    "streams": [], "importance": importance,
                    "topic": topic, "news_id": str(news_id),
                }
                merged[key] = row
            if view not in row["streams"]:
                row["streams"].append(view)
    rows = list(merged.values())
    for row in rows:
        row["streams"] = ",".join(row["streams"])
    rows.sort(key=lambda x: x["event_dt"])
    return rows


# Playwright init script — reduce obvious automation fingerprints.
STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
  get: () => [{ name: 'Chrome PDF Plugin' }, { name: 'Chrome PDF Viewer' }, { name: 'Native Client' }],
});
"""


def _browser_headers(ua):
    """Realistic navigation headers matching a normal Chrome session."""
    # Keep Sec-CH-UA roughly aligned with the UA string version when possible.
    m = re.search(r"Chrome/(\d+)", ua)
    ver = m.group(1) if m else "124"
    platform = '"macOS"' if "Macintosh" in ua else '"Windows"'
    return {
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": f'"Chromium";v="{ver}", "Google Chrome";v="{ver}", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": platform,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


class Scraper:
    def __init__(self, headed=False, stop=None):
        self.headed = headed
        self.stop = stop or threading.Event()
        self.pw = sync_playwright().start()
        self._launch()

    def _stopped(self):
        return self.stop.is_set()

    def _pause(self, lo_hi, scale=1.0):
        human_delay(lo_hi, stop=self.stop, scale=scale)

    def _launch(self):
        ua = random.choice(USER_AGENTS)
        width = random.randint(1280, 1600)
        height = random.randint(800, 1000)
        self.browser = self.pw.chromium.launch(
            headless=not self.headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-default-browser-check",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
        )
        # timezone_id="UTC" matters: the stream page computes "x minutes ago" in the browser timezone.
        self.ctx = self.browser.new_context(
            user_agent=ua,
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": width, "height": height},
            extra_http_headers=_browser_headers(ua),
            java_script_enabled=True,
            bypass_csp=False,
        )
        self.ctx.add_init_script(STEALTH_INIT_JS)
        chrome_ver = re.search(r"Chrome/([\d.]+)", ua)
        log.debug("[SCRAPE] browser started (headless=%s, chrome=%s, viewport=%dx%d)",
                  not self.headed, chrome_ver.group(1) if chrome_ver else "?", width, height)

    def restart(self):
        log.warning("[SCRAPE] restarting browser")
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        self._warmed = False
        self._launch()

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass

    # ---------------- calendar ----------------
    def _calendar_rows(self, range_code):
        base = {"domain": ".tradingeconomics.com", "path": "/"}
        self.ctx.add_cookies([{**base, "name": "calendar-range", "value": range_code},
                              {**base, "name": "cal-timezone-offset", "value": "0"}])
        page = self.ctx.new_page()
        try:
            resp = page.goto(CAL_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp else None
            log.debug("[SCRAPE] calendar range=%s HTTP %s", range_code, status)
            if status and status >= 400:
                raise RuntimeError(f"calendar returned HTTP {status} (blocked?)")
            page.wait_for_timeout(int(CAL_SETTLE_MS * random.uniform(0.85, 1.35)))
            try:
                page.wait_for_selector("table#calendar tr[data-url]", timeout=10000)
            except Exception:
                log.warning("[SCRAPE] no event rows for range=%s", range_code)
                return []
            raw = page.evaluate(JS_PARSE_ROWS)
        finally:
            page.close()
        rows = []
        for r in raw:
            m = DATE_RE.search(r["td_class"])
            if r["url"] and m:
                rows.append({"date": m.group(0), "time": r["time"], "country": r["country"],
                             "heading": r["heading"], "url": r["url"]})
        log.debug("[SCRAPE] calendar range=%s: %d rows parsed", range_code, len(rows))
        return rows

    def _fetch_summary(self, url):
        """-> (summary, state) with state ok | none (no #description) | error"""
        full = url if url.startswith("http") else BASE + url
        last = None
        for attempt in range(1, RETRIES + 1):
            if self._stopped():
                return "", "error"
            page = self.ctx.new_page()
            try:
                resp = page.goto(full, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                if resp and resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                try:
                    page.wait_for_selector("#description", state="attached", timeout=8000)
                except PWTimeout:
                    return "", "none"
                el = page.locator("#description").first
                text = el.inner_text(timeout=4000) or el.text_content() or ""
                return " ".join(text.split()), "ok"
            except Exception as e:
                last = e
                log.debug("[SCRAPE] event page attempt %d failed: %s", attempt, e)
                self._pause(DELAY_RETRY_S, scale=attempt)
            finally:
                page.close()
        log.warning("[SCRAPE] event page failed after %d tries: %s", RETRIES, last)
        return "", "error"

    def calendar_items(self, store, range_codes):
        """Yield DB-ready dicts for calendar events (today/yesterday UTC) not yet in the DB."""
        today = utcnow().date()
        wanted = {str(today), str(today - timedelta(days=1))}
        rows = []
        for i, code in enumerate(range_codes):
            if self._stopped():
                return
            if i:
                self._pause(DELAY_CAL_RANGE_S)
            rows.extend(self._calendar_rows(code))
        seen, new = set(), []
        for r in rows:
            if r["date"] not in wanted:
                continue
            key = calendar_key(r)
            if key in seen:
                continue
            seen.add(key)
            candidate = {"source": "calendar", "item_key": key, "date": r["date"], "time": r["time"],
                         "country": r["country"], "heading": r["heading"]}
            if store.find_existing(candidate) is None:
                new.append((key, r))
        log.debug("[SCRAPE] calendar: %d events in window, %d new", len(seen), len(new))
        for key, r in new:
            if self._stopped():
                return
            # Re-check immediately before expensive event-page fetch (handles restart races).
            candidate = {"source": "calendar", "item_key": key, "date": r["date"], "time": r["time"],
                         "country": r["country"], "heading": r["heading"]}
            if store.find_existing(candidate) is not None:
                continue
            summary, state = self._fetch_summary(r["url"])
            if state == "error":
                log.warning("[SCRAPE] skipping '%s' for now (will retry next cycle)", r["heading"])
                continue
            log.debug("[SCRAPE] event page %s -> %s, %d chars", r["url"], state, len(summary))
            yield {"source": "calendar", "item_key": key, "date": r["date"], "time": r["time"],
                   "country": r["country"], "heading": r["heading"], "summary": summary,
                   "url": BASE + r["url"] if r["url"].startswith("/") else r["url"],
                   "event_dt": calendar_event_dt(r["date"], r["time"])}
            self._pause(DELAY_EVENT_S)

    # ---------------- news (JSON feed) ----------------
    def _warm_session(self):
        """Hit the public stream page once so ashx requests carry normal cookies/referer."""
        if getattr(self, "_warmed", False) or self._stopped():
            return
        page = self.ctx.new_page()
        try:
            page.goto(STREAM_VIEWS["all"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(random.randint(800, 2000))
            self._warmed = True
            log.debug("[SCRAPE] session warmed via /stream")
        except Exception as e:
            log.debug("[SCRAPE] session warm skipped: %s", e)
        finally:
            page.close()

    def _stream_api_page(self, name, start):
        """Fetch one page from ws/stream.ashx. Returns list of dicts."""
        params = {"start": str(start), "size": str(STREAM_PAGE_SIZE)}
        params.update(STREAM_API_EXTRA.get(name) or {})
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{STREAM_API[name]}?{qs}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": STREAM_VIEWS.get(name, STREAM_VIEWS["all"]),
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = self.ctx.request.get(url, headers=headers, timeout=NAV_TIMEOUT_MS)
        if resp.status >= 400:
            raise RuntimeError(f"stream.ashx [{name}] HTTP {resp.status}")
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"stream.ashx [{name}] bad JSON: {e}") from e
        if not isinstance(data, list):
            raise RuntimeError(f"stream.ashx [{name}] expected list, got {type(data).__name__}")
        return data

    def _normalize_api_item(self, item):
        """Map one stream.ashx object to the internal news raw shape."""
        news_id = str(item.get("ID") or item.get("id") or "")
        href = item.get("url") or ""
        title = (item.get("title") or "").strip()
        published_dt = parse_api_dt(item.get("date"))
        country = (item.get("country") or "").strip()
        category = (item.get("category") or "").strip()
        desc = item.get("description") or ""
        importance = item.get("importance")
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            importance = 0
        key = news_key(news_id, href, title)
        return {
            "id": news_id,
            "key": key,
            "headline": title,
            "title": title,
            "url": href,
            "country": country,
            "topic": category,
            "category": category,
            "text": desc,
            "description": desc,
            "importance": importance,
            "published_dt": published_dt,
            "age": None,
        }

    def _collect_stream_api(self, name, store):
        """Page through stream.ashx until past the 24h window or already-stored items."""
        self._warm_session()
        seen, out = {}, []
        now = utcnow()
        last_err = None
        for page_i in range(MAX_STREAM_PAGES):
            if self._stopped():
                break
            start = page_i * STREAM_PAGE_SIZE
            batch = None
            for attempt in range(1, RETRIES + 1):
                if self._stopped():
                    break
                try:
                    batch = self._stream_api_page(name, start)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    log.warning("[SCRAPE] stream.ashx [%s] start=%d attempt %d failed: %s",
                                name, start, attempt, e)
                    self._pause(DELAY_RETRY_S, scale=attempt)
            if batch is None:
                if page_i == 0:
                    raise RuntimeError(f"stream.ashx [{name}] failed: {last_err}")
                break
            if not batch:
                log.debug("[SCRAPE] stream.ashx [%s] start=%d empty page — done", name, start)
                break

            stop_paging = False
            oldest_age = None
            for item in batch:
                raw = self._normalize_api_item(item)
                if raw["key"] in seen:
                    continue
                seen[raw["key"]] = True
                pub = raw["published_dt"]
                if pub is None:
                    continue
                age = now - pub
                oldest_age = age if oldest_age is None else max(oldest_age, age)
                if age > NEWS_WINDOW:
                    stop_paging = True
                    continue
                out.append(raw)

            log.debug("[SCRAPE] stream.ashx [%s] start=%d: +%d items (total %d)%s",
                      name, start, len(batch), len(out),
                      f", oldest~{oldest_age}" if oldest_age is not None else "")

            # Stop early once we hit items already stored and older than a few minutes.
            last = batch[-1]
            last_key = news_key(str(last.get("ID") or ""), last.get("url") or "",
                                (last.get("title") or "").strip())
            last_pub = parse_api_dt(last.get("date"))
            if last_pub is not None and (now - last_pub) > NEWS_WINDOW:
                stop_paging = True
            if store.exists(last_key) and last_pub is not None and (now - last_pub) > timedelta(minutes=5):
                log.debug("[SCRAPE] stream.ashx [%s] reached already-stored items; stop paging", name)
                stop_paging = True
            if stop_paging:
                break
            self._pause(DELAY_EVENT_S)
        return out

    def news_items(self, store):
        collected = {}
        for i, name in enumerate(STREAM_API):
            if self._stopped():
                return
            if i:
                self._pause(DELAY_STREAM_S)
            collected[name] = self._collect_stream_api(name, store)
            log.debug("[SCRAPE] stream [%s]: %d items collected", name, len(collected[name]))
        rows = build_news_items(collected)
        new = [r for r in rows if store.find_existing(r) is None]
        log.debug("[SCRAPE] news: %d items in last 24h, %d new (duplicates skipped)", len(rows), len(new))
        yield from new


# =====================================================================
#  MAIN LOOP
# =====================================================================
def _is_shutdown_scrape_error(exc: BaseException) -> bool:
    """Ctrl-C / process stop often kills Chromium mid wait_for_timeout."""
    msg = str(exc).lower()
    needles = (
        "has been closed",
        "target closed",
        "browser has been closed",
        "context or browser has been closed",
        "connection closed",
        "torn down",
    )
    return any(n in msg for n in needles)


def run_cycle(cycle, scraper, store, wake, state):
    t0 = time.time()
    today = str(utcnow().date())
    # first cycle / new UTC day: calendar 'recent' + 'yesterday' views; otherwise just the default view
    codes = ["0", "-1"] if (cycle == 1 or state.get("day") != today) else ["0"]
    n_cal = n_news = 0
    log.info("--- cycle %d: polling calendar (ranges %s) + news streams ---", cycle, codes)

    try:
        for item in scraper.calendar_items(store, codes):
            iid = store.insert_item(item)
            if iid:
                n_cal += 1
                wake.set()
                log.info("[NEW] calendar #%d %s %s | %s | %s", iid, item["date"], item["time"], item["country"],
                         item["heading"][:70])
        state["day"] = today
        state["cal_fail"] = 0
    except Exception as e:
        if scraper.stop.is_set():
            log.debug("[SCRAPE] calendar poll interrupted on shutdown: %s", e)
        elif _is_shutdown_scrape_error(e):
            # Ctrl-C often kills Chromium before Python marks stop — not a real outage.
            log.warning("[SCRAPE] calendar browser closed (likely shutdown): %s", e)
        else:
            state["cal_fail"] = state.get("cal_fail", 0) + 1
            log.error("[SCRAPE] calendar poll failed (%d in a row): %s", state["cal_fail"], e)

    if not scraper.stop.is_set():
        scraper._pause(DELAY_STREAM_S)

    try:
        for item in scraper.news_items(store):
            iid = store.insert_item(item)
            if iid:
                n_news += 1
                wake.set()
                log.info("[NEW] news #%d %s %s | %s | %s", iid, item["date"], item["time"], item["country"],
                         item["heading"][:70])
        state["news_fail"] = 0
    except Exception as e:
        if scraper.stop.is_set():
            log.debug("[SCRAPE] news poll interrupted on shutdown: %s", e)
        elif _is_shutdown_scrape_error(e):
            log.warning("[SCRAPE] news browser closed (likely shutdown): %s", e)
        else:
            state["news_fail"] = state.get("news_fail", 0) + 1
            log.error("[SCRAPE] news poll failed (%d in a row): %s", state["news_fail"], e)

    if state.get("cal_fail", 0) >= 3 or state.get("news_fail", 0) >= 3:
        scraper.restart()
        state["cal_fail"] = state["news_fail"] = 0

    log.info("--- cycle %d done in %.1fs: new calendar=%d, new news=%d | db: %s ---",
             cycle, time.time() - t0, n_cal, n_news, store.counts())


def run_check(env, args):
    """Test API key, each model, and Telegram without scraping."""
    key = env.get("NVIDIA_API_KEY") or os.environ.get("NVIDIA_API_KEY")
    log.info("NVIDIA_API_KEY: %s", mask(key))
    if not key:
        log.error("NVIDIA_API_KEY missing in %s", args.env)
        return 1
    router = LLMRouter(key, MODELS, threading.Event())
    sample = {"date": fmt_dt(utcnow())[:10], "time": fmt_dt(utcnow())[11:19], "country": "United States",
              "heading": "Fed minutes point to another rate hike", "summary":
              "Minutes from the September meeting showed most policymakers favour one more hike this year; "
              "the dollar and Treasury yields rose while gold slipped toward a two-month low.",
              "url": "https://tradingeconomics.com/commodity/gold"}
    bad = 0
    for m in MODELS:
        t0 = time.time()
        try:
            raw = router.call_model(m, build_prompt(sample))
            res = parse_llm_json(raw)
            log.info("[CHECK] %-40s OK  %.1fs -> %s", m["name"], time.time() - t0, json.dumps(res, ensure_ascii=False))
        except Exception as e:
            bad += 1
            log.error("[CHECK] %-40s FAIL %.1fs (%s): %s", m["name"], time.time() - t0, classify_error(e)[0], str(e)[:300])
    token, chat = env.get("TELEGRAM_STOCK_BOT_TOKEN"), env.get("TELEGRAM_STOCK_CHAT_ID")
    log.info("TELEGRAM_STOCK_BOT_TOKEN: %s | TELEGRAM_STOCK_CHAT_ID: %s", mask(token), chat or "<missing>")
    if token and chat:
        ok, info = Telegram(token, chat).get_me()
        if ok:
            log.info("[CHECK] Telegram bot OK: @%s", info["result"].get("username"))
        else:
            bad += 1
            log.error("[CHECK] Telegram FAIL: %s", info)
    else:
        bad += 1
        log.error("[CHECK] Telegram variables missing")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="Gold signal monitor (Trading Economics -> NVIDIA LLM -> Telegram)")
    ap.add_argument("--env", default=ENV_PATH, help="path to env.txt (default: repo-root env.txt)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL_S,
                    help="nominal seconds between polls (actual wait is randomized)")
    ap.add_argument("--once", action="store_true", help="one poll cycle, analyze the queue, then exit")
    ap.add_argument("--check", action="store_true", help="test NVIDIA models + Telegram and exit")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--alert-all", action="store_true", help="alert on every analysis, not only buy/sell signals")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--log-level", default="DEBUG", choices=["DEBUG", "INFO", "WARNING"])
    args = ap.parse_args()

    threading.current_thread().name = "main"
    setup_logging(args.log_level)
    log.info("Gold signal monitor starting | db=%s env=%s interval~%ss (randomized) | telegram times=IST",
             args.db, args.env, args.interval)

    try:
        env = load_env(args.env)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 1
    log.debug("env keys found: %s", sorted(env))
    if args.check:
        return run_check(env, args)

    api_key = env.get("NVIDIA_API_KEY") or os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        log.error("NVIDIA_API_KEY not found in %s", args.env)
        return 1
    log.debug("NVIDIA_API_KEY loaded: %s", mask(api_key))

    telegram = None
    if not args.no_telegram:
        token, chat = env.get("TELEGRAM_STOCK_BOT_TOKEN"), env.get("TELEGRAM_STOCK_CHAT_ID")
        if token and chat:
            telegram = Telegram(token, chat)
            log.debug("Telegram enabled: token %s, chat %s", mask(token), chat)
        else:
            log.warning("Telegram variables missing in %s: alerts disabled", args.env)
    else:
        log.info("Telegram alerts disabled by --no-telegram")

    stop, wake = threading.Event(), threading.Event()
    shutting_down = {"done": False}

    def _request_shutdown(signum, _frame):
        if shutting_down["done"]:
            return
        shutting_down["done"] = True
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        log.info("%s received: finishing current work, closing DB, then exiting...", name)
        stop.set()
        wake.set()

    signal.signal(signal.SIGINT, _request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_shutdown)

    store = None
    scraper = None
    worker = None
    try:
        store = Store(args.db)
        log.info("DB ready: %s | status counts: %s", Path(args.db).resolve(), store.counts())
        router = LLMRouter(api_key, MODELS, stop)
        worker = Worker(store, router, telegram, stop, wake, alert_all=args.alert_all)
        worker.start()
        wake.set()                      # pick up anything left pending from a previous run

        scraper = Scraper(headed=args.headed, stop=stop)
        state, cycle = {}, 0
        while not stop.is_set():
            cycle += 1
            t0 = time.time()
            try:
                run_cycle(cycle, scraper, store, wake, state)
            except Exception:
                log.exception("cycle %d crashed", cycle)
            if args.once or stop.is_set():
                break
            # Randomize inter-poll wait so traffic does not look perfectly periodic.
            target = args.interval * random.uniform(*POLL_JITTER_S)
            wait = max(5.0, target - (time.time() - t0))
            log.debug("sleeping %.0fs until next poll (queue: %d open)", wait, store.open_work())
            stop.wait(wait)
        if args.once and not stop.is_set():
            log.info("--once: waiting for the AI queue to drain (%d open)...", store.open_work())
            deadline = time.time() + 20 * 60
            while not stop.is_set() and store.open_work() and time.time() < deadline:
                stop.wait(2)
    finally:
        stop.set()
        wake.set()
        if worker is not None:
            worker.join(timeout=REQUEST_TIMEOUT_S + 10)
            if worker.is_alive():
                log.warning("AI worker still running after join timeout; continuing shutdown")
        if scraper is not None:
            try:
                scraper.close()
            except Exception:
                log.exception("error while closing browser")
        if store is not None:
            try:
                counts = store.counts()
            except Exception:
                counts = "?"
            try:
                store.close()
            except Exception:
                log.exception("error while closing DB")
            log.info("Stopped cleanly. db status counts: %s", counts)
        else:
            log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
