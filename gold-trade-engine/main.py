#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gold News Trading Engine — Upstox / MCX
=======================================

All engine files and generated outputs live under ``gold-trade-engine/``.
Credentials are read only from the repo-root ``env.txt``.

Strategy inputs: US economic calendar, DXY, US10Y yields, gold (MCX) price action.
Decisions: BUY | SELL | NO_TRADE

Usage (from repo root)
----------------------
    python gold-trade-engine/main.py              # paper mode, 1 lot (config.json)
    python gold-trade-engine/main.py --selftest
    python gold-trade-engine/main.py --audit          # component validation (weekend-safe)
    python gold-trade-engine/main.py --backtest-synthetic
    python gold-trade-engine/main.py --mode live   # override to live
    python gold-trade-engine/main.py --lots 2
    python gold-trade-engine/main.py --backtest 2026-01-01 2026-03-01 --yahoo-macros
    python gold-trade-engine/main.py --login
    python gold-trade-engine/main.py --exchange-code CODE

Credentials: repo-root env.txt only (UPSTOX_ACCESS_TOKEN, UPSTOX_API_TOKEN,
UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI).

DXY / Treasury yields are NOT on Upstox. Supply CSVs via --dxy-csv / --yield-csv
or pass --yahoo-macros for a research-only Yahoo pull. Economic calendar: CSV/JSON
from a provider that permits redistribution (sample file ships for wiring tests).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Bootstrapping: allow `python gold-trade-engine/main.py` and hyphenated -m entry.
_ENGINE_DIR = Path(__file__).resolve().parent
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

import pandas as pd

from backtest import generate_synthetic_dataset, run_backtest
from config import (
    DEFAULT_CALENDAR,
    DEFAULT_CONFIG_FILE,
    ENV_FILE,
    OUTPUT_DIR,
    EngineConfig,
    ensure_engine_dirs,
    load_config_file,
)
from feeds import (
    CalendarFeedManager,
    EconomicEvent,
    candles_completed_as_of,
    load_calendar,
    load_macros,
    load_ohlcv_csv,
    maybe_fetch_macros,
    mcx_context_from_instrument,
    official_links_markdown,
    refresh_macros,  # noqa: F401
    resample_ohlcv,
    validate_candle_frame,
)
from strategy import NewsDecisionEngine, build_signal_from_market
from trading import ExecutionEngine, RiskManager, write_rejects_csv, write_trades_csv
from upstox_client import (
    UpstoxClient,
    load_instrument_master,
    resolve_credentials,
    resolve_mcx_gold_future,
    token_expiry,
    update_env_file,
)
from market_snapshot import (
    build_engine_snapshot,
    contract_from_mcx_context,
    persist_snapshot,
)
from market_signals import evaluate_market_signal
from news_assessment import assess_news
from decision_gates import evaluate_entry_gates
from trade_journal import TradeJournal
from config import KILL_SWITCH_FILE, ENGINE_DIR
from producers import HUB as PRODUCER_HUB
from eod_report import write_eod_report

UTC = timezone.utc
log = logging.getLogger("gold_news")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    if root.handlers:
        for h in root.handlers:
            h.setLevel(level)
        root.setLevel(level)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            force=True,
        )
    # Keep noisy HTTP libs / embedded producers quieter unless --verbose
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)
        logging.getLogger("gold").setLevel(logging.WARNING)
        logging.getLogger("ai-worker").setLevel(logging.WARNING)
        logging.getLogger("gold_news.calendar.manager").setLevel(logging.WARNING)
        logging.getLogger("gold_news.producers").setLevel(logging.WARNING)
        logging.getLogger("gold_news.trading").setLevel(logging.WARNING)
        logging.getLogger("gold_news.diag").setLevel(logging.WARNING)


# -------------------------------------------------------------------------------- session / ops


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = str(value).split(":")
    return int(h), int(m)


