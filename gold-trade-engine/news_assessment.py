"""
Normalized news assessment from commodity-events SQLite.

AI recommendations never place orders — they only produce a directional
assessment for the decision engine's news gate.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from config import ENGINE_DIR

UTC = timezone.utc
log = logging.getLogger("gold_news.news_assessment")

DEFAULT_DB = ENGINE_DIR / "gold_signals.db"

RECO_SCORE = {
    "strong buy": 2,
    "buy": 1,
    "neutral": 0,
    "no impact": 0,
    "sell": -1,
    "strong sell": -2,
}


@dataclass
class NewsAssessment:
    bias: str = "unknown"          # bullish | bearish | neutral | conflicting | unknown
    score: float = 0.0
    confidence: float = 0.0
    fresh: bool = False
    conflicting: bool = False
    item_count: int = 0
    as_of_utc: str = ""
    freshness_limit_minutes: float = 45.0
    reasons: list[str] = field(default_factory=list)
    item_ids: list[int] = field(default_factory=list)
    source_db: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def reco_to_score(reco: str) -> Optional[int]:
    if not reco:
        return None
    return RECO_SCORE.get(str(reco).strip().lower())


def _normalize_heading(heading: str) -> str:
    h = re.sub(r"\s+", " ", (heading or "").strip().lower())
    return h[:160]


def load_analyzed_items(
    db_path: str | Path,
    *,
    now_utc: Optional[datetime] = None,
    freshness_minutes: float = 45.0,
    lookback_hours: float = 24.0,
) -> list[dict[str, Any]]:
    """Load AI-done items within lookback; annotate age vs freshness window."""
    path = Path(db_path)
    now_utc = now_utc or datetime.now(tz=UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    if not path.exists():
        return []

    cutoff = now_utc - timedelta(hours=float(lookback_hours))
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(str(path), timeout=10)
        con.row_factory = sqlite3.Row
        cur = con.execute(
            """
            SELECT id, source, item_key, heading, summary, event_dt, published_at,
                   scraped_at, analyzed_at, ai_status, ai_impact, ai_recommendation,
                   ai_summary, importance
            FROM items
            WHERE ai_status = 'done'
              AND ai_recommendation IS NOT NULL
              AND ai_recommendation != ''
            ORDER BY COALESCE(analyzed_at, scraped_at) DESC
            LIMIT 500
            """
        )
        for r in cur.fetchall():
            d = dict(r)
            # Prefer publication/event time; never invent from scrape alone for freshness
            t = (
                _parse_dt(d.get("published_at"))
                or _parse_dt(d.get("event_dt"))
                or _parse_dt(d.get("analyzed_at"))
            )
            if t is None:
                continue
            if t < cutoff:
                continue
            age_m = (now_utc - t).total_seconds() / 60.0
            if age_m < -5:
                continue  # future-dated
            score = reco_to_score(d.get("ai_recommendation") or "")
            if score is None:
                continue
            d["_ts"] = t
            d["_age_minutes"] = age_m
            d["_score"] = score
            d["_fresh"] = age_m <= float(freshness_minutes)
            d["_norm_heading"] = _normalize_heading(d.get("heading") or "")
            rows.append(d)
        con.close()
    except sqlite3.Error as exc:
        log.warning("news db read failed: %s", exc)
        return []
    return rows


def assess_news(
    db_path: str | Path = "",
    *,
    now_utc: Optional[datetime] = None,
    freshness_minutes: float = 45.0,
    lookback_hours: float = 24.0,
    min_items: int = 1,
) -> NewsAssessment:
    """
    Aggregate fresh analyzed items into a single directional assessment.

    Dedupes by normalized heading (repeated copies add no weight).
    Conflicting bullish+bearish scores → conflicting (not confirmatory).
    """
    now_utc = now_utc or datetime.now(tz=UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    path = Path(db_path) if db_path else DEFAULT_DB
    out = NewsAssessment(
        as_of_utc=now_utc.isoformat(),
        freshness_limit_minutes=float(freshness_minutes),
        source_db=str(path),
    )
    if not path.exists():
        out.bias = "unknown"
        out.reasons.append("db_missing")
        return out

    items = load_analyzed_items(
        path, now_utc=now_utc,
        freshness_minutes=freshness_minutes,
        lookback_hours=lookback_hours,
    )
    fresh_items = [i for i in items if i.get("_fresh")]
    if not fresh_items:
        out.bias = "unknown"
        out.fresh = False
        out.reasons.append("no_fresh_analyzed_items")
        return out

    # Dedupe: keep newest per heading
    seen: dict[str, dict] = {}
    for it in sorted(fresh_items, key=lambda x: x["_ts"], reverse=True):
        key = it.get("_norm_heading") or str(it.get("id"))
        if key in seen:
            continue
        seen[key] = it

    uniq = list(seen.values())
    out.item_count = len(uniq)
    out.item_ids = [int(i["id"]) for i in uniq]
    scores = [int(i["_score"]) for i in uniq]
    # Ignore pure zeros for direction; keep for confidence dilution
    directional = [s for s in scores if s != 0]
    if len(uniq) < min_items:
        out.bias = "unknown"
        out.reasons.append("insufficient_items")
        return out

    if not directional:
        out.bias = "neutral"
        out.score = 0.0
        out.fresh = True
        out.confidence = 0.2
        out.reasons.append("all_neutral")
        return out

    bull = sum(1 for s in directional if s > 0)
    bear = sum(1 for s in directional if s < 0)
    avg = sum(directional) / len(directional)
    out.score = float(avg)
    out.fresh = True
    out.confidence = min(1.0, abs(avg) / 2.0 * (len(directional) / max(len(uniq), 1)))

    if bull > 0 and bear > 0:
        out.conflicting = True
        out.bias = "conflicting"
        out.reasons.append("bullish_and_bearish_present")
        return out

    if avg >= 1.0:
        out.bias = "bullish"
        out.reasons.append("score_bullish")
    elif avg <= -1.0:
        out.bias = "bearish"
        out.reasons.append("score_bearish")
    else:
        out.bias = "neutral"
        out.reasons.append("score_weak")
    return out


def news_supports_side(assessment: NewsAssessment, side: str) -> bool:
    """True if assessment confirms BUY/SELL direction (not conflicting/unknown)."""
    if not assessment.fresh or assessment.conflicting:
        return False
    side = (side or "").upper()
    if side == "BUY":
        return assessment.bias == "bullish" and assessment.score >= 1.0
    if side == "SELL":
        return assessment.bias == "bearish" and assessment.score <= -1.0
    return False
