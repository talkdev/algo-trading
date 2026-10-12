"""
Compact console dashboard for the live paper/trading loop.

Replaces the noisy per-step audit dump with one readable board per cycle.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

UTC = timezone.utc

# ANSI (safe no-op when not a TTY)
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if _USE_COLOR and os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:  # noqa: BLE001
        _USE_COLOR = False


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(t: str) -> str:
    return _c("32", t)


def _red(t: str) -> str:
    return _c("31", t)


def _yellow(t: str) -> str:
    return _c("33", t)


def _cyan(t: str) -> str:
    return _c("36", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _dim(t: str) -> str:
    return _c("2", t)


def _mark(ok: bool) -> str:
    return _green("OK ") if ok else _red("NO ")


def _fmt_px(v: Any, digits: int = 2) -> str:
    try:
        if v is None:
            return "—"
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


@dataclass
class BoardView:
    """Snapshot of one cycle for rendering."""
    cycle_no: int = 0
    mode: str = "paper"
    now_utc: Optional[datetime] = None
    symbol: str = "GOLDM"
    instrument_key: str = ""

    # Quote / gold
    bid: Optional[float] = None
    ask: Optional[float] = None
    ltp: Optional[float] = None
    spread: Optional[float] = None
    bar_o: Optional[float] = None
    bar_h: Optional[float] = None
    bar_l: Optional[float] = None
    bar_c: Optional[float] = None
    bar_age_m: Optional[float] = None
    candles_5m: int = 0
    candles_15m: int = 0

    # Signal
    trend: str = "unknown"
    breakout: str = "none"
    direction: str = "neutral"
    regime: str = "unknown"
    atr: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None

    # News
    news_bias: str = "unknown"
    news_score: float = 0.0
    news_fresh: bool = False
    news_items: int = 0
    news_detail: str = ""

    # Feeds
    quote_ok: bool = False
    candles_ok: bool = False
    history_ok: bool = False
    monitor_ok: bool = False
    news_db_ok: bool = False
    calendar_ok: bool = False
    macros_ok: bool = False
    dxy: Optional[float] = None
    us10y: Optional[float] = None

    # Gates / decision
    gate_rows: list[tuple[str, bool, str]] = field(default_factory=list)
    decision: str = "WAIT"
    decision_reason: str = ""
    system_state: str = "WAIT"

    # Position / plan
    has_position: bool = False
    position_side: str = ""
    position_entry: Optional[float] = None
    position_stop: Optional[float] = None
    position_target: Optional[float] = None
    plan_side: str = ""
    plan_entry: Optional[float] = None
    plan_stop: Optional[float] = None
    plan_target: Optional[float] = None
    plan_rejected: str = ""

    trades_today: int = 0
    max_trades: int = 0


def _ist_stamp(now_utc: Optional[datetime]) -> str:
    now = now_utc or datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    ist = now.astimezone(ZoneInfo("Asia/Kolkata"))
    return ist.strftime("%Y-%m-%d %H:%M:%S IST")


def _pad_row(text: str, width: int = 78) -> str:
    visible = text
    for seq in (
        "\033[0m", "\033[1m", "\033[2m", "\033[31m", "\033[32m",
        "\033[33m", "\033[36m", "\033[35m", "\033[34m",
    ):
        visible = visible.replace(seq, "")
    inner = width - 4  # "│ " + content + " │"
    if len(visible) > inner:
        # Truncate raw text carefully
        text = text[: inner + (len(text) - len(visible))]
        visible = visible[:inner]
    pad = inner - len(visible)
    return "│ " + text + (" " * max(0, pad)) + " │"


def _section(title: str, width: int = 78) -> str:
    return _pad_row(_bold(_cyan(title)), width)


def render_board(v: BoardView, *, width: int = 78) -> str:
    w = width
    top = "┌" + "─" * (w - 2) + "┐"
    mid = "├" + "─" * (w - 2) + "┤"
    bot = "└" + "─" * (w - 2) + "┘"

    mode = v.mode.upper()
    header = (
        f"{_bold(v.symbol)}  {_yellow(mode) if mode == 'PAPER' else _red(mode)}  "
        f"cycle #{v.cycle_no}  {_dim(_ist_stamp(v.now_utc))}"
    )

    lines = [top, _pad_row(header, w), mid]

    # --- GOLD ---
    lines.append(_section("GOLD (MCX)", w))
    mid_px = v.ltp if v.ltp is not None else (
        (v.bid + v.ask) / 2 if v.bid is not None and v.ask is not None else v.bar_c
    )
    lines.append(_pad_row(
        f"LTP {_bold(_fmt_px(v.ltp))}   "
        f"Bid {_fmt_px(v.bid)}  Ask {_fmt_px(v.ask)}  "
        f"Spread {_fmt_px(v.spread)}",
        w,
    ))
    age_bit = f"  age {v.bar_age_m:.1f}m" if v.bar_age_m is not None else ""
    lines.append(_pad_row(
        f"5m OHLC {_fmt_px(v.bar_o,0)}/{_fmt_px(v.bar_h,0)}/"
        f"{_fmt_px(v.bar_l,0)}/{_bold(_fmt_px(v.bar_c,0))}{age_bit}",
        w,
    ))
    trend_txt = v.trend.upper()
    if v.trend == "bullish":
        trend_col = _green(trend_txt)
    elif v.trend == "bearish":
        trend_col = _red(trend_txt)
    else:
        trend_col = _yellow(trend_txt)
    brk = v.breakout.upper() if v.breakout else "NONE"
    lines.append(_pad_row(
        f"Trend15 {trend_col}  EMA {_fmt_px(v.ema_fast,0)}/{_fmt_px(v.ema_slow,0)}  "
        f"Brk5 {_bold(brk)}  ATR {_fmt_px(v.atr,1)}  {v.regime}",
        w,
    ))
    lines.append(_pad_row(
        f"Signal {v.direction}   completed bars 5m={v.candles_5m} 15m={v.candles_15m}",
        w,
    ))
    if v.dxy is not None or v.us10y is not None:
        lines.append(_pad_row(
            f"Context  DXY {_fmt_px(v.dxy, 3)}   US10Y {_fmt_px(v.us10y, 3)}  "
            f"{_dim('(support only — not trade price)')}",
            w,
        ))

    # --- DATA ---
    lines.append(mid)
    lines.append(_section("DATA FETCH", w))
    feeds = [
        ("Quote book", v.quote_ok),
        ("Candles 5m/15m", v.candles_ok),
        ("History stitch", v.history_ok),
        ("Monitor dash", v.monitor_ok),
        ("News DB", v.news_db_ok),
        ("Calendar", v.calendar_ok),
        ("Macros", v.macros_ok),
    ]
    chunk: list[str] = []
    for name, ok in feeds:
        chunk.append(f"{_mark(ok)}{name}")
    # two rows of feeds
    half = (len(chunk) + 1) // 2
    lines.append(_pad_row("   ".join(chunk[:half]), w))
    if chunk[half:]:
        lines.append(_pad_row("   ".join(chunk[half:]), w))

    # --- NEWS ---
    lines.append(mid)
    lines.append(_section("NEWS (AI)", w))
    bias = v.news_bias.upper()
    if bias in ("BULLISH",):
        bias_s = _green(bias)
    elif bias in ("BEARISH",):
        bias_s = _red(bias)
    else:
        bias_s = _yellow(bias)
    lines.append(_pad_row(
        f"Bias {bias_s}   score {v.news_score:+.2f}   "
        f"fresh={'yes' if v.news_fresh else 'no'}   items={v.news_items}",
        w,
    ))
    if v.news_detail:
        lines.append(_pad_row(_dim(v.news_detail[:70]), w))

    # --- CHECKLIST (human-readable, one condition per line) ---
    lines.append(mid)
    lines.append(_section("ENTRY CHECKLIST", w))

    gate_map = {str(n).upper(): (bool(ok), str(d)) for n, ok, d in (v.gate_rows or [])}

    def _g(name: str) -> tuple[bool, str]:
        return gate_map.get(name.upper(), (False, "n/a"))

    sess_ok, _ = _g("SESSION")
    data_ok, _ = _g("DATA")
    # DATA gate may be absent on HOLD cycles — fall back to feed flags
    if "DATA" not in gate_map:
        data_ok = bool(v.quote_ok and v.candles_ok)
    event_ok, event_d = _g("EVENT")
    if "EVENT" not in gate_map:
        event_ok, event_d = True, "clear"
    risk_ok, risk_d = _g("RISK")
    if "RISK" not in gate_map:
        risk_ok, risk_d = (not v.has_position), ("has position" if v.has_position else "ok")

    trend_up = v.trend == "bullish"
    trend_dn = v.trend == "bearish"
    brk_up = v.breakout == "up"
    brk_dn = v.breakout == "down"
    news_long = bool(v.news_fresh) and v.news_score >= 1.0 and v.news_bias in ("bullish",)
    news_short = bool(v.news_fresh) and v.news_score <= -1.0 and v.news_bias in ("bearish",)
    # Conflicting / unknown news never confirms
    if v.news_bias in ("conflicting", "unknown", "neutral") or not v.news_fresh:
        news_long = False
        news_short = False

    def _chk(ok: bool, label: str, note: str = "") -> str:
        note_s = f"  {_dim(note)}" if note else ""
        return f"{_mark(ok)}{label}{note_s}"

    lines.append(_pad_row(_bold("Shared (both sides)"), w))
    lines.append(_pad_row(_chk(sess_ok, "In session entry window"), w))
    lines.append(_pad_row(_chk(data_ok, "MCX quote + candles valid"), w))
    lines.append(_pad_row(_chk(event_ok, "No event blackout", event_d if not event_ok else ""), w))
    lines.append(_pad_row(_chk(risk_ok, "Risk allows new trade", risk_d if not risk_ok else ""), w))

    lines.append(_pad_row(_bold("BUY (long) — all must be OK"), w))
    lines.append(_pad_row(_chk(trend_up, "15m trend bullish (EMA20 > EMA50)",
                               f"now={v.trend}"), w))
    lines.append(_pad_row(_chk(brk_up, "5m close above prior 20-bar high",
                               f"breakout={v.breakout}"), w))
    lines.append(_pad_row(_chk(news_long, "News confirms long (score ≥ +1, fresh)",
                               f"{v.news_bias} {v.news_score:+.1f}"), w))

    lines.append(_pad_row(_bold("SELL (short) — all must be OK"), w))
    lines.append(_pad_row(_chk(trend_dn, "15m trend bearish (EMA20 < EMA50)",
                               f"now={v.trend}"), w))
    lines.append(_pad_row(_chk(brk_dn, "5m close below prior 20-bar low",
                               f"breakout={v.breakout}"), w))
    lines.append(_pad_row(_chk(news_short, "News confirms short (score ≤ −1, fresh)",
                               f"{v.news_bias} {v.news_score:+.1f}"), w))

    long_ready = sess_ok and data_ok and event_ok and risk_ok and trend_up and brk_up and news_long
    short_ready = sess_ok and data_ok and event_ok and risk_ok and trend_dn and brk_dn and news_short
    lines.append(_pad_row(
        f"Setup  BUY [{'READY' if long_ready else 'waiting'}]   "
        f"SELL [{'READY' if short_ready else 'waiting'}]",
        w,
    ))

    # --- SL / TP preview ---
    lines.append(mid)
    lines.append(_section("STOP / TARGET (from ATR × 1.5 , target 2R)", w))
    atr = float(v.atr) if v.atr else None
    entry = float(mid_px) if mid_px is not None else None
    if v.has_position:
        lines.append(_pad_row(
            f"OPEN {_bold(v.position_side)}  entry {_fmt_px(v.position_entry)}  "
            f"SL {_red(_fmt_px(v.position_stop))}  TP {_green(_fmt_px(v.position_target))}",
            w,
        ))
    elif v.plan_entry is not None:
        rej = f"  {_red(v.plan_rejected)}" if v.plan_rejected else ""
        lines.append(_pad_row(
            f"PLAN {v.plan_side}  entry {_fmt_px(v.plan_entry)}  "
            f"SL {_red(_fmt_px(v.plan_stop))}  TP {_green(_fmt_px(v.plan_target))}{rej}",
            w,
        ))
    elif atr and entry and atr > 0:
        d = 1.5 * atr
        lines.append(_pad_row(
            f"If BUY  @ {_fmt_px(entry)} → SL {_fmt_px(entry - d)}  TP {_fmt_px(entry + 2 * d)}",
            w,
        ))
        lines.append(_pad_row(
            f"If SELL @ {_fmt_px(entry)} → SL {_fmt_px(entry + d)}  TP {_fmt_px(entry - 2 * d)}",
            w,
        ))
        lines.append(_pad_row(
            _dim(f"D={_fmt_px(d, 1)} pts  (illustrative until a setup is approved)"),
            w,
        ))
    else:
        lines.append(_pad_row(_dim("ATR unavailable — no stop/target preview"), w))

    # --- DECISION ---
    lines.append(mid)
    lines.append(_section("DECISION", w))
    act = v.decision.upper()
    if act in ("BUY", "SELL"):
        act_s = _green(_bold(act))
    elif act.startswith("HALT"):
        act_s = _red(_bold(act))
    elif act == "HOLD":
        act_s = _yellow(_bold(act))
    else:
        act_s = _yellow(_bold(act))
    lines.append(_pad_row(f"{act_s}   state={v.system_state}", w))
    if v.decision_reason:
        lines.append(_pad_row(_dim(v.decision_reason[:72]), w))
    lines.append(_pad_row(
        f"Trades today {v.trades_today}/{v.max_trades or '∞'}   "
        f"key {_dim(v.instrument_key or '—')}",
        w,
    ))

    lines.append(bot)
    return "\n".join(lines)


def print_board(v: BoardView, *, clear: bool = True) -> None:
    text = render_board(v)
    if clear and sys.stdout.isatty():
        if os.name == "nt":
            os.system("cls")
        else:
            sys.stdout.write("\033[H\033[J")
    # Prefer print so board is not prefixed by logging timestamps
    print(text, flush=True)


def gate_rows_from_decision(gate: Any) -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    for g in getattr(gate, "gates", None) or []:
        rows.append((
            str(getattr(g, "name", "?")),
            bool(getattr(g, "passed", False)),
            str(getattr(g, "detail", "") or ""),
        ))
    return rows