def _local_session_minutes(now_utc: datetime, cfg: EngineConfig) -> tuple[int, int, int, int]:
    """Return (now_mins, open_mins, cutoff_mins, close_mins) in trading_day_tz."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.trading_day_tz or "Asia/Kolkata")
    local = now_utc.astimezone(tz) if now_utc.tzinfo else now_utc.replace(tzinfo=UTC).astimezone(tz)
    oh, om = _parse_hhmm(cfg.market_open)
    cut_h, cut_m = _parse_hhmm(cfg.entry_cutoff)
    close_h, close_m = _parse_hhmm(cfg.market_close)
    now_m = local.hour * 60 + local.minute
    return now_m, oh * 60 + om, cut_h * 60 + cut_m, close_h * 60 + close_m


def _in_entry_window(now_utc: datetime, cfg: EngineConfig) -> bool:
    """
    True if now is within [market_open, entry_cutoff] AND before market_close
    in trading_day_tz (same-day MCX session; overnight sessions rejected by validate).
    """
    mins, open_m, cutoff_m, close_m_total = _local_session_minutes(now_utc, cfg)
    return open_m <= mins <= cutoff_m and mins < close_m_total


def _past_flatten_cutoff(now_utc: datetime, cfg: EngineConfig) -> bool:
    """True when within flatten_before_close_minutes of market_close (or after)."""
    mins, _o, _cut, close_m = _local_session_minutes(now_utc, cfg)
    lead = int(getattr(cfg, "flatten_before_close_minutes", 5) or 0)
    return mins >= (close_m - lead)


def _acquire_process_lock(lock_path: Path, *, instrument_key: str = ""):
    """Exclusive file lock so two instances cannot trade the same account/instrument."""
    import os
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+", encoding="utf-8")
        if os.name == "nt":
            import msvcrt
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return None
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} instrument={instrument_key}\n")
        fh.flush()
        return fh
    except OSError as exc:
        log.error("process lock failed: %s", exc)
        return None


def _release_process_lock(fh, lock_path: Path) -> None:
    import os
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    except OSError:
        pass


def _check_min_order_affordable(cfg: EngineConfig, risk: RiskManager,
                                tick_size: float = 1.0) -> dict:
    """Fail closed when even one fixed lot exceeds margin / risk budget at a ref price."""
    # Use a conservative high reference for GOLD-scale quotes.
    # GOLDM trades ~1e5; keep affordability on a realistic ATR band, not tick×N
    # (MCX tick can be 1 or 100 — tick×5 would invent a 500–1500pt stop and false-fail).
    ref = 150_000.0 if str(cfg.gold_symbol).upper().startswith("GOLDM") else 72_000.0
    import pandas as pd
    idx = pd.date_range("2026-01-05", periods=max(cfg.atr_period + 2, 8), freq="5min", tz="UTC")
    mid = ref
    # ~40pt half-range ≈ typical quiet 5m GOLDM ATR band for a smoke check
    half = max(float(tick_size) if tick_size and tick_size < 50 else 40.0, 20.0)
    candles = pd.DataFrame({
        "open": mid, "high": mid + half,
        "low": mid - half, "close": mid,
    }, index=idx)
    plan = risk.build_trade_plan("BUY", mid - 1, mid + 1, candles, spread_points=2.0)
    if plan.rejected:
        return {
            "ok": False,
            "reason": (
                f"min order not affordable under configured assumptions: "
                f"{plan.reject_reason} (symbol={cfg.gold_symbol} equity={risk.equity:,.0f} "
                f"vppu={cfg.value_per_price_unit} margin_pct={cfg.margin_pct_of_notional}). "
                f"Use GOLDM / raise equity / lower fixed_lots before live."
            ),
        }
    return {
        "ok": True,
        "reason": f"1-lot plan ok lots={plan.lots} qty={plan.quantity} risk≈{plan.risk_budget:,.0f}",
    }


# -------------------------------------------------------------------------------- selftest


def run_selftest() -> int:
    """Offline unit checks — no network, no Upstox token."""
    from datetime import timedelta

    from feeds import (
        EconomicEvent,
        atr,
        classify_direction,
        events_known_as_of,
        normalize_event,
        percent_change,
        yield_change_bps,
    )
    from strategy import (
        NewsDecisionEngine,
        find_pending_release_in_window,
        generate_signal,
        get_gold_bias,
        get_price_confirmation,
        is_inside_news_block,
        is_relevant_event,
    )
    from trading import RiskManager, calculate_position_size, stop_and_target

    errors: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  PASS  {name}")
        else:
            msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
            print(msg)
            errors.append(name)

    print("Gold News Engine selftest")
    print("=" * 40)

    # percent / yield maths
    check("percent_change 0.03%", abs(percent_change(100.03, 100.0) - 0.03) < 1e-9)
    check("yield_change 2 bps", abs(yield_change_bps(4.02, 4.00) - 2.0) < 1e-9)
    check("classify up", classify_direction(0.05, 0.03) == "up")
    check("classify flat", classify_direction(0.01, 0.03) == "flat")
    check("classify unknown", classify_direction(None, 0.03) == "unknown")

    # bias
    check("bullish bias", get_gold_bias("down", "down") == "bullish")
    check("bearish bias", get_gold_bias("up", "up") == "bearish")
    check("neutral bias", get_gold_bias("up", "down") == "neutral")
    check("mixed real yield", get_gold_bias("down", "down", "up", True) == "mixed")

    # news filter
    now = datetime(2026, 1, 14, 13, 30, tzinfo=UTC)
    ev = EconomicEvent(
        event_id="t1", event_name="US CPI", currency="USD", importance="high",
        scheduled_time_utc=now, status="scheduled",
    )
    check("relevant USD high", is_relevant_event(ev))
    check("inside block at release", is_inside_news_block(now, [ev]))
    check("outside block +1h", not is_inside_news_block(now + timedelta(hours=1), [ev]))
    check("unknown time blocks", is_inside_news_block(now, [
        EconomicEvent("x", "X", "USD", "high", None)
    ]))

    # never fill actual from forecast
    ne = normalize_event({"event_name": "CPI", "currency": "USD", "importance": "high",
                          "forecast": 0.3, "actual": ""})
    check("actual not filled from forecast", ne.actual is None)

    # point-in-time calendar
    released = EconomicEvent(
        event_id="cpi", event_name="CPI", currency="USD", importance="high",
        scheduled_time_utc=now, actual=0.4, status="released",
        retrieved_at_utc=now - timedelta(days=10),
        actual_release_time_utc=now,
    )
    before = events_known_as_of([released], now - timedelta(minutes=1))
    after = events_known_as_of([released], now + timedelta(minutes=1))
    check("no actual look-ahead", before[0].actual is None)
    check("actual after release", after[0].actual == 0.4)

    # breakout
    idx = pd.date_range("2026-01-05", periods=10, freq="5min", tz="UTC")
    base = [100, 101, 102, 101, 100, 99, 100, 101, 102, 110]
    candles = pd.DataFrame({
        "open": base, "high": [b + 1 for b in base], "low": [b - 1 for b in base],
        "close": base,
    }, index=idx)
    check("bullish breakout", get_price_confirmation(candles, "bullish", 6) == "buy")
    flat = candles.copy()
    flat.iloc[-1, flat.columns.get_loc("close")] = float(candles.iloc[-7:-1]["high"].max())
    check("exact high no trade", get_price_confirmation(flat, "bullish", 6) == "no_trade")

    # ATR / sizing / stops
    atr_v = atr(candles, 5)
    check("atr positive", atr_v is not None and atr_v > 0)
    units = calculate_position_size(100_000, 0.25, 6.0, 100.0, 5.0)
    check("position size floor", units == int(250 // (6 * 100 + 5)))
    check("never round up size", calculate_position_size(100, 0.25, 50, 100, 5) == 0)
    sp, tp, td = stop_and_target("BUY", 1000.0, 10.0, 2.0)
    check("buy stop/target", sp == 990.0 and tp == 1020.0 and td == 20.0)

    # config.json defaults: paper + fixed 1 lot
    file_defaults = load_config_file(DEFAULT_CONFIG_FILE)
    check("config mode paper", file_defaults.get("mode") == "paper")
    check("config fixed_lots 1", int(file_defaults.get("fixed_lots", 0)) == 1)
    check("config sizing fixed", file_defaults.get("sizing_mode") == "fixed")
    rm = RiskManager(EngineConfig.from_file(
        DEFAULT_CONFIG_FILE, maximum_spread_points=1000, margin_pct_of_notional=0.0,
        atr_period=5, value_per_price_unit=10.0, enforce_risk_budget=True,
    ))
    plan = rm.build_trade_plan("BUY", 72000.0, 72001.0, candles)
    check("fixed 1 lot plan", (not plan.rejected) and plan.lots == 1, plan.reject_reason)
    # Fixed lot that blows the risk budget must be rejected
    rm_big = RiskManager(EngineConfig.from_mapping(
        sizing_mode="fixed", fixed_lots=1, risk_per_trade_pct=0.25,
        initial_equity=500_000, value_per_price_unit=100.0,
        atr_stop_multiplier=1.5, atr_period=5, enforce_risk_budget=True,
        maximum_spread_points=1000, margin_pct_of_notional=0.0,
    ))
    # Inflate ATR via wide range candles
    wide = candles.copy()
    wide["high"] = wide["close"] + 50
    wide["low"] = wide["close"] - 50
    plan_big = rm_big.build_trade_plan("BUY", 72000.0, 72001.0, wide)
    check("fixed lot rejects over budget",
          plan_big.rejected and "exceeds_risk_budget" in plan_big.reject_reason,
          plan_big.reject_reason)

    # news decision engine: block around high-impact USD release
    nd = NewsDecisionEngine(EngineConfig.from_mapping(news_mode="filtered"), events=[ev])
    check("news engine blocks at release",
          not nd.evaluate_news(now).allow_new_entries)
    # Past news block AND surprise reaction window (default 180m)
    check("news engine clear after window",
          nd.evaluate_news(now + timedelta(hours=4)).allow_new_entries)

    # calendar feed manager (CSV-only path, no API keys required)
    cal_path = Path(DEFAULT_CALENDAR)
    check(
        "sample_data/calendar.csv present",
        cal_path.is_file() and cal_path.stat().st_size > 0,
        f"path={cal_path} exists={cal_path.exists()}",
    )
    mgr = CalendarFeedManager(
        local_calendar_path=str(cal_path),
        primary="csv", secondary="", enable_stream=False,
        schedule_refresh_hours=6.0,
    )
    n = mgr.load_calendar()
    check("calendar manager loads local CSV", n >= 1,
          f"n={n} path={cal_path} exists={cal_path.exists()}")
    # Explicit CSV primary: start() baseline + freshness must be OK
    mgr.start()
    fresh = mgr.calendar_freshness(max_age_hours=12.0)
    check(
        "csv calendar freshness ok after start",
        fresh.get("ok") is True and int(fresh.get("event_count") or 0) >= 1,
        str(fresh),
    )
    # Simulate failed remote: age clock must not advance on unhealthy refresh
    aged = datetime.now(tz=UTC) - timedelta(hours=13)
    mgr.last_successful_refresh = aged
    mgr._last_schedule_refresh = aged
    mgr.stats["last_refresh_healthy"] = False
    stale = mgr.calendar_freshness(max_age_hours=12.0)
    check(
        "calendar_max_age_hours blocks stale schedule",
        stale.get("ok") is False and any("age_" in r for r in (stale.get("reasons") or [])),
        str(stale),
    )
    status = mgr.tick()
    check("calendar manager tick", "event_count" in status)

    # Historical calendar without retrieved_at must still be visible in backtests
    # (unknown retrieval ⇒ visible; actuals still gated by release time).
    from feeds import events_known_as_of, normalize_event
    bare = normalize_event({
        "event_id": "hist-cpi", "event_name": "US CPI m/m", "currency": "USD",
        "importance": "high",
        "scheduled_time_utc": "2025-06-01T12:30:00+00:00",
        "forecast": 0.2, "previous": 0.1,
        # no retrieved_at_utc
    })
    check("no retrieved_at stays None (not now())", bare.retrieved_at_utc is None)
    past = datetime(2025, 6, 15, tzinfo=UTC)
    known = events_known_as_of([bare], past)
    check("historical event visible without retrieved_at", len(known) == 1)
    check("near-release false far from events or boolean",
          isinstance(status.get("near_release"), bool))

    # release surprise → gold bias
    from strategy import (
        classify_surprise,
        combine_bias,
        event_polarity,
        surprise_to_gold_bias,
    )
    check("cpi hawkish polarity", event_polarity("US CPI m/m") == "hawkish_on_beat")
    check("unemployment dovish polarity",
          event_polarity("US Unemployment Rate") == "dovish_on_beat")
    check("cpi beat → bearish gold",
          surprise_to_gold_bias("US CPI m/m", "beat") == "bearish")
    check("cpi miss → bullish gold",
          surprise_to_gold_bias("US CPI m/m", "miss") == "bullish")
    check("unemployment beat → bullish gold",
          surprise_to_gold_bias("US Unemployment Rate", "beat") == "bullish")
    check("classify beat", classify_surprise(0.4, 0.3) == "beat")
    check("classify inline", classify_surprise(0.3, 0.3) == "inline")
    check("never invent actual", classify_surprise(None, 0.3) == "unknown")
    check("unknown polarity → no gold bias",
          surprise_to_gold_bias("Unknown Widget Sales", "beat") == "unknown")
    check("combine agree", combine_bias("bearish", "bearish")[0] == "bearish")
    check("combine disagree", combine_bias("bullish", "bearish")[0] == "mixed")
    check("combine unknown surprise → neutral",
          combine_bias("bearish", "unknown") == ("neutral", "surprise_unknown"))

    released = EconomicEvent(
        "cpi1", "US CPI m/m", "USD", "high",
        scheduled_time_utc=now - timedelta(minutes=45),
        forecast=0.3, previous=0.2, actual=0.5, status="released",
        actual_release_time_utc=now - timedelta(minutes=45),
    )
    # Hot CPI + DXY/yields up + bearish breakout → SELL after block
    bear_candles = candles.copy()
    prev_low = float(bear_candles.iloc[-7:-1]["low"].min())
    bear_candles.iloc[-1, bear_candles.columns.get_loc("close")] = prev_low - 1.0
    bear_candles.iloc[-1, bear_candles.columns.get_loc("low")] = prev_low - 2.0
    dxy_up = pd.Series(
        [100.0, 100.10],
        index=pd.to_datetime(["2026-01-01 12:00:00+00:00", "2026-01-01 12:15:00+00:00"], utc=True),
    )
    yld_up = pd.Series(
        [4.00, 4.05],
        index=pd.to_datetime(["2026-01-01 12:00:00+00:00", "2026-01-01 12:15:00+00:00"], utc=True),
    )
    # Align macro timestamps near `now` for evaluate_macros
    t1 = pd.Timestamp(now - timedelta(minutes=20))
    t2 = pd.Timestamp(now - timedelta(minutes=1))
    dxy_up.index = pd.DatetimeIndex([t1, t2], tz="UTC")
    yld_up.index = pd.DatetimeIndex([t1, t2], tz="UTC")
    nd_sur = NewsDecisionEngine(
        EngineConfig.from_mapping(
            news_mode="filtered", use_release_surprise=True,
            surprise_reaction_minutes=180, require_surprise_macro_agree=True,
            stale_data_minutes=60, macro_stale_minutes=4320,
            macro_trade_max_age_minutes=10_000, use_real_yield=False,
            gold_breakout_lookback_bars=6, strategy_mode="legacy_macro",
        ),
        events=[released],
    )
    sur_ctx = nd_sur.evaluate_news(now)
    check("surprise active post-block", sur_ctx.surprise_active and sur_ctx.surprise is not None)
    check("hot cpi surprise bearish",
          sur_ctx.surprise.gold_bias == "bearish", str(sur_ctx.surprise))
    dec_sur = nd_sur.decide(
        now_utc=now, dxy=dxy_up, yields=yld_up, gold_candles=bear_candles,
        daily_loss_limit_reached=False, has_open_position=False, gold_data_valid=True,
    )
    check("sell on surprise+macro+breakout",
          dec_sur.decision == "SELL", f"{dec_sur.decision}/{dec_sur.reason}")

    # Inline surprise → no trade even with macro bias
    inline_ev = EconomicEvent(
        "cpi2", "US CPI m/m", "USD", "high",
        scheduled_time_utc=now - timedelta(minutes=45),
        forecast=0.3, previous=0.2, actual=0.3, status="released",
        actual_release_time_utc=now - timedelta(minutes=45),
    )
    nd_inline = NewsDecisionEngine(
        EngineConfig.from_mapping(
            news_mode="filtered", use_release_surprise=True,
            surprise_reaction_minutes=180, require_surprise_macro_agree=True,
            stale_data_minutes=60, macro_stale_minutes=4320,
            macro_trade_max_age_minutes=10_000, use_real_yield=False,
            gold_breakout_lookback_bars=6, strategy_mode="legacy_macro",
        ),
        events=[inline_ev],
    )
    dec_inline = nd_inline.decide(
        now_utc=now, dxy=dxy_up, yields=yld_up, gold_candles=bear_candles,
        daily_loss_limit_reached=False, has_open_position=False, gold_data_valid=True,
    )
    check("inline surprise blocks",
          dec_inline.decision == "NO_TRADE", dec_inline.reason)

    # generate_signal gates (legacy path; keep lookback aligned with fixture length)
    cfg = EngineConfig.from_mapping(
        news_mode="filtered", gold_breakout_lookback_bars=6, strategy_mode="legacy_macro",
    )
    res = generate_signal(
        now_utc=now, events=[ev], dxy_direction="down", yield_direction="down",
        gold_candles=candles, data_is_valid=True, daily_loss_limit_reached=False,
        has_open_position=False, cfg=cfg,
    )
    check("news block blocks entry", res.decision == "NO_TRADE" and res.reason == "news_block")

    quiet = now + timedelta(hours=3)
    res2 = generate_signal(
        now_utc=quiet, events=[ev], dxy_direction="down", yield_direction="down",
        gold_candles=candles, data_is_valid=True, daily_loss_limit_reached=False,
        has_open_position=False, cfg=cfg,
    )
    check("buy when bias+breakout", res2.decision == "BUY", res2.reason)

    # daily-loss limit rolls over on new trading day
    from datetime import timezone as _tz
    rm_day = RiskManager(EngineConfig.from_mapping(
        maximum_daily_loss_pct=1.0, initial_equity=100_000,
        trading_day_tz="UTC",
    ))
    t_day1 = datetime(2026, 3, 10, 10, 0, tzinfo=_tz.utc)
    rm_day.roll_day(t_day1)
    rm_day.book_realized(-2_000)  # 2% loss vs 1% limit
    check("daily loss triggers", rm_day.daily_loss_reached(t_day1))
    t_day2 = datetime(2026, 3, 11, 10, 0, tzinfo=_tz.utc)
    check("daily loss clears next day", not rm_day.daily_loss_reached(t_day2))

    # observation frame drops stale macros
    from feeds import build_observation_frame
    g_idx = pd.date_range("2026-01-05", periods=3, freq="5min", tz="UTC")
    gold_obs = pd.DataFrame({
        "open": [100, 101, 102], "high": [101, 102, 103],
        "low": [99, 100, 101], "close": [100.5, 101.5, 102.5],
        "volume": [1, 1, 1],
    }, index=g_idx)
    stale_dxy = pd.Series(
        [104.0], index=pd.DatetimeIndex([g_idx[0] - pd.Timedelta(hours=3)], tz="UTC"),
    )
    fresh_yld = pd.Series(
        [4.2], index=pd.DatetimeIndex([g_idx[-1] - pd.Timedelta(minutes=5)], tz="UTC"),
    )
    obs = build_observation_frame(
        gold_obs, stale_dxy, fresh_yld, max_age_minutes=60,
    )
    check("stale dxy dropped in observation",
          obs["dxy"].isna().all(), f"dxy={list(obs['dxy'])}")
    check("fresh yield kept in observation",
          obs["us10y_yield_pct"].notna().any())

    # runtime state round-trip
    from trading import ExecutionEngine, Position
    from pathlib import Path as _Path
    import tempfile
    rm_rt = RiskManager(EngineConfig.from_mapping(initial_equity=100_000))
    exe_rt = ExecutionEngine(rm_rt, live=False)
    exe_rt.position = Position(
        side="BUY", quantity=1, lots=1, entry_price=72000.0,
        stop_price=71900.0, target_price=72200.0,
        entry_time_utc=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        instrument_key="TEST|GOLD",
    )
    exe_rt.risk.open_positions = 1
    with tempfile.TemporaryDirectory() as tmp:
        p = _Path(tmp) / "runtime_state.json"
        exe_rt.save_runtime_state(p)
        exe2 = ExecutionEngine(RiskManager(EngineConfig.from_mapping()), live=False)
        st = exe2.load_runtime_state(p)
        check("runtime_state restore", st == "ok" and exe2.position is not None
              and exe2.position.side == "BUY" and exe2.emergency_halt)
        rep = exe2.reconcile_broker("TEST|GOLD", clear_halt_on_ok=True)
        check("paper reconcile clears halt", rep.get("ok") and not exe2.emergency_halt)
        # Corrupt state must fail closed
        bad = _Path(tmp) / "bad_state.json"
        bad.write_text("{not json", encoding="utf-8")
        exe3 = ExecutionEngine(RiskManager(EngineConfig.from_mapping()), live=False)
        st3 = exe3.load_runtime_state(bad)
        check("corrupt state fail-closed",
              st3 == "corrupt" and exe3.state_corrupt and exe3.emergency_halt)
        # Quantity convention: broker qty == lots
        plan_q = RiskManager(EngineConfig.from_mapping(
            sizing_mode="fixed", fixed_lots=2, lot_size=100,
            maximum_spread_points=1000, margin_pct_of_notional=0.0,
            atr_period=5, value_per_price_unit=10.0, enforce_risk_budget=False,
            risk_per_trade_pct=5.0, initial_equity=500_000,
        )).build_trade_plan("BUY", 72000.0, 72001.0, candles)
        check("broker qty equals lots (not lots*lot_size)",
              (not plan_q.rejected) and plan_q.quantity == plan_q.lots == 2,
              f"qty={plan_q.quantity} lots={plan_q.lots} rej={plan_q.reject_reason}")
        # Product validation
        try:
            EngineConfig.from_mapping(product="M")
            check("reject legacy product M", False)
        except SystemExit:
            check("reject legacy product M", True)
        # Session gate
        noon = datetime(2026, 3, 10, 6, 30, tzinfo=UTC)  # 12:00 IST
        night = datetime(2026, 3, 10, 18, 30, tzinfo=UTC)  # 00:00 IST next
        cfg_sess = EngineConfig.from_mapping(
            market_open="09:00", entry_cutoff="23:00", market_close="23:30",
            trading_day_tz="Asia/Kolkata",
        )
        check("session allows midday IST", _in_entry_window(noon, cfg_sess))
        check("session blocks midnight IST", not _in_entry_window(night, cfg_sess))
        # CancelOutcome classification
        from upstox_client import CancelOutcome, SUPPORTED_PRODUCTS
        check("supported products I/D/MTF", SUPPORTED_PRODUCTS == frozenset({"I", "D", "MTF"}))
        check("cancel outcomes distinct",
              CancelOutcome.FILLED != CancelOutcome.CONFIRMED_CANCELLED)
        # Direction-aware tick rounding
        from trading import _round_stop_target
        sp, tp, eff = _round_stop_target("BUY", 1000.0, 995.3, 1010.7, 1.0)
        check("buy stop rounds adversely (down)", sp <= 995.0)
        # Filled stop is not active protection
        class _FakeClient:
            def order_details(self, oid):
                return {"status": "complete", "filled_quantity": 1, "quantity": 1}
        exe_stop = ExecutionEngine(RiskManager(EngineConfig.from_mapping()), live=True,
                                   persist_state=False)
        exe_stop.client = _FakeClient()
        check("filled stop != active protection",
              exe_stop._verify_stop_active("x", 1) == "filled")
        # Missing qty must not count as active protection
        class _FakeClientNoQty:
            def order_details(self, oid):
                return {"status": "trigger_pending", "quantity": None}
        exe_nq = ExecutionEngine(RiskManager(EngineConfig.from_mapping()), live=True,
                                persist_state=False)
        exe_nq.client = _FakeClientNoQty()
        check("zero/missing stop qty not active",
              exe_nq._verify_stop_active("x", 1) is False)
        # Risk ledger survives
        rm_led = RiskManager(EngineConfig.from_mapping(initial_equity=100_000))
        rm_led.roll_day(datetime(2026, 3, 10, 10, 0, tzinfo=UTC))
        rm_led.book_realized(-1500)
        exe_led = ExecutionEngine(rm_led, persist_state=False)
        led_path = _Path(tmp) / "risk_ledger.json"
        exe_led.save_risk_ledger(led_path)
        rm2 = RiskManager(EngineConfig.from_mapping(initial_equity=100_000))
        exe_led2 = ExecutionEngine(rm2, persist_state=False)
        check("risk ledger restore", exe_led2.load_risk_ledger(led_path)
              and abs(exe_led2.risk.equity - 98_500) < 0.01)
        # Candle validation rejects duplicates / non-monotonic
        from feeds import validate_candle_frame
        bad_idx = pd.DatetimeIndex(
            ["2026-01-05 10:00:00+00:00", "2026-01-05 10:00:00+00:00"]
        )
        bad_c = pd.DataFrame(
            {"open": [1, 1], "high": [2, 2], "low": [0.5, 0.5], "close": [1, 1]},
            index=bad_idx,
        )
        check("reject duplicate candle timestamps", not validate_candle_frame(bad_c))
        # Strict PIT: unknown retrieval cannot unlock actuals via scheduled time alone
        from feeds import events_known_as_of as _eka
        pit_ev = EconomicEvent(
            "pit1", "US CPI", "USD", "high",
            scheduled_time_utc=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
            actual=0.4, status="released", retrieved_at_utc=None,
            actual_release_time_utc=None,
        )
        known_strict = _eka([pit_ev], datetime(2026, 1, 6, tzinfo=UTC), strict_retrieval=True)
        check("strict PIT excludes event without retrieved_at",
              len(known_strict) == 0)
        pit_ev2 = EconomicEvent(
            "pit2", "US CPI", "USD", "high",
            scheduled_time_utc=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
            actual=0.4, status="released",
            retrieved_at_utc=datetime(2026, 1, 4, tzinfo=UTC),
            actual_release_time_utc=None,
        )
        known_strict2 = _eka([pit_ev2], datetime(2026, 1, 6, tzinfo=UTC), strict_retrieval=True)
        check("strict PIT hides actual without release timestamp",
              len(known_strict2) == 1 and known_strict2[0].actual is None)
        # Unknown config keys rejected
        try:
            EngineConfig.from_mapping({"mode": "paper", "typo_key_xyz": 1})
            check("reject unknown config keys", False)
        except SystemExit:
            check("reject unknown config keys", True)
        # surprise_required blocks when actual missing in reaction window
        pend_t = datetime(2026, 3, 10, 14, 0, tzinfo=UTC)
        pend_ev = EconomicEvent(
            "pend1", "US CPI m/m", "USD", "high",
            scheduled_time_utc=pend_t - timedelta(minutes=30),
            forecast=0.3, actual=None, status="scheduled",
            retrieved_at_utc=pend_t - timedelta(days=1),
        )
        check(
            "pending release detected without actual",
            find_pending_release_in_window(
                pend_t, [pend_ev], reaction_minutes=180, block_after_minutes=15,
            ) is not None,
        )
        eng_p = NewsDecisionEngine(EngineConfig.from_mapping(
            surprise_required_in_window=True, use_release_surprise=True,
            news_block_after_minutes=15, surprise_reaction_minutes=180,
        ))
        eng_p.set_events([pend_ev])
        ctx_p = eng_p.evaluate_news(pend_t)
        check(
            "surprise_required blocks without actual",
            (not ctx_p.allow_new_entries) and "surprise_pending" in (ctx_p.reason or ""),
            ctx_p.reason,
        )
        # Stop missing trigger → not protection
        class _FakeClientNoTrig:
            def order_details(self, oid):
                return {
                    "status": "trigger_pending",
                    "instrument_token": "MCX|TEST",
                    "transaction_type": "SELL",
                    "order_type": "SL-M",
                    "quantity": 1,
                    "trigger_price": None,
                }
        exe_nt = ExecutionEngine(RiskManager(EngineConfig.from_mapping()), live=True,
                                 persist_state=False)
        exe_nt.client = _FakeClientNoTrig()
        check(
            "stop missing trigger not active",
            exe_nt._verify_stop_active(
                "oid1", 1, instrument_key="MCX|TEST", exit_side="SELL",
                trigger_price=100.0, tick_size=1.0,
            ) is False,
        )
        # real_yield required → mixed when unknown
        check(
            "use_real_yield unknown blocks bias",
            get_gold_bias("down", "down", "unknown", True) == "mixed",
        )
        # Observation age comes from series index — not wall-clock fetch time
        from feeds import series_observation_age_minutes
        stale_idx = pd.DatetimeIndex([datetime(2026, 1, 1, tzinfo=UTC)])
        stale_s = pd.Series([100.0], index=stale_idx)
        age_s = series_observation_age_minutes(stale_s, datetime(2026, 1, 2, tzinfo=UTC))
        check(
            "macro observation age from series index",
            age_s is not None and age_s >= 24 * 60 - 1,
            f"age={age_s}",
        )
        # Affordability helper rejects GOLD @ 5L / 10% margin
        aff = _check_min_order_affordable(
            EngineConfig.from_mapping(
                gold_symbol="GOLD", value_per_price_unit=100.0,
                initial_equity=500_000, margin_pct_of_notional=0.10,
                fixed_lots=1, sizing_mode="fixed", enforce_risk_budget=False,
                maximum_spread_points=1000, atr_period=5,
            ),
            RiskManager(EngineConfig.from_mapping(
                gold_symbol="GOLD", value_per_price_unit=100.0,
                initial_equity=500_000, margin_pct_of_notional=0.10,
                fixed_lots=1, sizing_mode="fixed", enforce_risk_budget=False,
                maximum_spread_points=1000, atr_period=5,
            )),
        )
        check("GOLD unaffordable at default equity/margin", not aff["ok"])
        # Default GOLDM is affordable
        aff2 = _check_min_order_affordable(
            EngineConfig.from_mapping(),
            RiskManager(EngineConfig.from_mapping()),
        )
        check("default GOLDM affordable", aff2["ok"], aff2["reason"])

    # New modules: market signals / news assessment / gates / journal
    from market_signals import evaluate_market_signal, classify_breakout_5m
    from news_assessment import assess_news, reco_to_score, news_supports_side, NewsAssessment
    from decision_gates import evaluate_entry_gates
    from trade_journal import TradeJournal
    check("reco Strong Buy = +2", reco_to_score("Strong Buy") == 2)
    check("reco Sell = -1", reco_to_score("Sell") == -1)
    # Breakout must exclude evaluation candle (valid OHLC throughout)
    idx20 = pd.date_range("2026-01-05", periods=25, freq="5min", tz="UTC")
    closes = list(range(100, 125))
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    # Last bar breaks above prior window high (exclude self); keep OHLC consistent
    closes[-1] = 130
    highs[-1] = 131
    lows[-1] = 124
    brk_df = pd.DataFrame({
        "open": closes, "high": highs, "low": lows, "close": closes, "volume": [1] * 25,
    }, index=idx20)
    brk, lvl, _ = classify_breakout_5m(brk_df, lookback=20)
    check("5m breakout up excludes self", brk == "up", f"brk={brk} lvl={lvl}")
    na = NewsAssessment(bias="bullish", score=1.5, fresh=True, conflicting=False)
    check("news supports BUY", news_supports_side(na, "BUY"))
    check("news rejects SELL when bullish", not news_supports_side(na, "SELL"))
    from market_signals import MarketSignal
    msig_ok = MarketSignal(
        direction="bullish", trend="bullish", breakout="up", regime="trending",
        valid=True, data_valid=True, signal_id="up:test",
    )
    gate = evaluate_entry_gates(
        now_utc=datetime(2026, 3, 10, 6, 30, tzinfo=UTC),
        signal=msig_ok, news=na,
        session_ok=True, mcx_data_valid=True,
        event_block_active=False, broker_ok=True, risk_ok=True,
        require_news_confirmation=True,
    )
    check("all gates pass → BUY", gate.action == "BUY", gate.reason)
    gate_news = evaluate_entry_gates(
        now_utc=datetime(2026, 3, 10, 6, 30, tzinfo=UTC),
        signal=msig_ok,
        news=NewsAssessment(bias="unknown", fresh=False),
        session_ok=True, mcx_data_valid=True,
        event_block_active=False, broker_ok=True, risk_ok=True,
        require_news_confirmation=True,
    )
    check(
        "news gate blocks without confirmation",
        gate_news.action == "WAIT" and str(gate_news.reason).startswith("news_gate"),
        gate_news.reason,
    )
    import tempfile as _tmpmod
    with _tmpmod.TemporaryDirectory() as tmpj:
        tj = TradeJournal(Path(tmpj) / "tj.db")
        tj.log_decision(gate)
        tj.log_signal(msig_ok)
        check("trade journal writes", (Path(tmpj) / "tj.db").exists())
    # Empty news db → unknown (not confirmatory)
    miss = assess_news(Path("no_such_gold_signals.db"))
    check("missing news db → unknown", miss.bias == "unknown")

    # Session flatten cutoff helper
    flat_cfg = EngineConfig.from_mapping(
        market_open="09:00", market_close="23:30", entry_cutoff="23:00",
        flatten_before_close_minutes=5, trading_day_tz="Asia/Kolkata",
    )
    # 23:26 IST ≈ past flatten (23:25); use a known UTC offset day
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    before_flat = datetime(2026, 3, 10, 23, 20, tzinfo=ist).astimezone(UTC)
    after_flat = datetime(2026, 3, 10, 23, 26, tzinfo=ist).astimezone(UTC)
    check("flatten cutoff not yet", not _past_flatten_cutoff(before_flat, flat_cfg))
    check("flatten cutoff active", _past_flatten_cutoff(after_flat, flat_cfg))
    check("live_armed default false",
          EngineConfig.from_file().live_armed is False)

    # synthetic backtest end-to-end
    data = generate_synthetic_dataset(n_bars=600, seed=7)
    cfg_bt = EngineConfig.from_mapping(
        news_mode="aware", initial_equity=500_000,
        risk_per_trade_pct=1.0, maximum_spread_points=1000,
        # GOLDM-scale multiplier so Rs 5L can clear the margin gate in the demo.
        value_per_price_unit=10.0, estimated_cost_per_unit=5.0,
        maximum_daily_loss_pct=5.0, margin_pct_of_notional=0.05,
        backtest_slippage_points=1.0, backtest_fee_per_lot=2.0,
        strategy_mode="trend_breakout",
        gold_breakout_lookback_bars=20,
        require_news_confirmation=False,
    )
    result = run_backtest(
        data["gold"], data["dxy"], data["yields"], data["events"],
        cfg=cfg_bt, real_yields=data["real_yields"], bid_ask_spread=2.0,
    )
    check("backtest runs", result.performance is not None)
    check("rejects recorded", result.performance.reject_count > 0)
    check("synthetic produces trades", result.performance.trade_count >= 1,
          f"trades={result.performance.trade_count}")
    check("backtest meta records slippage",
          result.meta.get("slippage_points") == 1.0)
    check("backtest uses next-bar open entry",
          result.meta.get("entry_model") == "next_bar_open")
    check("backtest shares macro_trade_max_age",
          result.meta.get("macro_max_age_minutes") == float(
              getattr(cfg_bt, "macro_trade_max_age_minutes", 60) or 60
          ))
    # Out-of-sample split: second half must still run without crash
    gold_all = data["gold"]
    mid = gold_all.index[len(gold_all) // 2]
    oos = run_backtest(
        gold_all.loc[mid:], data["dxy"], data["yields"], data["events"],
        cfg=cfg_bt, real_yields=data["real_yields"], bid_ask_spread=2.0,
    )
    check("OOS backtest runs", oos.performance is not None)
    print(result.performance)

    # front-month chain helper (offline, no network)
    from upstox_client import list_mcx_gold_futures
    fake_master = []
    for i, day in enumerate((5, 20, 35)):
        fake_master.append({
            "exchange": "MCX", "instrument_type": "FUT",
            "trading_symbol": f"GOLD FUT {i}",
            "instrument_key": f"MCX_FO|GOLD{i}",
            "expiry": int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
                      + day * 86_400_000,
            "lot_size": 1, "tick_size": 1.0, "name": "GOLD",
        })
    chain = list_mcx_gold_futures(
        fake_master, "GOLD",
        as_of=date(2026, 1, 1), until=date(2026, 1, 25),
    )
    check("contract roll chain length", len(chain) >= 2, f"n={len(chain)}")

    # Upstox quote payloads use symbol keys, not instrument_key
    from upstox_client import UpstoxClient
    qrow = UpstoxClient._extract_quote_row(
        {"data": {
            "MCX_FO:GOLDM26NOVFUT": {
                "last_price": 100.0,
                "instrument_token": "MCX_FO|571445",
                "depth": {"buy": [{"price": 99.0}], "sell": [{"price": 101.0}]},
            }
        }},
        "MCX_FO|571445",
    )
    check("quote extract by instrument_token", qrow is not None and qrow["last_price"] == 100.0)

    if errors:
        print(f"\n{len(errors)} failure(s): {errors}")
        return 1
    print("\nAll selftests passed.")
    return 0


# -------------------------------------------------------------------------------- live/paper


def build_cfg(args: argparse.Namespace, file_cfg: dict | None = None) -> EngineConfig:
    """Merge config.json with CLI overrides. env.txt always comes from repo root."""
    file_cfg = file_cfg if file_cfg is not None else load_config_file(
        getattr(args, "config", None) or DEFAULT_CONFIG_FILE
    )
    overrides: dict = {"env_file": str(ENV_FILE)}

    if getattr(args, "mode", "") in ("paper", "live"):
        overrides["mode"] = args.mode
    if getattr(args, "lots", None) is not None:
        overrides["fixed_lots"] = int(args.lots)
        overrides["sizing_mode"] = "fixed"
    if args.symbol:
        overrides["gold_symbol"] = args.symbol
    if args.news_mode:
        overrides["news_mode"] = args.news_mode
    if args.dxy_csv:
        overrides["dxy_csv"] = args.dxy_csv
    if args.yield_csv:
        overrides["yield_csv"] = args.yield_csv
    if args.gold_csv:
        overrides["gold_csv"] = args.gold_csv
    if args.instrument_key:
        overrides["instrument_key_override"] = args.instrument_key
    if getattr(args, "equity_explicit", False):
        overrides["initial_equity"] = args.equity
    if args.spread is not None:
        overrides["maximum_spread_points"] = args.spread
    if getattr(args, "yahoo_macros", False):
        overrides["yahoo_macros"] = True
    if getattr(args, "no_producers", False):
        overrides["run_embedded_producers"] = False

    calendar = getattr(args, "calendar", None)
    if calendar and str(calendar) != str(DEFAULT_CALENDAR):
        cal_path = Path(calendar)
        if not cal_path.is_absolute():
            cal_path = _ENGINE_DIR / cal_path
        overrides["calendar_path"] = str(cal_path)
    elif file_cfg.get("calendar_path"):
        cal_path = Path(str(file_cfg["calendar_path"]))
        if not cal_path.is_absolute():
            cal_path = _ENGINE_DIR / cal_path
        overrides["calendar_path"] = str(cal_path)

    cfg = EngineConfig.from_mapping(file_cfg, **overrides)
    cfg.apply_mcx_symbol(cfg.gold_symbol)
    return cfg


def run_paper_or_live(cfg: EngineConfig, *, once: bool = False) -> int:
    """
    Architecture:
      1. Startup — load_calendar, connect streams, load macros, resolve instrument
      2. Loop — process calendar updates, refresh market data, decide, size, paper/live order
      3. Reconnect is handled inside CalendarFeedManager / TE stream
    """
    # Mark producers stopping as soon as Ctrl-C hits (Chromium often dies in the same tick).
    def _on_interrupt(signum, frame):  # noqa: ANN001
        try:
            PRODUCER_HUB.request_stop()
        except Exception:  # noqa: BLE001
            pass
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _on_interrupt)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_interrupt)
    except Exception:  # noqa: BLE001
        pass

    live = cfg.is_live
    yahoo_macros = bool(cfg.yahoo_macros)
    startup = CycleDiagnostics(0, mode=cfg.mode)
    startup.begin()
    log.info("  STARTUP diagnostics — validating every dependency before the loop")

    # --- credentials ---
    try:
        resolve_credentials(cfg)
        cred_status, cred_detail = credential_health(cfg, str(ENV_FILE))
        startup.step("CREDENTIALS", cred_status, cred_detail)
    except SystemExit as exc:
        startup.step("CREDENTIALS", FAIL, str(exc))
        startup.end()
        raise

    # --- calendar feeds ---
    cal_mgr = CalendarFeedManager(
        local_calendar_path=cfg.calendar_path,
        primary=cfg.calendar_provider,
        secondary=cfg.calendar_secondary,
        enable_stream=bool(cfg.calendar_stream),
        schedule_refresh_hours=float(cfg.calendar_schedule_refresh_hours),
        near_release_window_minutes=float(cfg.calendar_near_release_window_minutes),
        near_release_poll_seconds=float(cfg.calendar_near_release_poll_seconds),
        horizon_days=int(cfg.calendar_horizon_days),
        importance_required=cfg.news_importance_required,
    )
    try:
        cal_mgr.start()
        events = cal_mgr.snapshot()
        high_usd = sum(
            1 for e in events
            if (e.currency or "").upper() == "USD"
            and (e.importance or "").lower() == (cfg.news_importance_required or "high").lower()
        )
        with_actual = sum(1 for e in events if e.actual is not None)
        stream_on = bool(cal_mgr.stats.get("stream"))
        primary = cal_mgr.stats.get("primary") or "none"
        if not events:
            cal_status, cal_detail = FAIL, "calendar empty after startup load"
        elif primary == "csv" and not stream_on:
            cal_status = PARTIAL
            cal_detail = (
                f"events={len(events)} high_usd={high_usd} with_actual={with_actual} "
                f"primary={primary} stream=OFF — licensed TE/FMP keys missing; using local CSV"
            )
        else:
            cal_status = OK if stream_on or primary != "csv" else PARTIAL
            cal_detail = (
                f"events={len(events)} high_usd={high_usd} with_actual={with_actual} "
                f"primary={primary} secondary={cal_mgr.stats.get('secondary')} "
                f"stream={'ON' if stream_on else 'OFF'} "
                f"schedule_refresh_h={cfg.calendar_schedule_refresh_hours}"
            )
        startup.step("CALENDAR", cal_status, cal_detail)
        # Live + require_live_calendar: empty or CSV-only with zero high-USD → abort
        if live and bool(getattr(cfg, "require_live_calendar", True)):
            if not events or high_usd < 1:
                startup.step(
                    "CALENDAR.LIVE_GATE", FAIL,
                    "live entries blocked — required calendar unhealthy/empty "
                    "(set require_live_calendar=false only for explicit paper/dev)",
                )
                cal_mgr.stop()
                startup.end()
                return 2
            if primary == "csv" and not stream_on:
                startup.step(
                    "CALENDAR.LIVE_GATE", FAIL,
                    "live mode refuses CSV-only calendar fallback — add TE/FMP keys "
                    "or set require_live_calendar=false",
                )
                cal_mgr.stop()
                startup.end()
                return 2
    except Exception as exc:  # noqa: BLE001
        startup.step("CALENDAR", FAIL, f"startup failed: {exc}")
        log.exception("Calendar startup error")
        cal_mgr.stop()
        startup.end()
        return 2

    news_engine = NewsDecisionEngine(cfg, events=cal_mgr.snapshot())
    startup.step(
        "NEWS_ENGINE", OK,
        f"armed events={len(news_engine.events)} mode={cfg.news_mode} "
        f"block=±{cfg.news_block_before_minutes}/{cfg.news_block_after_minutes}m "
        f"surprise={cfg.use_release_surprise} reaction={cfg.surprise_reaction_minutes}m",
    )

    # --- Upstox instrument ---
    client = UpstoxClient(cfg)
    try:
        master = load_instrument_master(cfg.instrument_cache)
        cache_path = Path(cfg.instrument_cache)
        age_h = None
        if cache_path.exists():
            age_h = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600.0
        max_age_h = float(getattr(cfg, "instrument_master_max_age_hours", 36) or 36)
        master_st = OK if master else FAIL
        if master and age_h is not None and age_h > max_age_h:
            master_st = WARN
        startup.step(
            "INSTRUMENT_MASTER",
            master_st,
            f"rows={len(master) if master is not None else 0} "
            f"age_h={fmt_num(age_h, 1)} max={max_age_h} cache={cfg.instrument_cache}",
        )
        if live and (not master or (age_h is not None and age_h > max_age_h * 2)):
            startup.step(
                "INSTRUMENT_MASTER.LIVE", FAIL,
                "live requires fresh instrument master — refresh cache before arming",
            )
            cal_mgr.stop()
            startup.end()
            return 2
        fut = resolve_mcx_gold_future(
            master, cfg.gold_symbol, override_key=cfg.instrument_key_override,
        )
        ctx = mcx_context_from_instrument(fut, cfg.gold_symbol)
        cfg.value_per_price_unit = ctx.value_per_price_unit
        cfg.lot_size = ctx.lot_size
        startup.step(
            "GOLD_CONTRACT", OK,
            f"{ctx.trading_symbol} key={ctx.instrument_key} lot={ctx.lot_size} "
            f"tick={ctx.tick_size} value/unit={cfg.value_per_price_unit}",
        )
    except Exception as exc:  # noqa: BLE001
        startup.step("GOLD_CONTRACT", FAIL, f"{exc}")
        log.exception("Instrument resolve failed")
        cal_mgr.stop()
        startup.end()
        return 2

    startup.step(
        "CONFIG", INFO,
        f"mode={cfg.mode} sizing={cfg.sizing_mode} lots={cfg.fixed_lots} "
        f"tf={cfg.timeframe_minutes}m poll={cfg.poll_seconds}s "
        f"yahoo_macros={yahoo_macros} spread_max={cfg.maximum_spread_points}",
    )

    # --- macros ---
    dxy, yields = load_macros(
        cfg.dxy_csv, cfg.yield_csv, allow_yahoo=yahoo_macros, persist_cache=True,
    )
    now0 = datetime.now(tz=UTC)
    dxy_src = "csv" if cfg.dxy_csv else ("yahoo/cache" if yahoo_macros else "none")
    yld_src = "csv" if cfg.yield_csv else ("yahoo/cache" if yahoo_macros else "none")
    dxy_st, dxy_det = series_health(
        "DXY", dxy, now0, stale_limit_minutes=float(cfg.macro_stale_minutes), source=dxy_src,
    )
    yld_st, yld_det = series_health(
        "US10Y", yields, now0, stale_limit_minutes=float(cfg.macro_stale_minutes), source=yld_src,
    )
    startup.step("MACROS.DXY", dxy_st, dxy_det)
    startup.step("MACROS.US10Y", yld_st, yld_det)
    if dxy.empty or yields.empty:
        startup.step(
            "MACROS", FAIL,
            "DXY and/or yields empty — set dxy_csv/yield_csv or enable yahoo_macros",
        )
        startup.end()
        cal_mgr.stop()
        return 2

    if live and not bool(getattr(cfg, "live_armed", False)):
        startup.step(
            "LIVE_ARM", FAIL,
            "mode=live but live_armed=false — set live_armed=true in config after "
            "operational checklist (separate arming gate)",
        )
        cal_mgr.stop()
        startup.end()
        return 2

    # Single-instance lock (P1.23) — prevent two engines racing the same account.
    lock_path = Path(cfg.instrument_cache).parent / "engine.lock"
    lock_fh = _acquire_process_lock(lock_path, instrument_key=str(ctx.instrument_key))
    if lock_fh is None:
        startup.step(
            "PROCESS_LOCK", FAIL,
            f"another engine instance holds {lock_path} — refuse dual control",
        )
        cal_mgr.stop()
        startup.end()
        return 2
    startup.step("PROCESS_LOCK", OK, f"acquired {lock_path.name}")

    risk = RiskManager(cfg)
    exe = ExecutionEngine(
        risk, client=client if live else None, live=live,
        value_per_price_unit=cfg.value_per_price_unit,
        estimated_cost_per_unit=cfg.estimated_cost_per_unit,
    )
    # Operator kill switch (P1 #52) — create gold-trade-engine/KILL_SWITCH to halt entries
    from config import KILL_SWITCH_FILE
    if KILL_SWITCH_FILE.exists():
        exe.emergency_halt = True
        startup.step(
            "KILL_SWITCH", FAIL,
            f"{KILL_SWITCH_FILE.name} present — new entries blocked; "
            "reconcile exposure then remove file to resume",
        )
    ledger_ok = exe.load_risk_ledger()
    if exe.state_corrupt:
        startup.step(
            "RISK_LEDGER", FAIL,
            "corrupt/incompatible risk ledger — fail closed before loop",
        )
        cal_mgr.stop()
        _release_process_lock(lock_fh, lock_path)
        startup.end()
        return 2
    if ledger_ok:
        startup.step(
            "RISK_LEDGER", OK,
            f"restored equity={risk.equity:,.0f} day={risk.daily.trading_day} "
            f"blocked={risk.daily.blocked}",
        )
    else:
        # Missing ledger → explicit init + mandatory broker reconcile (not implicit flat).
        risk.equity = float(cfg.initial_equity)
        risk.roll_day(datetime.now(tz=UTC))
        exe.save_risk_ledger()
        startup.step(
            "RISK_LEDGER", WARN,
            f"no prior ledger — initialized from initial_equity={cfg.initial_equity:,.0f}; "
            f"broker reconcile required",
        )

    state_status = exe.load_runtime_state()
    if state_status in ("corrupt", "incompatible"):
        startup.step(
            "RUNTIME_STATE", FAIL,
            f"{state_status} — fail closed; restore backup / reconcile broker before restart",
        )
        cal_mgr.stop()
        _release_process_lock(lock_fh, lock_path)
        startup.end()
        return 2
    if state_status == "ok":
        startup.step(
            "RUNTIME_STATE", WARN,
            f"restored halt={exe.emergency_halt} open={exe.has_open_position} "
            f"pending={bool(exe._pending_intent)} lifecycle={exe.lifecycle_state}",
        )
    else:
        startup.step("RUNTIME_STATE", OK, "no prior runtime_state.json")

    # P0.1 — ALWAYS reconcile broker before live entries (missing state ≠ flat account).
    rep = exe.reconcile_broker(
        ctx.instrument_key,
        clear_halt_on_ok=(state_status == "ok" and not live),
        discover_orphans=True,
    )
    startup.step(
        "RECONCILE",
        OK if rep.get("ok") else (WARN if not live else FAIL),
        f"local={rep.get('local_signed_qty')} broker={rep.get('broker_net_qty')} "
        f"stop_ok={rep.get('stop_ok', '-')} orphans={rep.get('orphan_stops', 0)} "
        f"reason={rep.get('reason', 'ok')} halt={exe.emergency_halt}",
    )
    if live and not rep.get("ok"):
        startup.step(
            "RECONCILE.LIVE", FAIL,
            "unexplained broker exposure or unknown positions — recovery required",
        )
        cal_mgr.stop()
        _release_process_lock(lock_fh, lock_path)
        startup.end()
        return 2
    if live and state_status == "missing" and int(rep.get("broker_net_qty") or 0) != 0:
        startup.step(
            "RECONCILE.LIVE", FAIL,
            "local state missing but broker holds a position — refuse auto-start",
        )
        cal_mgr.stop()
        _release_process_lock(lock_fh, lock_path)
        startup.end()
        return 2

    # P1.3 — minimum order must be affordable under configured risk/margin model.
    afford = _check_min_order_affordable(cfg, risk, float(ctx.tick_size or 1.0))
    if not afford["ok"]:
        startup.step("AFFORDABILITY", FAIL, afford["reason"])
        if live:
            cal_mgr.stop()
            _release_process_lock(lock_fh, lock_path)
            startup.end()
            return 2
        startup.step(
            "AFFORDABILITY", WARN,
            afford["reason"] + " — paper continues but entries will reject",
        )
    else:
        startup.step("AFFORDABILITY", OK, afford["reason"])

    # Expiry / roll gate (calendar days before expiry — audit §29)
    if ctx.expiry is not None:
        days_left = (ctx.expiry.date() - datetime.now(tz=UTC).date()).days
        roll_d = int(getattr(cfg, "roll_days_before_expiry", 3) or 0)
        min_d = max(
            int(getattr(cfg, "min_days_to_expiry_for_entry", roll_d) or 0),
            roll_d,
        )
        if days_left < min_d:
            startup.step(
                "EXPIRY", WARN,
                f"contract expires in {days_left}d (< roll/entry min {min_d}d) — "
                f"new entries blocked; roll policy is calendar days",
            )
            exe.emergency_halt = True
        else:
            startup.step(
                "EXPIRY", OK,
                f"days_to_expiry={days_left} roll_days={roll_d} entry_min={min_d}",
            )

    # Live: require verified broker funds/margin (audit §27)
    if live and bool(getattr(cfg, "require_broker_funds", True)):
        funds = client.get_funds() if hasattr(client, "get_funds") else None
        if not funds:
            startup.step("FUNDS", FAIL, "broker funds/margin unavailable — refuse live start")
            cal_mgr.stop()
            _release_process_lock(lock_fh, lock_path)
            startup.end()
            return 2
        avail = None
        for k in ("available_margin", "available_cash", "equity", "available"):
            if isinstance(funds, dict) and funds.get(k) is not None:
                try:
                    avail = float(funds[k])
                    break
                except (TypeError, ValueError):
                    continue
        if avail is None or not (avail > 0):
            startup.step("FUNDS", FAIL, f"cannot parse available funds from {funds!r}")
            cal_mgr.stop()
            _release_process_lock(lock_fh, lock_path)
            startup.end()
            return 2
        # Rough 1-lot margin vs available
        ref = 72_000.0
        notional = ref * float(cfg.value_per_price_unit) * float(cfg.lot_size) * float(cfg.fixed_lots)
        need = notional * float(cfg.margin_pct_of_notional) * 1.1  # 10% buffer
        if avail < need:
            startup.step(
                "FUNDS", FAIL,
                f"available={avail:,.0f} < required≈{need:,.0f} "
                f"(notional≈{notional:,.0f} @ margin {cfg.margin_pct_of_notional})",
            )
            cal_mgr.stop()
            _release_process_lock(lock_fh, lock_path)
            startup.end()
            return 2
        startup.step("FUNDS", OK, f"available_margin≈{avail:,.0f} need≈{need:,.0f}")
    startup.step(
        "RISK_EXEC", OK,
        f"equity={risk.equity:,.0f} daily_loss_pct={cfg.maximum_daily_loss_pct} "
        f"blocked={risk.daily.blocked} orders={'REAL' if live else 'SIMULATED'} "
        f"open_pos={exe.has_open_position} product={cfg.product}",
    )
    journal = TradeJournal()
    startup.step(
        "JOURNAL", OK,
        f"trade_journal={journal.path} strategy_mode={cfg.strategy_mode} "
        f"contract={cfg.gold_symbol} key={ctx.instrument_key}",
    )

    # Embed monitor-gold + commodity-events (single process day-to-day run)
    if bool(getattr(cfg, "run_embedded_producers", True)):
        try:
            token = str(getattr(cfg, "access_token", "") or "")
            PRODUCER_HUB.start(
                access_token=token,
                gold_symbol=str(cfg.gold_symbol),
                instrument_key=str(ctx.instrument_key),
                news_db=str(getattr(cfg, "news_db_path", "") or "") or str(ENGINE_DIR / "gold_signals.db"),
                monitor_refresh_seconds=float(getattr(cfg, "monitor_refresh_seconds", 60) or 60),
                news_interval_seconds=float(getattr(cfg, "news_poll_interval_seconds", 60) or 60),
                enable_monitor=bool(getattr(cfg, "enable_monitor_producer", True)),
                enable_news=bool(getattr(cfg, "enable_news_producer", True)),
                news_no_telegram=bool(getattr(cfg, "news_producer_no_telegram", False)),
            )
            pst = PRODUCER_HUB.status()
            startup.step(
                "PRODUCERS", OK,
                f"monitor={pst.get('started') and getattr(cfg, 'enable_monitor_producer', True)} "
                f"news={getattr(cfg, 'enable_news_producer', True)} "
                f"json=monitor_snapshot.json db=gold_signals.db",
            )
        except Exception as exc:  # noqa: BLE001
            startup.step("PRODUCERS", WARN, f"start failed (trading continues): {exc}")
    else:
        startup.step("PRODUCERS", INFO, "run_embedded_producers=false — run collectors separately if needed")

    startup.end()

    log.info(
        "Entering %s loop on %s — detailed per-cycle board follows every %.1fs "
        "(producers embedded=%s)",
        "LIVE" if live else "PAPER", ctx.instrument_key, cfg.poll_seconds,
        bool(getattr(cfg, "run_embedded_producers", True)),
    )
    state = {"dxy": dxy, "yields": yields, "eod_report_written": False}
    cycle_no = 0
    try:
        while True:
            cycle_no += 1
            try:
                _cycle(
                    client, ctx.instrument_key, news_engine,
                    state["dxy"], state["yields"], cfg, risk, exe,
                    cal_mgr=cal_mgr,
                    cycle_no=cycle_no,
                    yahoo_macros=yahoo_macros,
                    state=state,
                    tick_size=float(ctx.tick_size or 1.0),
                    journal=journal,
                    contract_info=ctx,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                # P0 #14: unexpected cycle exception → persistent halt + reconcile
                exe.emergency_halt = True
                crash = CycleDiagnostics(cycle_no, mode=cfg.mode)
                crash.begin()
                crash.step(
                    "CYCLE", FAIL,
                    f"unhandled exception — HALTED: {exc}",
                )
                try:
                    rep = exe.reconcile_broker(ctx.instrument_key, discover_orphans=True)
                    crash.step(
                        "CYCLE.RECONCILE",
                        OK if rep.get("ok") else FAIL,
                        f"post-crash reconcile ok={rep.get('ok')} "
                        f"broker={rep.get('broker_net_qty')}",
                    )
                except Exception as rex:  # noqa: BLE001
                    crash.step("CYCLE.RECONCILE", FAIL, f"{rex}")
                try:
                    exe.save_runtime_state()
                except Exception:  # noqa: BLE001
                    pass
                crash.end()
                log.exception("Cycle error (engine halted): %s", exc)
            if once:
                break
            time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        log.info("Ctrl-C — shutting down cleanly")
    finally:
        # Signal producers before anything else so scrapes treat closed browsers as expected.
        try:
            PRODUCER_HUB.request_stop()
        except Exception:  # noqa: BLE001
            pass
        log.info("Shutting down — persist state, keep broker protection, release lock")
        try:
            if not state.get("eod_report_written"):
                write_eod_report(
                    journal_path=journal.path,
                    trading_day=risk.trading_day_of(datetime.now(tz=UTC)),
                    equity=risk.equity,
                    realized_pnl=risk.daily.realized_pnl,
                    trades_today=risk.daily.trades_today,
                    blocked=risk.daily.blocked,
                    extra={"shutdown": True, "instrument_key": ctx.instrument_key},
                )
                state["eod_report_written"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("EOD report on shutdown failed: %s", exc)
        try:
            PRODUCER_HUB.stop(timeout=25.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("producer shutdown: %s", exc)
        try:
            exe.save_runtime_state()
            exe.save_risk_ledger()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown persist failed: %s", exc)
        # Do NOT cancel protective stops on shutdown — they must survive process exit.
        cal_mgr.stop()
        _release_process_lock(lock_fh, lock_path)
    return 0


def _publish_board(diag: "CycleDiagnostics", **fields: Any) -> None:
    """Attach / refresh the compact console BoardView on this cycle."""
    from console_board import BoardView
    base = diag.board if isinstance(diag.board, BoardView) else BoardView(
        cycle_no=diag.cycle_no, mode=diag.mode, now_utc=diag.now_utc,
    )
    for k, v in fields.items():
        if hasattr(base, k):
            setattr(base, k, v)
    diag.board = base


def _cycle(client: UpstoxClient, instrument_key: str, news_engine: NewsDecisionEngine,
           dxy, yields, cfg: EngineConfig, risk: RiskManager,
           exe: ExecutionEngine, cal_mgr: Optional[CalendarFeedManager] = None,
           *, cycle_no: int = 1, yahoo_macros: bool = False,
           state: Optional[dict] = None, tick_size: float = 1.0,
           journal: Optional[TradeJournal] = None,
           contract_info=None) -> None:
    now = datetime.now(tz=UTC)
    diag = CycleDiagnostics(cycle_no, now_utc=now, mode=cfg.mode)
    diag.begin()
    _publish_board(
        diag,
        symbol=str(cfg.gold_symbol),
        instrument_key=instrument_key,
        max_trades=int(getattr(cfg, "max_trades_per_day", 0) or 0),
        trades_today=int(risk.daily.trades_today),
    )

    # ---- 1) Calendar tick ----
    if cal_mgr is None:
        diag.step("CALENDAR.TICK", FAIL, "CalendarFeedManager not attached")
        cal_status: dict = {}
    else:
        try:
            cal_status = cal_mgr.tick(now)
            news_engine.set_events(cal_mgr.snapshot())
            n_rel = int(cal_status.get("new_releases") or 0)
            status = OK
            if n_rel:
                status = INFO
            detail = (
                f"events={cal_status.get('event_count')} "
                f"schedule_refresh={cal_status.get('schedule_refreshed')} "
                f"near_release={cal_status.get('near_release')} "
                f"near_poll={cal_status.get('near_polled')} "
                f"stream={'ON' if cal_status.get('stream') else 'OFF'} "
                f"new_releases={n_rel} "
                f"primary={cal_mgr.stats.get('primary')} "
                f"refreshes={cal_mgr.stats.get('schedule_refreshes')} "
                f"stream_updates={cal_mgr.stats.get('stream_updates')}"
            )
            diag.step("CALENDAR.TICK", status, detail)
            for rel in cal_status.get("releases") or []:
                diag.step(
                    "CALENDAR.RELEASE", INFO,
                    f"{rel.get('name')} actual={rel.get('actual')} id={rel.get('id')}",
                )
        except Exception as exc:  # noqa: BLE001
            diag.step("CALENDAR.TICK", FAIL, f"{exc}")
            cal_status = {"error": str(exc)}
            # Mandatory calendar health — block entries on tick failure
            if bool(getattr(cfg, "require_live_calendar", True)) and cfg.is_live:
                exe.emergency_halt = True
                diag.step("CALENDAR.GATE", FAIL, "calendar tick failed — entries halted")

    # Calendar freshness gate — always enforce calendar_max_age_hours when required.
    # Failed refreshes must not leave old events tradeable.
    cal_unhealthy = False
    if cal_mgr is not None and bool(getattr(cfg, "require_live_calendar", True)):
        max_age_h = float(getattr(cfg, "calendar_max_age_hours", 12) or 12)
        fresh = cal_mgr.calendar_freshness(now, max_age_hours=max_age_h)
        if cal_status.get("error"):
            fresh = dict(fresh)
            fresh["ok"] = False
            fresh["reasons"] = list(fresh.get("reasons") or []) + ["tick_error"]
        cal_unhealthy = not fresh.get("ok")
        if cal_unhealthy:
            diag.step(
                "CALENDAR.FRESHNESS", FAIL,
                f"UNHEALTHY age_h={fmt_num(fresh.get('age_hours'), 1)} "
                f"max={max_age_h} events={fresh.get('event_count')} "
                f"primary={fresh.get('primary')} "
                f"reasons={','.join(fresh.get('reasons') or [])} — block entries",
            )
            # Block entries in paper and live — stale calendar is never eligible
            exe.emergency_halt = True
        else:
            diag.step(
                "CALENDAR.FRESHNESS", OK,
                f"age_h={fmt_num(fresh.get('age_hours'), 1)} max={max_age_h} "
                f"events={fresh.get('event_count')} primary={fresh.get('primary')}",
            )

    # ---- 2) Macro refresh ----
    # Yahoo is research-oriented; live requires allow_yahoo_in_live.
    # Freshness for entries uses observation timestamps, not fetch-attempt time.
    live_yahoo_ok = bool(getattr(cfg, "allow_yahoo_in_live", False))
    use_yahoo = yahoo_macros and (not cfg.is_live or live_yahoo_ok)
    if cfg.is_live and yahoo_macros and not live_yahoo_ok:
        diag.step(
            "MACROS.SOURCE", FAIL,
            "yahoo_macros not approved for live — set allow_yahoo_in_live=true only with documented approval",
        )
        exe.emergency_halt = True
        use_yahoo = False
    try:
        from feeds import series_observation_age_minutes
        before_dxy_age = series_observation_age_minutes(dxy, now)
        before_yld_age = series_observation_age_minutes(yields, now)
        dxy, yields = refresh_macros(
            dxy, yields,
            allow_yahoo=use_yahoo,
            max_age_minutes=float(cfg.macro_refresh_minutes),
            prefer_csv=bool(cfg.dxy_csv or cfg.yield_csv),
        )
        if state is not None:
            state["dxy"], state["yields"] = dxy, yields
        dxy_age = series_observation_age_minutes(dxy, now)
        yld_age = series_observation_age_minutes(yields, now)
        obs_improved = (
            (dxy_age is not None and before_dxy_age is not None and dxy_age < before_dxy_age - 0.5)
            or (yld_age is not None and before_yld_age is not None and yld_age < before_yld_age - 0.5)
        )
        dxy_src = "csv" if cfg.dxy_csv else ("yahoo" if use_yahoo else ("yahoo/cache" if yahoo_macros else "none"))
        yld_src = "csv" if cfg.yield_csv else ("yahoo" if use_yahoo else ("yahoo/cache" if yahoo_macros else "none"))
        # Trade gate uses observation age (macro_trade_max_age); soft health uses macro_stale
        trade_max = float(getattr(cfg, "macro_trade_max_age_minutes", 60) or 60)
        st_d, det_d = series_health(
            "DXY", dxy, now, stale_limit_minutes=float(cfg.macro_stale_minutes), source=dxy_src,
        )
        st_y, det_y = series_health(
            "US10Y", yields, now, stale_limit_minutes=float(cfg.macro_stale_minutes), source=yld_src,
        )
        if dxy_age is None or yld_age is None or dxy_age > trade_max or yld_age > trade_max or dxy_age < -2 or yld_age < -2:
            diag.step(
                "MACROS.OBS_AGE", FAIL,
                f"observation age dxy={fmt_num(dxy_age, 0)}m yld={fmt_num(yld_age, 0)}m "
                f"trade_max={trade_max:.0f}m — entries blocked by strategy gate",
            )
        else:
            diag.step(
                "MACROS.OBS_AGE", OK,
                f"observation age dxy={fmt_num(dxy_age, 0)}m yld={fmt_num(yld_age, 0)}m "
                f"trade_max={trade_max:.0f}m obs_refreshed={obs_improved}",
            )
        diag.step("MACROS.DXY", st_d, det_d)
        diag.step("MACROS.US10Y", st_y, det_y)
    except Exception as exc:  # noqa: BLE001
        diag.step("MACROS", FAIL, f"refresh failed: {exc}")

    # ---- 3) Gold candles (Upstox) ----
    # Safety path: even if candles fail, reconcile open exposure (P0 #11).
    candle_source = "intraday"
    gold = pd.DataFrame()
    completed = pd.DataFrame()
    raw = pd.DataFrame()
    bar_age = None
    gold_ok = False
    candles_usable = False
    try:
        raw = client.intraday(instrument_key, "1minute")
        today = now.date() if hasattr(now, "date") else datetime.now(tz=UTC).date()
        if raw.empty:
            candle_source = "historical_fallback"
            diag.step(
                "GOLD.INTRADAY", WARN,
                "intraday empty — falling back to historical 1m (last 5 days)",
            )
            raw = client.historical(
                instrument_key, "1minute", today, today - timedelta(days=5),
            )
        else:
            last_1m = raw.index[-1]
            age_1m = age_minutes(last_1m, now)
            # Reject future-dated bars
            if age_1m is not None and age_1m < -2:
                diag.step("GOLD.INTRADAY", FAIL, f"future-dated bar age={age_1m:.1f}m")
                raw = pd.DataFrame()
            else:
                st = OK if age_1m is not None and age_1m <= 10 else PARTIAL
                diag.step(
                    "GOLD.INTRADAY", st,
                    f"bars={len(raw)} last={last_1m} age={fmt_num(age_1m, 1)}m source={candle_source}",
                )
                # Stitch prior sessions so 15m EMA50 has warmup beyond today's session
                try:
                    hist = client.historical(
                        instrument_key, "1minute",
                        today - timedelta(days=1),
                        today - timedelta(days=7),
                    )
                    if hist is not None and not hist.empty:
                        raw = pd.concat([hist, raw]).sort_index()
                        raw = raw[~raw.index.duplicated(keep="last")]
                        candle_source = "intraday+history"
                        diag.step(
                            "GOLD.HISTORY", OK,
                            f"stitched prior 1m bars={len(hist)} total_1m={len(raw)}",
                        )
                except Exception as hexc:  # noqa: BLE001
                    diag.step("GOLD.HISTORY", WARN, f"warmup stitch skipped: {hexc}")
        if not raw.empty:
            gold = resample_ohlcv(raw, cfg.timeframe_minutes)
        if gold.empty:
            diag.step("GOLD.CANDLES", FAIL, f"resample to {cfg.timeframe_minutes}m produced 0 bars")
        else:
            completed = candles_completed_as_of(gold, now, cfg.timeframe_minutes)
            need = cfg.gold_breakout_lookback_bars + cfg.atr_period
            if len(completed) < need:
                diag.step(
                    "GOLD.CANDLES", WARN,
                    f"warming up completed={len(completed)} need>={need} "
                    f"tf={cfg.timeframe_minutes}m — no new entries",
                )
            else:
                last_bar = completed.iloc[-1]
                bar_age = age_minutes(completed.index[-1], now)
                gold_ok = validate_candle_frame(completed)
                if bar_age is not None and bar_age < -2:
                    gold_ok = False
                    diag.step("GOLD.CANDLES", FAIL, "future-dated completed bar")
                else:
                    candles_usable = gold_ok
    except Exception as exc:  # noqa: BLE001
        diag.step("GOLD.CANDLES", FAIL, f"Upstox candle fetch failed: {exc}")

    if not candles_usable:
        # Still run quote + position safety / reconcile
        diag.step(
            "SAFETY", WARN,
            "candles unusable — skipping signal; running quote/reconcile for exposure",
        )
        try:
            bid, ask, ltp = client.bid_ask(instrument_key)
        except Exception:
            bid = ask = ltp = None
        if exe.has_open_position or cfg.is_live:
            rep = exe.reconcile_broker(instrument_key, discover_orphans=True)
            diag.step(
                "POSITION.RECONCILE",
                OK if rep.get("ok") else FAIL,
                f"candle-fail reconcile ok={rep.get('ok')} "
                f"local={rep.get('local_signed_qty')} broker={rep.get('broker_net_qty')}",
            )
            if not rep.get("ok"):
                exe.emergency_halt = True
        diag.step("SIGNAL", SKIP, "no candles for decision")
        diag.end()
        return

    last_bar = completed.iloc[-1]
    stale_lim = float(cfg.stale_data_minutes)
    # Index is bar-open (left label); freshness is age since the bar *closed*.
    tf_m = int(cfg.timeframe_minutes)
    completion_age = (bar_age - tf_m) if bar_age is not None else None
    bar_fresh = (
        completion_age is not None
        and completion_age <= stale_lim
        and completion_age >= -2
    )
    if not gold_ok:
        candle_st = FAIL
    elif completion_age is not None and completion_age > max(stale_lim * 12, 120):
        candle_st = WARN
    elif completion_age is not None and completion_age > stale_lim:
        candle_st = PARTIAL
    else:
        candle_st = OK
    need = cfg.gold_breakout_lookback_bars + cfg.atr_period
    diag.step(
        "GOLD.CANDLES", candle_st,
        f"completed={len(completed)} need>={need} last={completed.index[-1]} "
        f"O={fmt_num(last_bar['open'],2)} H={fmt_num(last_bar['high'],2)} "
        f"L={fmt_num(last_bar['low'],2)} C={fmt_num(last_bar['close'],2)} "
        f"closed_age={fmt_num(completion_age,1)}m stale_limit={stale_lim:.0f}m "
        f"valid={gold_ok} fresh={bar_fresh}",
    )

    # ---- 4) Bid / ask (book only — LTP never invents a zero-spread quote) ----
    quote_degraded = False
    quote_failed = False
    ltp: Optional[float] = None
    bid = ask = None
    mon_bid = mon_ask = float(completed.iloc[-1]["close"])
    spread = 0.0
    try:
        bid, ask, ltp = client.bid_ask(instrument_key)
        if bid is None or ask is None or ask < bid:
            quote_degraded = True
            diag.step(
                "GOLD.QUOTE", PARTIAL,
                f"book incomplete bid={fmt_num(bid,2)} ask={fmt_num(ask,2)} "
                f"ltp={fmt_num(ltp,2)} — NEW ENTRIES BLOCKED (no LTP fallback spread)",
            )
            # Monitor-only fallback prices (never used to approve new entries).
            mid = float(ltp) if ltp is not None else float(completed.iloc[-1]["close"])
            mon_bid = bid if bid is not None else mid
            mon_ask = ask if ask is not None else mid
            spread = 0.0
        else:
            mon_bid, mon_ask = bid, ask
            spread = ask - bid
            st = OK
            if spread > cfg.maximum_spread_points:
                st = WARN
            if spread <= 0:
                quote_degraded = True
                st = PARTIAL
            diag.step(
                "GOLD.QUOTE", st,
                f"bid={fmt_num(bid,2)} ask={fmt_num(ask,2)} ltp={fmt_num(ltp,2)} "
                f"spread={fmt_num(spread,2)} max={cfg.maximum_spread_points} book_ok=True",
            )
    except Exception as exc:  # noqa: BLE001
        quote_failed = True
        quote_degraded = True
        diag.step(
            "GOLD.QUOTE", FAIL,
            f"{exc} — NEW ENTRIES BLOCKED; continuing position/reconcile management",
        )
        if getattr(client, "auth_ok", True) is False:
            exe.emergency_halt = True
            diag.step("AUTH", FAIL, "HTTP 401 — entries disabled until credentials healthy")

    # Quote degraded (incomplete book) == quote failed for safety (P0 #13)
    quote_safety_event = quote_failed or quote_degraded
    _publish_board(
        diag,
        bid=bid, ask=ask, ltp=ltp, spread=spread if not quote_safety_event else None,
        quote_ok=not quote_safety_event and bid is not None and ask is not None,
        bar_o=float(last_bar["open"]), bar_h=float(last_bar["high"]),
        bar_l=float(last_bar["low"]), bar_c=float(last_bar["close"]),
        bar_age_m=completion_age, candles_ok=bool(candles_usable and gold_ok),
        history_ok=("intraday+history" in candle_source) or candle_source == "historical_fallback",
        candles_5m=len(completed),
    )

    # ---- 5a) Mandatory session flatten (before new entries; after quote known) ----
    if exe.has_open_position and _past_flatten_cutoff(now, cfg):
        diag.step(
            "FLATTEN", WARN,
            f"session flatten cutoff — closing before market_close={cfg.market_close}",
        )
        try:
            exe.monitor(
                now, mon_bid, mon_ask,
                data_valid=True, allow_software_exits=False,
            )
            # Force close via daily-style path
            if exe.has_open_position:
                closed = exe._close(now, mon_bid, mon_ask, "session_flatten")
                if journal is not None:
                    journal.log_position("session_flatten", exe.position)
                    journal.log_risk("session_flatten", "mandatory_eod")
                    journal.log_decision({
                        "action": "EXIT", "reason": "session_flatten",
                        "side": "", "signal_id": "", "system_state": "EXIT_PENDING",
                        "timestamp_utc": now.isoformat(),
                    })
                if closed is None and exe.has_open_position:
                    exe.emergency_halt = True
                    diag.step("FLATTEN", FAIL, "flatten incomplete — halt + reconcile")
                    rep = exe.reconcile_broker(instrument_key, discover_orphans=True)
                    diag.step(
                        "FLATTEN.RECONCILE",
                        OK if rep.get("ok") and int(rep.get("broker_net_qty") or 0) == 0 else FAIL,
                        f"broker_net={rep.get('broker_net_qty')} ok={rep.get('ok')}",
                    )
                else:
                    diag.step("FLATTEN", OK, "position closed for session — EXIT")
        except Exception as exc:  # noqa: BLE001
            exe.emergency_halt = True
            diag.step("FLATTEN", FAIL, f"{exc}")

    # EOD report once past flatten cutoff when flat
    if (
        state is not None
        and not state.get("eod_report_written")
        and _past_flatten_cutoff(now, cfg)
        and not exe.has_open_position
        and journal is not None
    ):
        try:
            write_eod_report(
                journal_path=journal.path,
                trading_day=risk.trading_day_of(now),
                equity=risk.equity,
                realized_pnl=risk.daily.realized_pnl,
                trades_today=risk.daily.trades_today,
                blocked=risk.daily.blocked,
                extra={"instrument_key": instrument_key, "trigger": "flatten_cutoff"},
            )
            state["eod_report_written"] = True
            diag.step("EOD.REPORT", OK, "daily report archived under reports/")
        except Exception as exc:  # noqa: BLE001
            diag.step("EOD.REPORT", WARN, str(exc))

    # ---- 5) Open position monitor (ALWAYS runs — even on quote outage) ----
    if exe.has_open_position:
        try:
            last = completed.iloc[-1]
            # P0 #12: bar-based software exits only with a verified fresh bar.
            # On stale/degraded quotes: rely on broker protection + reconcile.
            if quote_safety_event:
                exe.emergency_halt = True
            use_bar_exits = bar_fresh and not quote_safety_event
            exe.monitor(
                now, mon_bid, mon_ask,
                bar_high=float(last["high"]) if use_bar_exits else None,
                bar_low=float(last["low"]) if use_bar_exits else None,
                bar_open=float(last["open"]) if use_bar_exits else None,
                data_valid=True,
                allow_software_exits=use_bar_exits,
            )
            if quote_safety_event and exe.has_open_position:
                rep = exe.reconcile_broker(instrument_key, discover_orphans=True)
                diag.step(
                    "POSITION.RECONCILE",
                    WARN if rep.get("ok") else FAIL,
                    f"quote safety reconcile ok={rep.get('ok')} "
                    f"local={rep.get('local_signed_qty')} broker={rep.get('broker_net_qty')} "
                    f"degraded={quote_degraded} failed={quote_failed}",
                )
                if not rep.get("ok"):
                    exe.emergency_halt = True
            elif not bar_fresh and exe.has_open_position:
                rep = exe.reconcile_broker(instrument_key, discover_orphans=True)
                diag.step(
                    "POSITION.RECONCILE",
                    WARN if rep.get("ok") else FAIL,
                    f"stale-bar reconcile (no software exits) ok={rep.get('ok')}",
                )
                exe.emergency_halt = True
            pos = exe.position
            diag.step(
                "POSITION", INFO,
                f"open {getattr(pos, 'side', '?') if pos else 'FLAT'} "
                f"qty={getattr(pos, 'quantity', 0)} "
                f"entry={fmt_num(getattr(pos, 'entry_price', None), 2)} "
                f"sl={fmt_num(getattr(pos, 'stop_price', None), 2)} "
                f"tp={fmt_num(getattr(pos, 'target_price', None), 2)} "
                f"broker_sl={getattr(pos, 'stop_order_id', '-') if pos else '-'} "
                f"bar_exits={use_bar_exits}",
            )
        except Exception as exc:  # noqa: BLE001
            diag.step("POSITION", FAIL, f"monitor failed: {exc}")
            exe.emergency_halt = True
    else:
        diag.step("POSITION", SKIP, "no open position")
        if quote_safety_event:
            # Still probe broker in case of untracked exposure
            rep = exe.reconcile_broker(instrument_key)
            if not rep.get("skipped") and (
                rep.get("broker_net_qty") not in (0, None) or not rep.get("ok")
            ):
                diag.step(
                    "POSITION.ORPHAN", FAIL,
                    f"no local position but broker={rep.get('broker_net_qty')} "
                    f"reason={rep.get('reason')}",
                )
                exe.emergency_halt = True

    # ---- 6) Risk gates ----
    daily_hit = risk.daily_loss_reached(now)
    trades_hit = risk.trades_limit_reached()
    consec_hit = risk.consecutive_loss_limit_reached()
    if daily_hit:
        diag.step("RISK.DAILY", FAIL, "daily loss limit reached — no new entries")
    elif trades_hit:
        diag.step(
            "RISK.DAILY", FAIL,
            f"max_trades_per_day={getattr(cfg, 'max_trades_per_day', 0)} reached "
            f"(trades_today={risk.daily.trades_today})",
        )
    elif consec_hit:
        diag.step(
            "RISK.DAILY", FAIL,
            f"max_consecutive_losses={getattr(cfg, 'max_consecutive_losses', 0)} reached",
        )
    else:
        diag.step(
            "RISK.DAILY", OK,
            f"daily_loss_ok equity≈{cfg.initial_equity:,.0f} "
            f"limit={cfg.maximum_daily_loss_pct}% trades={risk.daily.trades_today}/"
            f"{getattr(cfg, 'max_trades_per_day', 0)}",
        )

    # HOLD: open position — manage only, no new entries
    if exe.has_open_position and not _past_flatten_cutoff(now, cfg):
        side_h = getattr(exe.position, "side", "?") if exe.position else "?"
        pos = exe.position
        diag.step(
            "DECISION", INFO,
            f"action=HOLD system_state={'LONG_OPEN' if side_h == 'BUY' else 'SHORT_OPEN'} "
            f"side={side_h} — managing open position",
        )
        # Journal HOLD periodically (not every 5s poll)
        if journal is not None and (cycle_no % 12 == 1):
            journal.log_decision({
                "action": "HOLD",
                "reason": "open_position",
                "side": side_h,
                "signal_id": str(exe.last_breakout_key or ""),
                "system_state": "LONG_OPEN" if side_h == "BUY" else "SHORT_OPEN",
                "timestamp_utc": now.isoformat(),
            })
        diag.step("RISK.PLAN", SKIP, "HOLD — no new entry")
        diag.step("EXECUTION", SKIP, "position open")
        _publish_board(
            diag,
            has_position=True, position_side=str(side_h),
            position_entry=getattr(pos, "entry_price", None),
            position_stop=getattr(pos, "stop_price", None),
            position_target=getattr(pos, "target_price", None),
            decision="HOLD",
            system_state="LONG_OPEN" if side_h == "BUY" else "SHORT_OPEN",
            decision_reason="managing open position — no new entries",
            trades_today=int(risk.daily.trades_today),
        )
        diag.end()
        return

    # ---- 7) Decision path ----
    strategy_mode = str(getattr(cfg, "strategy_mode", "trend_breakout") or "trend_breakout").lower()
    approved_side = ""
    breakout_key = ""
    news_related = False

    if strategy_mode == "trend_breakout":
        # Market snapshot (5m + 15m completed candles; MCX only for trading)
        from market_snapshot import ContractInfo
        if contract_info is not None:
            try:
                cinfo = contract_from_mcx_context(contract_info, cfg)
            except Exception:  # noqa: BLE001
                cinfo = ContractInfo(
                    symbol=cfg.gold_symbol, instrument_key=instrument_key,
                    lot_size=int(cfg.lot_size), tick_size=float(tick_size),
                    value_per_price_unit=float(cfg.value_per_price_unit),
                )
        else:
            cinfo = ContractInfo(
                symbol=cfg.gold_symbol, instrument_key=instrument_key,
                lot_size=int(cfg.lot_size), tick_size=float(tick_size),
                value_per_price_unit=float(cfg.value_per_price_unit),
            )
        mon_snap = PRODUCER_HUB.latest_monitor_snap() if PRODUCER_HUB.started else None
        snap = build_engine_snapshot(
            cfg=cfg, contract=cinfo, raw_1m=raw,
            bid=bid, ask=ask, ltp=ltp, now_utc=now, quote_source="upstox",
            monitor_snap=mon_snap,
        )
        # Contract mismatch: monitor MCX key ≠ engine key → block entries
        pst = PRODUCER_HUB.status() if PRODUCER_HUB.started else {}
        if pst.get("contract_mismatch"):
            snap.mcx_data_valid = False
            snap.reasons.append(
                f"monitor_contract_mismatch engine={instrument_key} "
                f"monitor={pst.get('monitor_mcx_key')}"
            )
        try:
            persist_snapshot(snap)
        except Exception as exc:  # noqa: BLE001
            diag.step("SNAPSHOT", WARN, f"persist failed: {exc}")
        diag.step(
            "SNAPSHOT",
            OK if snap.mcx_data_valid else FAIL,
            f"mcx_valid={snap.mcx_data_valid} q={snap.quote.valid} "
            f"5m={snap.candles_5m.valid}({snap.candles_5m.bars}) "
            f"15m={snap.candles_15m.valid}({snap.candles_15m.bars}) "
            f"ctx={len(snap.context)} reasons={','.join(snap.reasons)}",
        )
        # Producer health: embedded monitor + news threads
        from market_snapshot import SNAPSHOT_PATH
        from producers import MONITOR_JSON
        mon_age = None
        if MONITOR_JSON.exists():
            mon_age = max(0.0, (now - datetime.fromtimestamp(
                MONITOR_JSON.stat().st_mtime, tz=UTC,
            )).total_seconds())
        mon_ok = bool(pst.get("monitor_last_ok")) and not pst.get("monitor_error")
        diag.step(
            "PRODUCER.MONITOR",
            OK if mon_ok and (mon_age is None or mon_age < 180) else (
                WARN if PRODUCER_HUB.started else PARTIAL
            ),
            f"cycles={pst.get('monitor_cycles', 0)} age_s={fmt_num(mon_age, 0) if mon_age is not None else 'n/a'} "
            f"err={pst.get('monitor_error') or '-'} mismatch={pst.get('contract_mismatch')}",
        )
        snap_age_s: Optional[float] = None
        if SNAPSHOT_PATH.exists():
            snap_age_s = max(0.0, (now - datetime.fromtimestamp(
                SNAPSHOT_PATH.stat().st_mtime, tz=UTC,
            )).total_seconds())
        diag.step(
            "PRODUCER.ENGINE_SNAP",
            OK if snap.collector_healthy and (snap_age_s is None or snap_age_s < 300)
            else (WARN if snap_age_s is not None else FAIL),
            f"file={SNAPSHOT_PATH.name} age_s={fmt_num(snap_age_s, 0) if snap_age_s is not None else 'n/a'} "
            f"healthy={snap.collector_healthy}",
        )

        news_db = str(getattr(cfg, "news_db_path", "") or "") or str(ENGINE_DIR / "gold_signals.db")
        news_a = assess_news(
            news_db, now_utc=now,
            freshness_minutes=float(getattr(cfg, "news_freshness_minutes", 45) or 45),
        )
        news_path = Path(news_db)
        news_age_s: Optional[float] = None
        if news_path.exists():
            news_age_s = max(0.0, (now - datetime.fromtimestamp(
                news_path.stat().st_mtime, tz=UTC,
            )).total_seconds())
        diag.step(
            "PRODUCER.NEWS_DB",
            OK if news_a.fresh else (WARN if news_path.exists() else PARTIAL),
            f"path={news_path.name} age_s={fmt_num(news_age_s, 0) if news_age_s is not None else 'missing'} "
            f"fresh={news_a.fresh} items={news_a.item_count}",
        )
        diag.step(
            "NEWS.ASSESS",
            OK if news_a.fresh and news_a.bias in ("bullish", "bearish") else (
                WARN if news_a.fresh else PARTIAL
            ),
            f"bias={news_a.bias} score={news_a.score:.2f} fresh={news_a.fresh} "
            f"conflict={news_a.conflicting} n={news_a.item_count} "
            f"reasons={','.join(news_a.reasons)}",
        )
        if journal is not None:
            journal.log_news(news_a)

        msig = evaluate_market_signal(
            snap.frame_5m if snap.frame_5m is not None else completed,
            snap.frame_15m if snap.frame_15m is not None else pd.DataFrame(),
            lookback=int(cfg.gold_breakout_lookback_bars),
            atr_period=int(cfg.atr_period),
            ema_fast=int(getattr(cfg, "trend_ema_fast", 20)),
            ema_slow=int(getattr(cfg, "trend_ema_slow", 50)),
            data_valid=snap.mcx_data_valid,
        )
        diag.step(
            "SIGNAL",
            OK if msig.valid else INFO,
            f"dir={msig.direction} trend={msig.trend} brk={msig.breakout} "
            f"regime={msig.regime} atr={fmt_num(msig.atr, 2)} id={msig.signal_id or '-'} "
            f"reasons={','.join(msig.reasons)}",
        )
        if journal is not None:
            journal.log_signal(msig)

        # Calendar event blackout (reuse news engine window)
        ev_ctx = news_engine.evaluate_news(now)
        event_block = bool(ev_ctx.inside_block and str(cfg.news_mode).lower() == "filtered")
        diag.step(
            "EVENT",
            WARN if event_block else OK,
            f"block={event_block} nearest={ev_ctx.nearest_event_name or '-'} "
            f"reason={ev_ctx.reason}",
        )

        session_ok = (not bool(getattr(cfg, "enforce_session_hours", True))) or _in_entry_window(now, cfg)
        kill = KILL_SWITCH_FILE.exists()
        gate = evaluate_entry_gates(
            now_utc=now,
            signal=msig,
            news=news_a,
            session_ok=session_ok and not _past_flatten_cutoff(now, cfg),
            session_detail=f"open={cfg.market_open} cut={cfg.entry_cutoff} close={cfg.market_close}",
            mcx_data_valid=snap.mcx_data_valid and not quote_degraded and not quote_failed,
            data_detail=",".join(snap.reasons),
            event_block_active=event_block,
            event_detail=ev_ctx.reason,
            broker_ok=not exe.state_corrupt and getattr(client, "auth_ok", True) is not False,
            broker_detail="ok" if not exe.state_corrupt else "state_corrupt",
            risk_ok=(
                not daily_hit and not trades_hit and not consec_hit
                and not exe.has_open_position
            ),
            risk_detail=(
                "daily_loss" if daily_hit else
                "max_trades" if trades_hit else
                "consecutive_losses" if consec_hit else
                ("has_position" if exe.has_open_position else "ok")
            ),
            emergency_halt=bool(exe.emergency_halt),
            kill_switch=kill,
            last_signal_id=str(exe.last_breakout_key or ""),
            require_news_confirmation=bool(getattr(cfg, "require_news_confirmation", True)),
        )
        for g in gate.gates:
            diag.step(
                f"GATE.{g.name}",
                OK if g.passed else FAIL,
                g.detail,
            )
        diag.step(
            "DECISION",
            OK if gate.action in ("BUY", "SELL") else INFO,
            f"action={gate.action} state={gate.system_state} reason={gate.reason} "
            f"side={gate.side} sid={gate.signal_id} "
            f"line={gate.meta.get('decision_line', '')}",
        )
        if journal is not None:
            journal.log_decision(gate)

        # DXY / yield latest for context row
        dxy_last = float(dxy.iloc[-1]) if dxy is not None and len(dxy) else None
        yld_last = float(yields.iloc[-1]) if yields is not None and len(yields) else None
        from console_board import gate_rows_from_decision
        _publish_board(
            diag,
            candles_15m=int(snap.candles_15m.bars or 0),
            candles_5m=int(snap.candles_5m.bars or 0),
            trend=msig.trend, breakout=msig.breakout, direction=msig.direction,
            regime=msig.regime, atr=msig.atr, ema_fast=msig.ema_fast, ema_slow=msig.ema_slow,
            news_bias=news_a.bias, news_score=float(news_a.score),
            news_fresh=bool(news_a.fresh), news_items=int(news_a.item_count),
            news_detail=",".join(news_a.reasons[:3]),
            news_db_ok=bool(news_a.fresh or news_a.item_count),
            monitor_ok=bool(pst.get("monitor_last_ok")) and not pst.get("monitor_error"),
            calendar_ok=not bool(cal_unhealthy),
            macros_ok=dxy_last is not None and yld_last is not None,
            dxy=dxy_last, us10y=yld_last,
            gate_rows=gate_rows_from_decision(gate),
            decision=str(gate.action), system_state=str(gate.system_state),
            decision_reason=str(gate.reason),
            trades_today=int(risk.daily.trades_today),
        )

        if gate.action not in ("BUY", "SELL"):
            diag.step("RISK.PLAN", SKIP, f"no entry — {gate.reason}")
            diag.step("EXECUTION", SKIP, "no order submitted")
            diag.end()
            return
        approved_side = gate.action
        breakout_key = gate.signal_id
        news_related = True
        # Use 5m completed frame for ATR plan
        plan_candles = snap.frame_5m if snap.frame_5m is not None and not snap.frame_5m.empty else completed
    else:
        # ---- legacy_macro path (retained, disabled by default) ----
        try:
            decision = news_engine.decide(
                now_utc=now, dxy=dxy, yields=yields, gold_candles=completed,
                daily_loss_limit_reached=daily_hit,
                has_open_position=exe.has_open_position,
                last_breakout_key=exe.last_breakout_key,
                gold_data_valid=gold_ok,
            )
        except Exception as exc:  # noqa: BLE001
            diag.step("DECISION", FAIL, f"news_engine.decide crashed: {exc}")
            diag.end()
            return
        news = decision.news
        diag.step(
            "SIGNAL",
            OK if decision.decision in ("BUY", "SELL") else INFO,
            f"legacy decision={decision.decision} reason={decision.reason} bias={decision.bias}",
        )
        if decision.decision == "NO_TRADE":
            diag.step("EXECUTION", SKIP, "legacy NO_TRADE")
            diag.end()
            return
        if quote_degraded or quote_failed or bid is None or ask is None:
            diag.step("EXECUTION", SKIP, "quote degraded")
            diag.end()
            return
        if bool(getattr(cfg, "enforce_session_hours", True)) and not _in_entry_window(now, cfg):
            diag.step("EXECUTION", SKIP, "session")
            diag.end()
            return
        if exe.emergency_halt or exe.state_corrupt or KILL_SWITCH_FILE.exists():
            exe.emergency_halt = True
            diag.step("EXECUTION", FAIL, "halt/corrupt/kill")
            diag.end()
            return
        approved_side = decision.decision
        breakout_key = str((decision.signal.meta if decision.signal else {}) or {}).get("breakout_key") or ""
        news_related = bool(news.inside_block) or bool(news.surprise_active)
        plan_candles = completed

    if exe.emergency_halt or exe.state_corrupt:
        diag.step("EXECUTION", FAIL, "halt or corrupt — no entry")
        diag.end()
        return
    if KILL_SWITCH_FILE.exists():
        exe.emergency_halt = True
        diag.step("EXECUTION", FAIL, "KILL_SWITCH active")
        diag.end()
        return
    if quote_degraded or quote_failed or bid is None or ask is None or ask < bid:
        diag.step("EXECUTION", SKIP, "incomplete quote")
        diag.end()
        return

    # ---- 8) Risk plan ----
    try:
        plan = risk.build_trade_plan(
            approved_side, bid, ask, plan_candles, spread_points=spread,
        )
        if plan.rejected:
            diag.step(
                "RISK.PLAN", FAIL,
                f"REJECTED — {plan.reject_reason} side={approved_side} "
                f"spread={fmt_num(spread, 2)}",
            )
            if journal is not None:
                journal.log_risk("plan_rejected", plan.reject_reason)
            diag.step("EXECUTION", SKIP, "plan rejected — no order")
            _publish_board(
                diag, plan_side=approved_side, plan_entry=getattr(plan, "entry_price", None),
                plan_stop=getattr(plan, "stop_price", None),
                plan_target=getattr(plan, "target_price", None),
                plan_rejected=str(plan.reject_reason),
                decision="WAIT", decision_reason=f"plan rejected: {plan.reject_reason}",
            )
            diag.end()
            return
        diag.step(
            "RISK.PLAN", OK,
            f"side={plan.side} lots={plan.lots} qty={plan.quantity} "
            f"entry≈{fmt_num(plan.entry_price, 2)} "
            f"SL={fmt_num(plan.stop_price, 2)} TP={fmt_num(plan.target_price, 2)} "
            f"risk_budget={fmt_num(plan.risk_budget, 2)}",
        )
        _publish_board(
            diag, plan_side=plan.side, plan_entry=plan.entry_price,
            plan_stop=plan.stop_price, plan_target=plan.target_price,
        )
    except Exception as exc:  # noqa: BLE001
        diag.step("RISK.PLAN", FAIL, f"{exc}")
        diag.end()
        return

    # ---- 9) Execution ----
    try:
        trade = exe.open_trade(
            plan, now, instrument_key=instrument_key,
            breakout_key=breakout_key,
            news_related=news_related,
            tick_size=tick_size,
        )
        if trade is None:
            diag.step("EXECUTION", WARN, "open_trade returned None (rejected by execution layer)")
            _publish_board(diag, decision_reason="order not accepted by execution layer")
        else:
            diag.step(
                "EXECUTION", OK,
                f"{'LIVE' if cfg.is_live else 'PAPER'} {plan.side} qty={plan.quantity} "
                f"@ {fmt_num(getattr(trade, 'entry_price', plan.entry_price), 2)} "
                f"order_id={getattr(trade, 'order_id', '-')}",
            )
            _publish_board(
                diag, decision=plan.side, system_state="ORDER_PENDING",
                decision_reason=f"opened {plan.side} @ {getattr(trade, 'entry_price', plan.entry_price)}",
                has_position=True, position_side=plan.side,
                position_entry=getattr(trade, "entry_price", plan.entry_price),
                position_stop=getattr(trade, "stop_price", plan.stop_price),
                position_target=getattr(trade, "target_price", plan.target_price),
                trades_today=int(risk.daily.trades_today),
            )
            if journal is not None:
                journal.log_order(
                    order_id=str(getattr(trade, "order_id", "") or ""),
                    side=plan.side, quantity=int(plan.quantity),
                    status="opened",
                    payload={"signal_id": breakout_key},
                )
                journal.log_position("open", trade)
                journal.log_fill(
                    order_id=str(getattr(trade, "order_id", "") or ""),
                    side=plan.side, quantity=int(getattr(trade, "quantity", plan.quantity)),
                    price=float(getattr(trade, "entry_price", plan.entry_price)),
                    tag="entry",
                )
    except Exception as exc:  # noqa: BLE001
        diag.step("EXECUTION", FAIL, f"order failed: {exc}")
        if journal is not None:
            journal.log_risk("order_failed", str(exc))
        _publish_board(diag, decision_reason=f"order failed: {exc}")

    diag.end()


def run_upstox_backtest(cfg: EngineConfig, start: date, end: date,
                        yahoo_macros: bool) -> int:
    from upstox_client import list_mcx_gold_futures, stitch_front_month_history

    resolve_credentials(cfg)
    client = UpstoxClient(cfg)
    master = load_instrument_master(cfg.instrument_cache)
    if cfg.instrument_key_override:
        fut = resolve_mcx_gold_future(
            master, cfg.gold_symbol, as_of=start,
            override_key=cfg.instrument_key_override,
        )
        chain = [fut]
        log.info("Backtest using instrument override %s (no roll)", fut.get("instrument_key"))
        raw = client.historical_range(str(fut["instrument_key"]), "1minute", end, start)
    else:
        chain = list_mcx_gold_futures(master, cfg.gold_symbol, as_of=start, until=end)
        if not chain:
            log.error("No MCX %s futures in instrument master for %s..%s",
                      cfg.gold_symbol, start, end)
            return 2
        fut = chain[0]
        log.info(
            "Fetching front-month roll (%s contracts) %s .. %s",
            len(chain), start, end,
        )
        for row in chain:
            log.info("  chain %s expiry_ms=%s", row.get("trading_symbol"), row.get("expiry"))
        raw = stitch_front_month_history(client, chain, "1minute", start, end)
    ctx = mcx_context_from_instrument(fut, cfg.gold_symbol)
    cfg.value_per_price_unit = ctx.value_per_price_unit
    cfg.lot_size = ctx.lot_size
    if raw.empty:
        log.error("No historical gold data returned from Upstox")
        return 2
    gold = resample_ohlcv(raw, cfg.timeframe_minutes)
    dxy, yields = maybe_fetch_macros(cfg.dxy_csv, cfg.yield_csv, allow_yahoo=yahoo_macros)
    if dxy.empty or yields.empty:
        log.error("Macro series required for backtest (DXY + yields).")
        return 2
    events = load_calendar(cfg.calendar_path)
    result = run_backtest(gold, dxy, yields, events, cfg=cfg, bid_ask_spread=2.0)
    print(result.performance)
    ensure_engine_dirs()
    out = OUTPUT_DIR / f"gold_trades_{start}_{end}.csv"
    write_trades_csv(result.trades, out)
    write_rejects_csv(result.rejects, OUTPUT_DIR / f"gold_rejects_{start}_{end}.csv")
    log.info("Wrote %s (%d trades)", out, len(result.trades))
    return 0



# -------------------------------------------------------------------------------- diagnostics + audit (inlined)

UTC = timezone.utc
log = logging.getLogger("gold_news.diag")

OK = "OK"
PARTIAL = "PARTIAL"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"
INFO = "INFO"

_STATUS_ORDER = {FAIL: 0, WARN: 1, PARTIAL: 2, SKIP: 3, INFO: 4, OK: 5}


@dataclass
class StepResult:
    step: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


class CycleDiagnostics:
    """Collects cycle health; compact mode prints a single console board."""

    # Set False via --verbose for the old line-by-line audit dump
    compact_default: bool = True

    def __init__(self, cycle_no: int, now_utc: Optional[datetime] = None,
                 mode: str = "paper", *, compact: Optional[bool] = None):
        self.cycle_no = cycle_no
        self.now_utc = now_utc or datetime.now(tz=UTC)
        self.mode = mode
        self.steps: list[StepResult] = []
        self._step_no = 0
        self.compact = self.compact_default if compact is None else bool(compact)
        self.board: Any = None  # console_board.BoardView

    def step(self, name: str, status: str, detail: str, **data: Any) -> StepResult:
        self._step_no += 1
        result = StepResult(
            step=f"{self._step_no:02d} {name}",
            status=status.upper(),
            detail=detail,
            data=data,
        )
        self.steps.append(result)
        if self.compact and self.cycle_no > 0:
            return result
        line = f"  [{result.step:<22}] {result.status:<8} {result.detail}"
        if result.status == FAIL:
            log.error(line)
        elif result.status in (WARN, PARTIAL):
            log.warning(line)
        else:
            log.info(line)
        return result

    def begin(self) -> None:
        if self.compact and self.cycle_no > 0:
            return
        stamp = self.now_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        bar = "=" * 78
        log.info(bar)
        log.info(
            "CYCLE #%d  %s  mode=%s",
            self.cycle_no, stamp, self.mode,
        )
        log.info(bar)

    def end(self) -> dict[str, int]:
        counts = {OK: 0, PARTIAL: 0, WARN: 0, FAIL: 0, SKIP: 0, INFO: 0}
        for s in self.steps:
            counts[s.status] = counts.get(s.status, 0) + 1
        worst = OK
        for s in self.steps:
            if _STATUS_ORDER.get(s.status, 9) < _STATUS_ORDER.get(worst, 9):
                worst = s.status

        if self.compact and self.cycle_no > 0 and self.board is not None:
            try:
                from console_board import print_board
                print_board(self.board, clear=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("console board render failed: %s", exc)
            return counts

        bar = "-" * 78
        log.info(bar)
        log.info(
            "CYCLE #%d HEALTH  worst=%s  OK=%d PARTIAL=%d WARN=%d FAIL=%d SKIP=%d  steps=%d",
            self.cycle_no, worst,
            counts.get(OK, 0), counts.get(PARTIAL, 0), counts.get(WARN, 0),
            counts.get(FAIL, 0), counts.get(SKIP, 0), len(self.steps),
        )
        if counts.get(FAIL, 0):
            failed = [s.step for s in self.steps if s.status == FAIL]
            log.error("  FAILING: %s", ", ".join(failed))
        if counts.get(WARN, 0) or counts.get(PARTIAL, 0):
            soft = [
                f"{s.step}={s.status}"
                for s in self.steps if s.status in (WARN, PARTIAL)
            ]
            log.warning("  DEGRADED: %s", ", ".join(soft))
        log.info(bar)
        return counts


def age_minutes(ts: Any, now_utc: datetime) -> Optional[float]:
    if ts is None:
        return None
    try:
        t = pd_timestamp_to_utc(ts)
    except Exception:  # noqa: BLE001
        return None
    return (now_utc.astimezone(UTC) - t).total_seconds() / 60.0


def pd_timestamp_to_utc(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    # pandas Timestamp / numpy
    import pandas as pd
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def fmt_num(value: Any, digits: int = 4, empty: str = "-") -> str:
    if value is None:
        return empty
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return empty


def series_health(
    name: str,
    series,
    now_utc: datetime,
    *,
    stale_limit_minutes: float,
    source: str = "",
) -> tuple[str, str]:
    """Return (status, detail) for a macro/price series."""
    if series is None or getattr(series, "empty", True):
        return FAIL, f"{name} EMPTY — no data loaded (source={source or 'none'})"
    last_ts = series.index[-1]
    last_val = series.iloc[-1]
    age = age_minutes(last_ts, now_utc)
    n = len(series)
    src = f" source={source}" if source else ""
    if age is None:
        return PARTIAL, f"{name} pts={n} last_val={fmt_num(last_val)} age=unknown{src}"
    if age > stale_limit_minutes:
        return FAIL, (
            f"{name} pts={n} last={last_ts} val={fmt_num(last_val)} "
            f"age={age:.0f}m EXCEEDS stale_limit={stale_limit_minutes:.0f}m{src}"
        )
    # Soft warn when older than 1 day for intraday macros
    if age > 24 * 60:
        return WARN, (
            f"{name} pts={n} last={last_ts} val={fmt_num(last_val)} "
            f"age={age:.0f}m (weekend/US-close gap ok if <{stale_limit_minutes:.0f}m){src}"
        )
    if age > 60:
        return PARTIAL, (
            f"{name} pts={n} last={last_ts} val={fmt_num(last_val)} "
            f"age={age:.0f}m — not fresh intraday{src}"
        )
    return OK, (
        f"{name} pts={n} last={last_ts} val={fmt_num(last_val)} "
        f"age={age:.1f}m{src}"
    )


def credential_health(cfg, env_path: str = "") -> tuple[str, str]:
    from upstox_client import load_env_file, token_expiry

    file_vals = load_env_file(str(getattr(cfg, "env_file", env_path) or env_path))
    token = (getattr(cfg, "access_token", "") or "").strip()
    api_key = bool(getattr(cfg, "api_key", "") or file_vals.get("UPSTOX_API_TOKEN")
                   or file_vals.get("UPSTOX_API_KEY"))
    te = bool(file_vals.get("TRADING_ECONOMICS_KEY") or file_vals.get("TE_API_KEY")
              or file_vals.get("TRADING_ECONOMICS_CLIENT") or file_vals.get("TE_CLIENT"))
    fmp = bool(file_vals.get("FMP_API_KEY") or file_vals.get("FINANCIAL_MODELING_PREP_API_KEY"))

    parts = []
    status = OK
    if not token:
        return FAIL, "UPSTOX_ACCESS_TOKEN missing — run --login"
    exp = token_expiry(token)
    if exp:
        now_ist = datetime.now(tz=exp.tzinfo)
        hrs = (exp - now_ist).total_seconds() / 3600.0
        if hrs <= 0:
            return FAIL, f"Upstox token EXPIRED at {exp.isoformat()}"
        if hrs < 2:
            status = WARN
            parts.append(f"token expires {exp.isoformat()} ({hrs:.1f}h left — renew soon)")
        else:
            parts.append(f"token ok until {exp.isoformat()} ({hrs:.1f}h left)")
    else:
        status = PARTIAL
        parts.append("token present but expiry unreadable")
    if not api_key:
        status = WARN if status == OK else status
        parts.append("UPSTOX_API_TOKEN missing (login/orders may fail)")
    else:
        parts.append("api_key=set")
    parts.append("TE_key=" + ("set" if te else "MISSING"))
    parts.append("FMP_key=" + ("set" if fmp else "MISSING"))
    if not te and not fmp:
        status = PARTIAL if status == OK else status
        parts.append("calendar will use local CSV only")
    return status, " | ".join(parts)


from feeds import (
    DEFAULT_DXY_CACHE,
    DEFAULT_YIELD_CACHE,
    CalendarFeedManager,
    TradingEconomicsClient,
    FMPCalendarClient,
    _env_creds,
    fetch_dxy_yahoo,
    fetch_us10y_yahoo,
    load_macros,
    resample_ohlcv,
)
from strategy import (
    NewsDecisionEngine,
    classify_surprise,
    combine_bias,
    event_polarity,
    surprise_to_gold_bias,
)
from trading import ExecutionEngine, RiskManager

UTC = timezone.utc
log = logging.getLogger("gold_news.audit")

# Soft statuses that do not fail the audit exit code
_SOFT = {OK, WARN, PARTIAL, SKIP, INFO}


def run_audit(cfg: EngineConfig) -> int:
    """
    Validate each component. Exit 0 if no FAIL steps; exit 2 if any FAIL.

    SESSION_CLOSED / weekend gaps are WARN, not FAIL.
    """
    diag = CycleDiagnostics(0, mode=f"audit/{cfg.mode}")
    diag.begin()
    log.info("  AUDIT validation — probing APIs + offline logic (safe on weekends)")
    log.info("  FAIL = broken wiring | WARN = session closed / optional key missing")

    # ------------------------------------------------------------------ 1 creds
    try:
        resolve_credentials(cfg)
        st, detail = credential_health(cfg, str(ENV_FILE))
        # Missing TE/FMP is expected optional → keep PARTIAL, not FAIL
        diag.step("A01.CREDENTIALS", st, detail)
    except SystemExit as exc:
        diag.step("A01.CREDENTIALS", FAIL, str(exc))
        return _finish(diag)

    # ----------------------------------------------------------- 2 env key flags
    from upstox_client import load_env_file
    env = load_env_file(str(ENV_FILE))
    te_ok = bool(
        env.get("TRADING_ECONOMICS_KEY") or env.get("TE_API_KEY")
        or env.get("TRADING_ECONOMICS_CLIENT") or env.get("TE_CLIENT")
    )
    fmp_ok = bool(env.get("FMP_API_KEY") or env.get("FINANCIAL_MODELING_PREP_API_KEY"))
    diag.step(
        "A02.API_KEYS",
        OK if (te_ok or fmp_ok) else PARTIAL,
        f"Upstox_token=yes api_key={'yes' if cfg.api_key else 'no'} "
        f"TE={'yes' if te_ok else 'NO'} FMP={'yes' if fmp_ok else 'NO'} "
        f"env={ENV_FILE}",
    )

    # -------------------------------------------------------------- 3 calendar
    cal_mgr = CalendarFeedManager(
        local_calendar_path=cfg.calendar_path or str(DEFAULT_CALENDAR),
        primary=cfg.calendar_provider,
        secondary=cfg.calendar_secondary,
        enable_stream=False,  # audit: REST/CSV only (stream probed separately)
        schedule_refresh_hours=float(cfg.calendar_schedule_refresh_hours),
        near_release_window_minutes=float(cfg.calendar_near_release_window_minutes),
        near_release_poll_seconds=float(cfg.calendar_near_release_poll_seconds),
        horizon_days=int(cfg.calendar_horizon_days),
        importance_required=cfg.news_importance_required,
    )
    try:
        cal_mgr.start()
        events = cal_mgr.snapshot()
        high_usd = sum(
            1 for e in events
            if (e.currency or "").upper() == "USD"
            and (e.importance or "").lower() == (cfg.news_importance_required or "high").lower()
        )
        primary = cal_mgr.stats.get("primary") or "none"
        if not events:
            diag.step("A03.CALENDAR_LOAD", FAIL, "0 events after startup")
        elif primary == "csv":
            diag.step(
                "A03.CALENDAR_LOAD", PARTIAL,
                f"events={len(events)} high_usd={high_usd} primary=csv "
                f"(add TE/FMP keys for live licensed feed)",
            )
        else:
            diag.step(
                "A03.CALENDAR_LOAD", OK,
                f"events={len(events)} high_usd={high_usd} primary={primary} "
                f"secondary={cal_mgr.stats.get('secondary')}",
            )
        tick = cal_mgr.tick()
        diag.step(
            "A04.CALENDAR_TICK", OK,
            f"event_count={tick.get('event_count')} near_release={tick.get('near_release')} "
            f"schedule_refreshed={tick.get('schedule_refreshed')}",
        )
    except Exception as exc:  # noqa: BLE001
        diag.step("A03.CALENDAR_LOAD", FAIL, f"{exc}")
        events = []
    finally:
        try:
            cal_mgr.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------- 4 TE / FMP REST (if keyed)
    if te_ok:
        try:
            from feeds import TradingEconomicsClient, _env_creds
            creds = _env_creds()
            te_key = creds["te_client"] or creds["te_key"]
            te = TradingEconomicsClient(te_key, creds["te_secret"])
            start = date.today() - timedelta(days=2)
            end = date.today() + timedelta(days=7)
            rows = te.fetch_calendar(start, end)
            diag.step(
                "A05.TE_REST",
                OK if rows else WARN,
                f"fetch returned {len(rows)} events for {start}..{end}",
            )
        except Exception as exc:  # noqa: BLE001
            diag.step("A05.TE_REST", FAIL, f"{exc}")
    else:
        diag.step("A05.TE_REST", SKIP, "TRADING_ECONOMICS_KEY not set — skipped")

    if fmp_ok:
        try:
            from feeds import FMPCalendarClient, _env_creds
            creds = _env_creds()
            fmp = FMPCalendarClient(creds["fmp_key"])
            start = date.today() - timedelta(days=2)
            end = date.today() + timedelta(days=7)
            rows = fmp.fetch_calendar(start, end)
            diag.step(
                "A06.FMP_REST",
                OK if rows else WARN,
                f"fetch returned {len(rows)} events for {start}..{end}",
            )
        except Exception as exc:  # noqa: BLE001
            diag.step("A06.FMP_REST", FAIL, f"{exc}")
    else:
        diag.step("A06.FMP_REST", SKIP, "FMP_API_KEY not set — skipped")

    # -------------------------------------------------------- 5 Upstox master
    client: Optional[UpstoxClient] = None
    ctx = None
    try:
        client = UpstoxClient(cfg)
        master = load_instrument_master(cfg.instrument_cache)
        diag.step(
            "A07.INSTRUMENT_MASTER",
            OK if master else FAIL,
            f"rows={len(master) if master else 0}",
        )
        fut = resolve_mcx_gold_future(
            master, cfg.gold_symbol, override_key=cfg.instrument_key_override,
        )
        ctx = mcx_context_from_instrument(fut, cfg.gold_symbol)
        cfg.value_per_price_unit = ctx.value_per_price_unit
        cfg.lot_size = ctx.lot_size
        diag.step(
            "A08.GOLD_CONTRACT", OK,
            f"{ctx.trading_symbol} key={ctx.instrument_key} lot={ctx.lot_size} "
            f"tick={ctx.tick_size}",
        )
    except Exception as exc:  # noqa: BLE001
        diag.step("A07.INSTRUMENT_MASTER", FAIL, f"{exc}")
        return _finish(diag)

    # ---------------------------------------------- 6 Upstox candles + quote
    assert client is not None and ctx is not None
    try:
        intraday = client.intraday(ctx.instrument_key, "1minute")
        if intraday.empty:
            diag.step(
                "A09.GOLD_INTRADAY", WARN,
                "empty (expected when MCX session closed) — API reachable",
            )
        else:
            age = age_minutes(intraday.index[-1], datetime.now(tz=UTC))
            st = OK if age is not None and age <= 15 else PARTIAL
            diag.step(
                "A09.GOLD_INTRADAY", st,
                f"bars={len(intraday)} last={intraday.index[-1]} age={fmt_num(age,1)}m",
            )
    except Exception as exc:  # noqa: BLE001
        diag.step("A09.GOLD_INTRADAY", FAIL, f"API error: {exc}")

    try:
        today = date.today()
        hist = client.historical(
            ctx.instrument_key, "1minute", today, today - timedelta(days=5),
        )
        if hist.empty:
            # widen window once
            hist = client.historical(
                ctx.instrument_key, "day", today, today - timedelta(days=30),
            )
        if hist.empty:
            diag.step("A10.GOLD_HISTORICAL", FAIL, "no historical candles returned")
        else:
            gold = resample_ohlcv(hist, cfg.timeframe_minutes) if "open" in hist.columns else hist
            diag.step(
                "A10.GOLD_HISTORICAL", OK,
                f"bars={len(hist)} last={hist.index[-1]} "
                f"resampled_{cfg.timeframe_minutes}m={len(gold) if hasattr(gold, '__len__') else '-'} "
                f"— Upstox history path OK (works on weekends)",
            )
    except Exception as exc:  # noqa: BLE001
        diag.step("A10.GOLD_HISTORICAL", FAIL, f"{exc}")

    try:
        bid, ask, ltp = client.bid_ask(ctx.instrument_key)
        q = client.quote(ctx.instrument_key)
        if bid is not None and ask is not None:
            diag.step(
                "A11.GOLD_QUOTE", OK,
                f"bid={fmt_num(bid,2)} ask={fmt_num(ask,2)} ltp={fmt_num(ltp,2)} "
                f"spread={fmt_num(ask-bid,2)}",
            )
        elif ltp is not None or (q and q.get("last_price")):
            px = ltp if ltp is not None else float(q.get("last_price"))
            diag.step(
                "A11.GOLD_QUOTE", WARN,
                f"no live book (session closed?) — last_price={fmt_num(px,2)} "
                f"quote_keys={list(q.keys())[:8] if q else []}",
            )
        elif q is not None:
            diag.step(
                "A11.GOLD_QUOTE", PARTIAL,
                f"quote object returned but no prices — keys={list(q.keys())[:12]}",
            )
        else:
            diag.step(
                "A11.GOLD_QUOTE", WARN,
                "empty quote (common when MCX closed) — HTTP path OK if no exception",
            )
    except Exception as exc:  # noqa: BLE001
        diag.step("A11.GOLD_QUOTE", FAIL, f"{exc}")

    # ----------------------------------------------------- 7 Yahoo macros force
    try:
        fresh_dxy = fetch_dxy_yahoo()
        if fresh_dxy.empty:
            # fallback: prove cache readable
            cached = load_macros(allow_yahoo=False, persist_cache=False)[0]
            if cached.empty:
                diag.step("A12.YAHOO_DXY", FAIL, "live fetch empty and no cache")
            else:
                diag.step(
                    "A12.YAHOO_DXY", WARN,
                    f"live fetch empty (Yahoo/weekend) — cache pts={len(cached)} "
                    f"last={cached.index[-1]} path={DEFAULT_DXY_CACHE}",
                )
        else:
            age = age_minutes(fresh_dxy.index[-1], datetime.now(tz=UTC))
            st = OK if age is not None and age < 24 * 60 else WARN
            diag.step(
                "A12.YAHOO_DXY", st,
                f"live fetch pts={len(fresh_dxy)} last={fresh_dxy.index[-1]} "
                f"val={fmt_num(fresh_dxy.iloc[-1])} age={fmt_num(age,0)}m",
            )
    except Exception as exc:  # noqa: BLE001
        diag.step("A12.YAHOO_DXY", FAIL, f"{exc}")

    try:
        fresh_yld = fetch_us10y_yahoo()
        if fresh_yld.empty:
            cached = load_macros(allow_yahoo=False, persist_cache=False)[1]
            if cached.empty:
                diag.step("A13.YAHOO_US10Y", FAIL, "live fetch empty and no cache")
            else:
                diag.step(
                    "A13.YAHOO_US10Y", WARN,
                    f"live fetch empty — cache pts={len(cached)} "
                    f"last={cached.index[-1]} path={DEFAULT_YIELD_CACHE}",
                )
        else:
            age = age_minutes(fresh_yld.index[-1], datetime.now(tz=UTC))
            st = OK if age is not None and age < 24 * 60 else WARN
            diag.step(
                "A13.YAHOO_US10Y", st,
                f"live fetch pts={len(fresh_yld)} last={fresh_yld.index[-1]} "
                f"val={fmt_num(fresh_yld.iloc[-1])} age={fmt_num(age,0)}m",
            )
    except Exception as exc:  # noqa: BLE001
        diag.step("A13.YAHOO_US10Y", FAIL, f"{exc}")

    dxy, yld = load_macros(
        cfg.dxy_csv, cfg.yield_csv, allow_yahoo=True, persist_cache=True,
    )
    now = datetime.now(tz=UTC)
    st_d, det_d = series_health(
        "DXY", dxy, now, stale_limit_minutes=float(cfg.macro_stale_minutes),
        source="audit_load",
    )
    st_y, det_y = series_health(
        "US10Y", yld, now, stale_limit_minutes=float(cfg.macro_stale_minutes),
        source="audit_load",
    )
    # In audit, macro WARN (weekend gap) is acceptable if series non-empty
    if dxy.empty:
        diag.step("A14.MACROS_READY", FAIL, det_d)
    else:
        diag.step("A14.MACROS_READY", st_d if st_d != FAIL else WARN, det_d)
    if yld.empty:
        diag.step("A15.MACROS_US10Y_READY", FAIL, det_y)
    else:
        diag.step("A15.MACROS_US10Y_READY", st_y if st_y != FAIL else WARN, det_y)

    # ------------------------------------------- 8 offline logic (always test)
    try:
        assert event_polarity("US CPI m/m") == "hawkish_on_beat"
        assert surprise_to_gold_bias("US CPI m/m", "beat") == "bearish"
        assert classify_surprise(0.5, 0.3) == "beat"
        assert combine_bias("bearish", "bearish")[0] == "bearish"
        diag.step("A16.SURPRISE_LOGIC", OK, "polarity/classify/combine assertions passed")
    except AssertionError as exc:
        diag.step("A16.SURPRISE_LOGIC", FAIL, f"{exc}")

    try:
        data = generate_synthetic_dataset(n_bars=80, seed=3)
        now_syn = pd.Timestamp(data["gold"].index[-1]).to_pydatetime()
        if now_syn.tzinfo is None:
            now_syn = now_syn.replace(tzinfo=UTC)
        else:
            now_syn = now_syn.astimezone(UTC)
        released = EconomicEvent(
            "audit-cpi", "US CPI m/m", "USD", "high",
            scheduled_time_utc=now_syn - timedelta(minutes=45),
            forecast=0.3, previous=0.2, actual=0.5, status="released",
            actual_release_time_utc=now_syn - timedelta(minutes=45),
        )
        # Clean macro series: DXY + yields UP over 15m → bearish gold (agrees with hot CPI)
        t0 = pd.Timestamp(now_syn - timedelta(minutes=20)).tz_convert("UTC")
        t1 = pd.Timestamp(now_syn - timedelta(minutes=1)).tz_convert("UTC")
        dxy_syn = pd.Series([100.0, 100.20], index=pd.DatetimeIndex([t0, t1]))
        yld_syn = pd.Series([4.00, 4.10], index=pd.DatetimeIndex([t0, t1]))
        # Bearish breakout candle
        gold = data["gold"].copy()
        prev_low = float(gold.iloc[-7:-1]["low"].min())
        gold.iloc[-1, gold.columns.get_loc("close")] = prev_low - 1.0
        gold.iloc[-1, gold.columns.get_loc("low")] = prev_low - 2.0

        nd = NewsDecisionEngine(
            EngineConfig.from_mapping(
                news_mode="filtered", use_release_surprise=True,
                surprise_reaction_minutes=180, require_surprise_macro_agree=True,
                use_real_yield=False,
                stale_data_minutes=10_000, macro_stale_minutes=10_000,
                macro_trade_max_age_minutes=10_000,
                maximum_spread_points=10_000, margin_pct_of_notional=0.0,
            ),
            events=[released],
        )
        if not validate_candle_frame(gold):
            diag.step("A17.DECISION_PATH", FAIL, "synthetic gold frame invalid")
        else:
            dec = nd.decide(
                now_utc=now_syn, dxy=dxy_syn, yields=yld_syn, gold_candles=gold,
                daily_loss_limit_reached=False, has_open_position=False,
                gold_data_valid=True,
            )
            if dec.decision == "SELL":
                diag.step(
                    "A17.DECISION_PATH", OK,
                    f"synthetic hot-CPI → {dec.decision} reason={dec.reason} "
                    f"bias={dec.bias} surprise={dec.news.surprise.surprise if dec.news.surprise else '-'}",
                )
            else:
                diag.step(
                    "A17.DECISION_PATH", FAIL,
                    f"expected SELL, got {dec.decision}/{dec.reason} bias={dec.bias} "
                    f"dxy={dec.macros.dxy_direction} yld={dec.macros.yield_direction} "
                    f"sur={dec.news.surprise.gold_bias if dec.news.surprise else '-'}",
                )
    except Exception as exc:  # noqa: BLE001
        diag.step("A17.DECISION_PATH", FAIL, f"{exc}")
        log.exception("Decision path audit failed")

    # --------------------------------------------- 9 risk + paper execution
    try:
        data = generate_synthetic_dataset(n_bars=60, seed=5)
        gold = data["gold"]
        mid = float(gold.iloc[-1]["close"])
        risk = RiskManager(EngineConfig.from_mapping(
            sizing_mode="fixed", fixed_lots=1, maximum_spread_points=10_000,
            margin_pct_of_notional=0.0, value_per_price_unit=10.0,
            estimated_cost_per_unit=5.0, initial_equity=500_000,
        ))
        plan = risk.build_trade_plan("BUY", mid - 1, mid + 1, gold, spread_points=2.0)
        if plan.rejected:
            diag.step("A18.RISK_PLAN", FAIL, f"rejected: {plan.reject_reason}")
        else:
            diag.step(
                "A18.RISK_PLAN", OK,
                f"BUY lots={plan.lots} qty={plan.quantity} "
                f"SL={fmt_num(plan.stop_price,2)} TP={fmt_num(plan.target_price,2)}",
            )
            exe = ExecutionEngine(
                risk, client=None, live=False,
                value_per_price_unit=10.0, estimated_cost_per_unit=5.0,
                persist_state=False,
            )
            pos = exe.open_trade(plan, datetime.now(tz=UTC), instrument_key="AUDIT|SYN")
            if pos is None:
                diag.step("A19.PAPER_EXEC", FAIL, "open_trade returned None")
            else:
                diag.step(
                    "A19.PAPER_EXEC", OK,
                    f"paper OPEN {pos.side} qty={pos.quantity} @ {fmt_num(pos.entry_price,2)}",
                )
                exe.monitor(
                    datetime.now(tz=UTC), mid - 1, mid + 1,
                    bar_high=mid + 5, bar_low=mid - 5, data_valid=True,
                )
                diag.step("A20.POSITION_MONITOR", OK, "monitor() ran without error")
    except Exception as exc:  # noqa: BLE001
        diag.step("A18.RISK_PLAN", FAIL, f"{exc}")
        log.exception("Risk/exec audit failed")

    # ------------------------------------------------------------- 10 summary
    return _finish(diag)


def _finish(diag: CycleDiagnostics) -> int:
    counts = diag.end()
    fails = counts.get(FAIL, 0)
    log.info("")
    log.info("AUDIT RESULT: %s", "PASS (no FAIL steps)" if fails == 0 else f"FAIL ({fails} steps)")
    log.info(
        "  Tip: WARN on gold intraday/quote + weekend macros is normal on Sunday. "
        "FAIL means a component is broken and needs fixing."
    )
    if fails:
        for s in diag.steps:
            if s.status == FAIL:
                log.error("  → %s: %s", s.step, s.detail)
    return 0 if fails == 0 else 2
# -------------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Gold News Trading Engine (Upstox/MCX)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_FILE),
                   help="Path to engine config.json (default: gold-trade-engine/config.json)")
    p.add_argument("--mode", choices=["paper", "live"], default=None,
                   help="Override config mode (default from config.json: paper)")
    p.add_argument("--lots", type=int, default=None,
                   help="Override fixed lots (default from config.json: 1)")
    p.add_argument("--once", action="store_true", help="Single paper/live cycle then exit")
    p.add_argument(
        "--no-producers", action="store_true",
        help="Do not start embedded monitor-gold / commodity-events threads",
    )
    p.add_argument(
        "--audit", action="store_true",
        help="Audit/validation run: probe every component (safe on weekends; "
             "session-closed → WARN, broken wiring → FAIL)",
    )
    p.add_argument("--selftest", action="store_true", help="Offline unit checks")
    p.add_argument("--backtest-synthetic", action="store_true",
                   help="Run backtest on synthetic data")
    p.add_argument("--backtest", nargs=2, metavar=("FROM", "TO"),
                   help="Upstox historical backtest YYYY-MM-DD YYYY-MM-DD")
    p.add_argument("--symbol", default=None, help="MCX underlying: GOLD, GOLDM, ...")
    p.add_argument("--instrument-key", default="", help="Override instrument key")
    p.add_argument("--news-mode", choices=["filtered", "aware"], default=None)
    p.add_argument("--calendar", default=None,
                   help="Economic calendar CSV/JSON (default: engine sample_data)")
    p.add_argument("--dxy-csv", default="")
    p.add_argument("--yield-csv", default="")
    p.add_argument("--gold-csv", default="", help="Optional local gold OHLCV CSV")
    p.add_argument("--yahoo-macros", action="store_true",
                   help="Fetch DXY/US10Y from Yahoo (research only)")
    p.add_argument("--equity", type=float, default=None)
    p.add_argument("--spread", type=float, default=None)
    p.add_argument("--login", action="store_true")
    p.add_argument("--exchange-code", default="")
    p.add_argument("--calendar-sources", action="store_true",
                   help="Print licensed + official calendar source guidance and exit")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    ensure_engine_dirs()
    env_path = str(ENV_FILE)
    args.equity_explicit = args.equity is not None
    if args.equity is None:
        args.equity = 500_000.0
    if args.symbol is None:
        args.symbol = ""
    if args.news_mode is None:
        args.news_mode = ""
    if args.calendar is None:
        args.calendar = str(DEFAULT_CALENDAR)
    if args.mode is None:
        args.mode = ""

    if args.selftest:
        return run_selftest()

    # Compact dashboard unless --verbose (keeps the old step-by-step audit dump)
    CycleDiagnostics.compact_default = not bool(args.verbose)

    if args.audit:
        cfg = build_cfg(args)
        if args.yahoo_macros:
            cfg.yahoo_macros = True
        log.info("Loaded config %s -> AUDIT mode=%s", args.config, cfg.mode)
        return run_audit(cfg)

    if args.calendar_sources:
        print(official_links_markdown())
        print()
        print("Licensed API keys go in repo-root env.txt — see")
        print("  gold-trade-engine/sample_data/calendar_api_keys.example.txt")
        print()
        print("Intervals (config.json):")
        print("  calendar_schedule_refresh_hours     = 6   (schedule poll)")
        print("  calendar_near_release_poll_seconds  = 45  (near high-impact)")
        print("  calendar_stream                     = true (TE WebSocket actuals)")
        return 0

    if args.login:
        from upstox_client import load_env_file
        env = load_env_file(env_path)
        client_id = env.get("UPSTOX_API_TOKEN") or env.get("UPSTOX_API_KEY") or ""
        redirect = env.get("UPSTOX_REDIRECT_URI") or ""
        if not client_id or not redirect:
            print(f"Set UPSTOX_API_TOKEN and UPSTOX_REDIRECT_URI in {env_path} first.")
            return 2
        print(UpstoxClient.login_url(client_id, redirect))
        return 0

    if args.exchange_code:
        from upstox_client import load_env_file
        env = load_env_file(env_path)
        client_id = env.get("UPSTOX_API_TOKEN") or env.get("UPSTOX_API_KEY") or ""
        secret = env.get("UPSTOX_API_SECRET") or ""
        redirect = env.get("UPSTOX_REDIRECT_URI") or ""
        if not (client_id and secret and redirect):
            print(
                "UPSTOX_API_TOKEN, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI required in "
                f"{env_path}."
            )
            return 2
        payload = UpstoxClient.exchange_code(
            code=args.exchange_code, client_id=client_id,
            client_secret=secret, redirect_uri=redirect,
        )
        token = (payload or {}).get("access_token")
        if not token:
            print("Token exchange failed:", payload)
            return 2
        update_env_file(env_path, "UPSTOX_ACCESS_TOKEN", token)
        print(f"Updated {env_path} with fresh UPSTOX_ACCESS_TOKEN")
        return 0

    if args.backtest_synthetic:
        file_cfg = load_config_file(args.config)
        cfg = EngineConfig.from_mapping(
            file_cfg,
            news_mode=args.news_mode or file_cfg.get("news_mode", "aware"),
            initial_equity=args.equity,
            maximum_spread_points=1000, risk_per_trade_pct=1.0,
            maximum_daily_loss_pct=5.0,
            value_per_price_unit=10.0, margin_pct_of_notional=0.05,
            env_file=env_path,
            sizing_mode="fixed",
            fixed_lots=int(args.lots or file_cfg.get("fixed_lots") or 1),
        )
        data = generate_synthetic_dataset(n_bars=500, seed=11)
        result = run_backtest(
            data["gold"], data["dxy"], data["yields"], data["events"],
            cfg=cfg, real_yields=data["real_yields"], bid_ask_spread=2.0,
        )
        print(result.performance)
        write_trades_csv(result.trades, OUTPUT_DIR / "gold_trades_synthetic.csv")
        write_rejects_csv(result.rejects, OUTPUT_DIR / "gold_rejects_synthetic.csv")
        return 0

    if args.gold_csv and args.backtest:
        cfg = build_cfg(args)
        gold = load_ohlcv_csv(args.gold_csv)
        gold = resample_ohlcv(gold, cfg.timeframe_minutes)
        dxy, yields = maybe_fetch_macros(
            cfg.dxy_csv, cfg.yield_csv,
            allow_yahoo=cfg.yahoo_macros or args.yahoo_macros,
        )
        events = load_calendar(cfg.calendar_path)
        spread = max(2.0, float(cfg.maximum_spread_points) * 0.1)
        result = run_backtest(
            gold, dxy, yields, events, cfg=cfg, bid_ask_spread=spread,
        )
        print(result.performance)
        write_trades_csv(result.trades, OUTPUT_DIR / "gold_trades_csv.csv")
        return 0

    if args.backtest:
        cfg = build_cfg(args)
        start = date.fromisoformat(args.backtest[0])
        end = date.fromisoformat(args.backtest[1])
        return run_upstox_backtest(cfg, start, end, yahoo_macros=cfg.yahoo_macros)

    # Default action: run paper/live from config.json (paper + 1 lot by default).
    cfg = build_cfg(args)
    log.info("Loaded config %s -> mode=%s fixed_lots=%s",
             args.config, cfg.mode, cfg.fixed_lots)
    try:
        return run_paper_or_live(cfg, once=args.once)
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
