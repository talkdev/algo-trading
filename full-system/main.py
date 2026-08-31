# ============ FILE: main.py ============
"""
Orchestrator — starts and manages all components.

ALL FIXES APPLIED (passes 1-7 + live confirmed):
  LIVE: EXEC_END_TIME=14:00 (was 11:00) — enables trading
  LIVE: EXEC_START_TIME=09:30 (avoids opening auction)
  LIVE: _is_expiry_day fallback weekday==1 (Tuesday confirmed)
  LIVE: _last_trading_day() for candle to_date
  LIVE: WS kill switch market-hours check
  LIVE: Console uses os.system("cls") on Windows
  LIVE: Entry gate logs every block reason
  LIVE: data_refresh_complete set in finally block
  LIVE: Overnight keep condition dte>5 (not dte>3)
  LIVE: TIME_EXIT_EXPIRY=15:10 in config
"""

import asyncio
import signal
import sys
import os
import logging
import traceback
import aiohttp
import numpy as np
import sqlite3
import json
from datetime import datetime, date, timedelta
from typing import Optional, Dict
import pytz
import config
from data_manager import DataManager, _last_trading_day
from regime_engine import RegimeEngine
from strategy_engine import StrategyEngine


# ─────────────────────────────────────────────────────────────────────
# Module-level logger — handlers added by setup_logging() in main()
# ─────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# Console line tracking for overwrite
_CONSOLE_LINE_COUNT = 0


def setup_logging() -> logging.Logger:
    """Configure file and console logging."""
    IST       = pytz.timezone(config.TZ)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | "
        "%(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(
        getattr(logging, config.LOG_LEVEL, logging.INFO)
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(fmt)
    root_logger.addHandler(console_handler)

    audit_file = os.path.join(
        config.LOG_DIR,
        f"audit_log_{today_str}.log",
    )
    file_handler = logging.FileHandler(
        audit_file, mode="a", encoding="utf-8"
    )
    file_handler.setLevel(
        getattr(logging, config.LOG_LEVEL, logging.INFO)
    )
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    module_logger = logging.getLogger(__name__)
    module_logger.info(
        f"Logging initialized: {audit_file}"
    )
    return module_logger


def _load_access_token() -> Optional[str]:
    try:
        with open(config.TOKEN_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("UPSTOX_ACCESS_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    if token:
                        logger.info("Access token loaded")
                        return token
        logger.critical(
            f"Token not found in {config.TOKEN_FILE}"
        )
        return None
    except FileNotFoundError:
        logger.critical(
            f"env.txt not found at {config.TOKEN_FILE}"
        )
        return None
    except Exception as e:
        logger.critical(f"Token load error: {e}")
        return None


async def _run_preflight_checks(
    access_token: str,
) -> bool:
    """Run all pre-flight validation checks."""
    IST       = pytz.timezone(config.TZ)
    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")

    # CHECK 1 — Trading day
    if today_str in config.NSE_MARKET_HOLIDAYS:
        if not config.ALLOW_NON_TRADING_DAY_RUN:
            logger.critical(
                f"Today {today_str} is an NSE holiday"
            )
            return False
        else:
            logger.warning(
                f"Running on holiday {today_str} "
                f"(ALLOW_NON_TRADING_DAY_RUN=True)"
            )

    if today.weekday() >= 5:
        if today_str not in config.NSE_SPECIAL_TRADING_DAYS:
            if not config.ALLOW_NON_TRADING_DAY_RUN:
                logger.critical(
                    "Weekend — market closed"
                )
                return False
            else:
                logger.warning(
                    "Running on weekend (test mode)"
                )

    # CHECK 2 — NTP clock sync (live mode only)
    if not config.PAPER_TRADING_MODE:
        try:
            import ntplib
            client   = ntplib.NTPClient()
            response = client.request(
                config.NTP_SERVER, version=3
            )
            offset = abs(response.offset)
            if offset > config.NTP_MAX_OFFSET_SEC:
                logger.critical(
                    f"Clock offset {offset:.3f}s > "
                    f"max {config.NTP_MAX_OFFSET_SEC}s"
                )
                return False
            else:
                logger.info(
                    f"NTP sync OK: offset={offset:.3f}s"
                )
        except ImportError:
            logger.warning(
                "ntplib not installed — skipping NTP check"
            )
        except Exception as e:
            logger.warning(
                f"NTP check failed: {e} — proceeding"
            )

    # CHECK 3 — Token validation
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                config.UPSTOX_BASE_V2 + config.EP_PROFILE,
                headers={
                    "Authorization": (
                        f"Bearer {access_token}"
                    )
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = (
                        data.get("data", {}).get(
                            "name", "Unknown"
                        )
                        or data.get("name", "Unknown")
                    )
                    logger.info(
                        f"Token valid — User: {name}"
                    )
                elif resp.status == 401:
                    logger.critical(
                        "Token expired or invalid"
                    )
                    return False
                else:
                    logger.warning(
                        f"Profile check returned "
                        f"{resp.status} — proceeding"
                    )
    except Exception as e:
        logger.warning(
            f"Token validation error: {e} — proceeding"
        )

    # CHECK 4 — Execution window
    now_time = datetime.now(IST).time()
    if (
        now_time > config.EXEC_END_TIME
        and now_time < config.MARKET_CLOSE
    ):
        logger.warning(
            f"Entry window closed "
            f"({config.EXEC_START_TIME}-"
            f"{config.EXEC_END_TIME}) — monitoring only"
        )

    # CHECK 5 — Log directory
    if not os.path.exists(config.LOG_DIR):
        os.makedirs(config.LOG_DIR, exist_ok=True)

    # CHECK 6 — Holiday calendar review date
    days_since = (
        today - config.HOLIDAY_CALENDAR_REVIEWED_ON
    ).days
    if days_since > 180:
        logger.warning(
            f"Holiday calendar last reviewed "
            f"{days_since} days ago — please update"
        )

    # CHECK 7 — High impact events today
    if today_str in config.HIGH_IMPACT_EVENTS:
        event_name = config.HIGH_IMPACT_EVENTS[today_str]
        logger.warning(
            f"HIGH IMPACT EVENT TODAY: {event_name}"
        )

    logger.info(
        f"Pre-flight checks passed — "
        f"mode="
        f"{'PAPER' if config.PAPER_TRADING_MODE else 'LIVE'}"
    )
    return True


async def _wait_for_market_open(
    shutdown_event: asyncio.Event,
) -> None:
    """Wait until market opens. Uses IST.localize()."""
    IST = pytz.timezone(config.TZ)

    while not shutdown_event.is_set():
        now      = datetime.now(IST)
        now_time = now.time()

        if now_time >= config.MARKET_OPEN:
            logger.info(
                f"Market open — starting engine "
                f"(time={now_time})"
            )
            return

        market_open_naive = datetime.combine(
            now.date(), config.MARKET_OPEN
        )
        market_open_dt = IST.localize(market_open_naive)
        wait_sec = (
            market_open_dt - now
        ).total_seconds()

        if wait_sec > 0:
            logger.info(
                f"Waiting {wait_sec:.0f}s for market open"
            )
            while (
                wait_sec > 0
                and not shutdown_event.is_set()
            ):
                sleep_chunk = min(30.0, wait_sec)
                await asyncio.sleep(sleep_chunk)
                wait_sec -= sleep_chunk
        else:
            return


async def _find_nearest_expiry(
    dm: DataManager,
) -> str:
    """
    Find the nearest valid NIFTY option expiry.
    Scans forward until chain returns data.
    LIVE CONFIRMED: NSE weekly expiry = Tuesday (weekday=1).
    Does NOT hardcode any weekday.
    """
    today = date.today()
    for days in range(1, 60):
        check_date = (
            today + timedelta(days=days)
        ).strftime("%Y-%m-%d")
        try:
            chain = await dm.fetch_option_chain(
                check_date
            )
            if chain:
                logger.info(
                    f"Found valid expiry: {check_date}"
                )
                return check_date
        except Exception:
            pass
        await asyncio.sleep(0.2)

    logger.warning(
        "Could not find valid expiry in 60 days — "
        "using today + 7"
    )
    return (
        today + timedelta(days=7)
    ).strftime("%Y-%m-%d")


async def _ensure_future_expiry_coverage(
    dm: DataManager,
    min_dte_buffer: int = 8,
) -> None:
    """
    PATCH: keep at least one expiry with DTE >= min_dte_buffer
    loaded at all times. Without this, once the nearest known
    expiry drops below the strategies' minimum DTE (or expires),
    the engine can get stuck unable to build any trade, since the
    old discovery loop only re-ran when the known expiry list was
    completely empty.
    """
    try:
        today = date.today()
        known = dm.get_available_expiries()
        future_dtes = []
        for exp_str in known:
            try:
                exp_date = datetime.strptime(
                    exp_str, "%Y-%m-%d"
                ).date()
                dte = (exp_date - today).days
                if dte >= 0:
                    future_dtes.append(dte)
            except ValueError:
                continue

        max_dte = max(future_dtes) if future_dtes else -1
        if max_dte >= min_dte_buffer:
            return

        start_day = max_dte + 1 if max_dte >= 0 else 1
        logger.info(
            f"Expiry coverage low (max_dte={max_dte}) — "
            f"scanning forward from day {start_day}"
        )
        for days in range(start_day, start_day + 60):
            check_date = (
                today + timedelta(days=days)
            ).strftime("%Y-%m-%d")
            if check_date in known:
                continue
            try:
                chain = await dm.fetch_option_chain(check_date)
                if chain:
                    logger.info(
                        f"Expiry coverage: found new "
                        f"expiry {check_date}"
                    )
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)

        logger.warning(
            "Expiry coverage: forward scan found no "
            "additional expiry"
        )
    except Exception as e:
        logger.error(
            f"_ensure_future_expiry_coverage error: {e}"
        )


async def _ensure_term_structure_expiry(
    dm: DataManager,
    target_dte_low: int = 28,
    target_dte_high: int = 42,
) -> None:
    """
    PATCH: without this, dm.forward_iv always falls back to
    VIX/100 (see _compute_forward_iv in data_manager.py), because
    nothing ever fetches an expiry in the 30-45 DTE window needed
    for a genuine term-spread signal. NIFTY's weekly series
    naturally has one every month (the "monthly" contract is just
    that week's expiry) — this just needs to actually be fetched.
    Checked infrequently (every ~6h) from the main loop, not on
    every 60s data-refresh cycle.
    """
    try:
        today = date.today()
        known = dm.get_available_expiries()
        for exp_str in known:
            try:
                exp_date = datetime.strptime(
                    exp_str, "%Y-%m-%d"
                ).date()
                dte = (exp_date - today).days
                if target_dte_low <= dte <= target_dte_high:
                    return
            except ValueError:
                continue

        logger.info(
            "Term-structure expiry coverage: scanning "
            f"{target_dte_low}-{target_dte_high} DTE window"
        )
        for days in range(target_dte_low, target_dte_high + 1):
            check_date = (
                today + timedelta(days=days)
            ).strftime("%Y-%m-%d")
            if check_date in known:
                continue
            try:
                chain = await dm.fetch_option_chain(check_date)
                if chain:
                    logger.info(
                        f"Term-structure expiry found: "
                        f"{check_date} (dte={days})"
                    )
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)

        logger.warning(
            "Term-structure expiry coverage: no expiry found "
            f"in {target_dte_low}-{target_dte_high} DTE window"
        )
    except Exception as e:
        logger.error(
            f"_ensure_term_structure_expiry error: {e}"
        )


def _is_expiry_day(
    dm: Optional[DataManager] = None,
) -> bool:
    """
    Check if today is a NIFTY expiry day.
    PRIMARY: checks actual chain expiry list.
    FALLBACK: weekday==NSE_WEEKLY_EXPIRY_WEEKDAY=1 (Tuesday)
    LIVE CONFIRMED: all 6 expiries in next 60 days = Tuesday.
    """
    today_str = date.today().isoformat()
    if dm is not None:
        try:
            expiries = dm.get_available_expiries()
            if today_str in expiries:
                return True
        except Exception:
            pass
    # LIVE FIX: Tuesday = weekday 1 (confirmed)
    return date.today().weekday() == (
        config.NSE_WEEKLY_EXPIRY_WEEKDAY
    )


async def _end_of_day(
    se: StrategyEngine,
    dm: DataManager,
) -> None:
    """
    Execute end-of-day procedures.
    Uses live regime (not frozen at 14:45).
    Overnight keep condition: dte>5 (not dte>3).
    """
    IST = pytz.timezone(config.TZ)
    logger.info("=" * 60)
    logger.info("END OF DAY PROCEDURES STARTING")
    logger.info("=" * 60)

    logger.info("EOD: Running cancel sweep")
    cancelled = await se.cancel_all_open_orders(
        context="EOD_CANCEL_SWEEP"
    )
    if cancelled > 0:
        logger.info(
            f"EOD: Cancelled {cancelled} open orders"
        )
    await asyncio.sleep(1.0)

    for position in list(se.open_positions):
        regime = se.re.confirmed_regime

        try:
            expiry = datetime.strptime(
                position.expiry_date, "%Y-%m-%d"
            ).date()
            dte = (expiry - date.today()).days
        except (ValueError, TypeError):
            dte = 0

        should_close = True

        tomorrow_str = (
            date.today() + timedelta(days=1)
        ).isoformat()
        tomorrow_is_expiry = (
            tomorrow_str in dm.get_available_expiries()
        )
        vix_ok = (
            dm.vix is not None
            and dm.vix < config.VIX_SELL_VOL_MAX
        )

        # LIVE FIX: dte>5 (was dte>3)
        # dte>3 allowed DTE=8 positions overnight for 6 days.
        # dte>5 limits overnight hold to Mon/Tue entry only.
        if (
            regime in [
                config.REGIME_STRONG_SELL,
                config.REGIME_MILD_SELL,
            ]
            and dte > 5
            and position.strategy_name in [
                config.STRAT_SHORT_STRADDLE,
                config.STRAT_IRON_CONDOR,
                config.STRAT_CREDIT_SPREADS,
            ]
            and not tomorrow_is_expiry
            and vix_ok
        ):
            should_close = False
            logger.info(
                f"Keeping overnight: "
                f"{position.strategy_name} "
                f"trade_id={position.trade_id[:8]} "
                f"dte={dte} vix={dm.vix:.1f}"
            )

        if should_close:
            await se._close_position(
                position,
                config.EXIT_REASONS["EOD"],
            )

    _generate_eod_report(se, dm)

    dm.save_state_to_sqlite({
        "timestamp":       datetime.now(IST).isoformat(),
        "spot":            dm.spot,
        "vix":             dm.vix,
        "iv_atm":          dm.iv_atm,
        "rv_20d":          dm.rv_20d,
        "skew":            dm.skew,
        "adx":             dm.adx,
        "ema_50":          dm.ema_50,
        "composite_score": se.re.raw_composite,
        "regime":          se.re.confirmed_regime,
    })
    se.re.save_buffers_to_sqlite()

    logger.info("EOD procedures complete")


async def _expiry_day_close_all(
    se: StrategyEngine,
) -> None:
    """
    Force close all expiring positions.
    TIME_EXIT_EXPIRY=15:10 (in config) captures more theta.
    _close_position skips OTM legs with LTP<0.10.
    """
    logger.info(
        "EXPIRY DAY: Force closing expiring positions"
    )

    cancelled = await se.cancel_all_open_orders(
        context="EXPIRY_CANCEL_SWEEP"
    )
    if cancelled > 0:
        logger.info(
            f"EXPIRY DAY: Cancelled {cancelled} orders"
        )
    await asyncio.sleep(1.0)

    for position in list(se.open_positions):
        try:
            expiry = datetime.strptime(
                position.expiry_date, "%Y-%m-%d"
            ).date()
        except (ValueError, TypeError):
            continue

        if expiry == date.today():
            await se._close_position(
                position,
                config.EXIT_REASONS["EXPIRY"],
                use_market=True,
            )
            logger.info(
                f"Expiry closed: "
                f"{position.strategy_name} "
                f"trade_id={position.trade_id[:8]}"
            )

    logger.info("Expiry day close complete")


async def _graceful_shutdown(
    se: StrategyEngine,
    dm: DataManager,
    shutdown_event: asyncio.Event,
) -> None:
    """Execute graceful shutdown. Guards against None RegimeEngine."""
    IST = pytz.timezone(config.TZ)
    logger.info("=" * 60)
    logger.info("GRACEFUL SHUTDOWN INITIATED")
    logger.info("=" * 60)

    try:
        cancelled = await se.cancel_all_open_orders(
            context="SHUTDOWN_CANCEL_SWEEP"
        )
        if cancelled > 0:
            logger.warning(
                f"SHUTDOWN: Cancelled {cancelled} "
                f"open orders"
            )
        await asyncio.sleep(1.0)
    except Exception as e:
        logger.error(
            f"SHUTDOWN: Cancel sweep failed: {e}"
        )

    if not config.PAPER_TRADING_MODE:
        logger.info("Live mode: squaring off positions")
        for position in list(se.open_positions):
            try:
                await se._close_position(
                    position,
                    config.EXIT_REASONS["MANUAL"],
                    use_market=True,
                )
            except Exception as e:
                logger.error(
                    f"Shutdown close error "
                    f"{position.trade_id[:8]}: {e}"
                )
    else:
        logger.info(
            "Paper mode: logging open positions"
        )
        for position in se.open_positions:
            logger.info(
                f"Open at shutdown: "
                f"{position.strategy_name} "
                f"trade_id={position.trade_id[:8]} "
                f"net_pnl=₹{position.net_pnl:,.2f}"
            )

    if dm.ws is not None and dm.ws_connected:
        try:
            await dm.ws.close()
            logger.info("WebSocket closed")
        except Exception as e:
            logger.warning(f"WS close error: {e}")

    if (
        dm.session is not None
        and not dm.session.closed
    ):
        try:
            await dm.session.close()
            logger.info("HTTP session closed")
        except Exception as e:
            logger.warning(f"Session close error: {e}")

    try:
        dm.save_state_to_sqlite({
            "timestamp":       datetime.now(
                IST
            ).isoformat(),
            "spot":            dm.spot,
            "vix":             dm.vix,
            "iv_atm":          dm.iv_atm,
            "rv_20d":          dm.rv_20d,
            "skew":            dm.skew,
            "adx":             dm.adx,
            "ema_50":          dm.ema_50,
            "composite_score": (
                se.re.raw_composite
                if se.re is not None else 0.0
            ),
            "regime": (
                se.re.confirmed_regime
                if se.re is not None
                else config.REGIME_NEUTRAL
            ),
        })
    except Exception as e:
        logger.warning(f"Final state save error: {e}")

    try:
        if se.re is not None:
            se.re.save_buffers_to_sqlite()
    except Exception as e:
        logger.warning(f"Buffer save error: {e}")

    _generate_eod_report(se, dm)

    logger.info("Shutdown complete")
    shutdown_event.set()


def _display_console(dm, re, se, cached_greeks=None):
    """Sequential console — append only, no screen clear."""
    IST      = pytz.timezone(config.TZ)
    now      = datetime.now(IST)
    now_str  = now.strftime("%Y-%m-%d %H:%M:%S")
    now_time = now.time()

    G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
    B = "\033[94m"; E = "\033[0m"
    PASS_S = f"{G}PASS{E}"; FAIL_S = f"{R}FAIL{E}"; NA_S = f"{B} N/A{E}"

    RCOLORS = {
        config.REGIME_STRONG_SELL: G,
        config.REGIME_MILD_SELL:   "\033[32m",
        config.REGIME_NEUTRAL:     Y,
        config.REGIME_BUY_VOL:     R,
        config.REGIME_STRONG_BUY:  "\033[31m",
        config.REGIME_EVENT:       "\033[95m",
    }

    def ps(ok, na=False):
        return NA_S if na else (PASS_S if ok else FAIL_S)

    greeks = cached_greeks or se._get_portfolio_greeks()
    regime = re.confirmed_regime
    rc     = RCOLORS.get(regime, "")

    today_str = now.date().strftime("%Y-%m-%d")
    is_tday   = (
        now.date().weekday() < 5
        and today_str not in config.NSE_MARKET_HOLIDAYS
    )
    is_mkt    = config.MARKET_OPEN <= now_time <= config.MARKET_CLOSE
    is_entry  = config.EXEC_START_TIME <= now_time <= config.EXEC_END_TIME

    if not is_tday:
        eng = f"{Y}WAITING — Non-trading day{E}"
    elif not is_mkt:
        eng = (
            f"{Y}WAITING — Market opens at {config.MARKET_OPEN}{E}"
            if now_time < config.MARKET_OPEN
            else f"{Y}WAITING — Market closed{E}"
        )
    elif not is_entry:
        eng = (
            f"{Y}WAITING — Entry opens at {config.EXEC_START_TIME}{E}"
            if now_time < config.EXEC_START_TIME
            else f"{Y}MONITORING ONLY — Entry closed after {config.EXEC_END_TIME}{E}"
        )
    elif se.kill_switch_active:
        eng = f"{R}KILL SWITCH — monitoring only{E}"
    elif se.daily_trading_halted:
        eng = f"{R}DAILY HALTED — monitoring only{E}"
    elif dm.kill_switch_triggered:
        eng = f"{R}WS DISCONNECTED — REST only{E}"
    else:
        eng = f"{G}ACTIVE — scanning for trades{E}"

    strat_map = {
        config.REGIME_STRONG_SELL: "IRON_CONDOR or SHORT_STRADDLE",
        config.REGIME_MILD_SELL:   "CREDIT_SPREADS",
        config.REGIME_NEUTRAL:     "None (NEUTRAL — hold)",
        config.REGIME_BUY_VOL:     "BUTTERFLY or DEFENSIVE",
        config.REGIME_STRONG_BUY:  "LONG_STRADDLE or BACKSPREAD",
        config.REGIME_EVENT:       "LONG_STRANGLE",
    }
    intended = strat_map.get(regime, "Unknown")

    spot_ok = dm.spot is not None and dm.spot > 0
    vix_ok  = dm.vix  is not None and dm.vix  > 0
    iv_ok   = dm.iv_atm is not None and dm.iv_atm > 0
    rv_act  = dm.rv_20d is not None and dm.rv_20d > 0
    rv_est  = dm.get_estimated_rv()
    rv_ok   = rv_est is not None
    adx_ok  = dm.adx is not None
    ema_ok  = dm.ema_50 is not None
    chain_ok= len(dm.option_chain) > 0
    exp_ok  = dm._active_expiry is not None
    skew_ok = dm.skew is not None

    rv_disp = (
        f"{dm.rv_20d*100:.2f}%(actual)" if rv_act
        else f"{rv_est*100:.2f}%(est)"  if rv_est
        else "N/A"
    )

    ts = None
    if iv_ok and vix_ok:
        ts = dm.iv_atm - dm.vix / 100.0

    vix_gate  = dm.vix is None or dm.vix < config.VIX_SELL_VOL_MAX
    time_gate = is_entry
    reg_trade = regime in [
        config.REGIME_STRONG_SELL, config.REGIME_MILD_SELL,
        config.REGIME_BUY_VOL, config.REGIME_STRONG_BUY,
        config.REGIME_EVENT,
    ]
    pos_ok   = len(se.open_positions) < config.MAX_CONCURRENT_POSITIONS
    cool_ok  = (
        se._last_build_failure is None
        or (now - se._last_build_failure).total_seconds()
           >= config.BUILD_FAILURE_COOLDOWN_SEC
    )
    no_kill  = not se.kill_switch_active
    no_halt  = not se.daily_trading_halted

    all_ok = (
        is_tday and is_mkt and time_gate and reg_trade
        and vix_gate and pos_ok and cool_ok and no_kill and no_halt
    )

    ec      = sum(se._estimate_costs(p) for p in se.open_positions)
    net_pnl = se.daily_pnl - ec

    raw_vol   = re.vol_buffer[-1]   if re.vol_buffer   else 0.0
    raw_edge  = re.edge_buffer[-1]  if re.edge_buffer  else 0.0
    raw_trend = re.trend_buffer[-1] if re.trend_buffer else 0.0
    raw_flow  = re.flow_buffer[-1]  if re.flow_buffer  else 0.0

    vol_act   = re.confirmed_vol   != 0.0 or raw_vol   != 0.0
    edge_act  = re.confirmed_edge  != 0.0 or raw_edge  != 0.0
    trend_act = re.confirmed_trend != 0.0 or raw_trend != 0.0
    flow_act  = re.confirmed_flow  != 0.0 or raw_flow  != 0.0
    comp_act  = re.raw_composite   != 0.0

    warmup_done = re._refresh_count > re._warmup_required
    pbuf_full   = len(re.trend_buffer) >= config.PERSISTENCE_READINGS

    W = 72
    print()
    print("\u2501" * W)
    print(f" [{now_str}]  {eng}")
    print("\u2501" * W)

    print(f" {'MARKET DATA':<30}{'VALUE':<22}STATUS")
    print("\u2500" * W)
    print(f" {'Spot':<30}{str(f'{dm.spot:.2f}' if spot_ok else 'N/A'):<22}[{ps(spot_ok)}]")
    print(f" {'VIX':<30}{str(f'{dm.vix:.2f}' if vix_ok else 'N/A'):<22}[{ps(vix_ok)}]")
    print(f" {'IV_ATM':<30}{str(f'{dm.iv_atm*100:.2f}%' if iv_ok else 'N/A'):<22}[{ps(iv_ok)}]")
    print(f" {'RV_20d':<30}{rv_disp:<22}[{ps(rv_ok)}]{'  estimated' if not rv_act and rv_ok else ''}")
    print(f" {'ADX':<30}{str(f'{dm.adx:.2f}' if adx_ok else 'N/A'):<22}[{ps(adx_ok)}]")
    print(f" {'EMA_50':<30}{str(f'{dm.ema_50:.2f}' if ema_ok else 'N/A'):<22}[{ps(ema_ok)}]")
    print(f" {'Skew':<30}{str(f'{dm.skew:.4f}' if skew_ok else 'N/A'):<22}[{ps(skew_ok)}]")
    if ts is not None:
        neutral = abs(ts) <= config.TERM_SPREAD_CONTANGO
        sig     = "neutral(score=0)" if neutral else "elevated(score=-1)"
        print(f" {'Term Spread(iv-vix/100)':<30}{f'{ts:+.4f}':<22}[{ps(True, na=True)}]  {sig}")
    print(f" {'Option Chain':<30}{str(f'{len(dm.option_chain)} expiries') if chain_ok else 'N/A':<22}[{ps(chain_ok)}]")
    print(f" {'Active Expiry':<30}{str(dm._active_expiry or 'N/A'):<22}[{ps(exp_ok)}]")

    print("\u2500" * W)
    print(f" {'REGIME SCORES':<20}{'CONFIRMED':>12}  {'RAW':>8}  STATUS")
    print("\u2500" * W)
    print(f" {'Vol Score':<20}{re.confirmed_vol:>12.2f}  {raw_vol:>8.2f}  [{ps(vol_act)}]")
    print(f" {'Edge Score':<20}{re.confirmed_edge:>12.2f}  {raw_edge:>8.2f}  [{ps(edge_act)}]")
    print(f" {'Trend Score':<20}{re.confirmed_trend:>12.2f}  {raw_trend:>8.2f}  [{ps(trend_act)}]")
    print(f" {'Flow Score':<20}{re.confirmed_flow:>12.2f}  {raw_flow:>8.2f}  [{ps(flow_act)}]")
    print(f" {'Composite':<20}{re.raw_composite:>12.4f}  {'':>8}  [{ps(comp_act)}]")

    if not warmup_done:
        print(
            f" {'Status':<20}"
            f"WARMUP {re._refresh_count}/{re._warmup_required} "
            f"— composite forced 0"
        )
    elif not pbuf_full:
        need = config.PERSISTENCE_READINGS - len(re.trend_buffer)
        print(
            f" {'Status':<20}"
            f"FILLING {len(re.trend_buffer)}/{config.PERSISTENCE_READINGS} "
            f"— wait {need} refresh(es) (~{need*60}s)"
        )
    else:
        print(f" {'Status':<20}ACTIVE — all buffers confirmed")

    print(f" {'Regime':<20}{rc}{regime}{E}  (persist={re.persistence_count})")
    print(f" {'Intended Strategy':<20}{intended}")

    print("\u2500" * W)
    print(f" {'ENTRY GATES':<30}{'CONDITION':<26}STATUS")
    print("\u2500" * W)
    print(f" {'Trading day':<30}{str(is_tday):<26}[{ps(is_tday)}]")
    print(f" {'Market open':<30}{str(is_mkt):<26}[{ps(is_mkt)}]")
    print(f" {'Entry window':<30}{str(config.EXEC_START_TIME)+'-'+str(config.EXEC_END_TIME):<26}[{ps(time_gate)}]")
    print(f" {'Regime tradeable':<30}{regime:<26}[{ps(reg_trade)}]")
    print(f" {'VIX gate':<30}{str(f'VIX={dm.vix:.1f}<{config.VIX_SELL_VOL_MAX}' if vix_ok else 'N/A'):<26}[{ps(vix_gate)}]")
    print(f" {'Positions':<30}{str(len(se.open_positions))+'/'+str(config.MAX_CONCURRENT_POSITIONS):<26}[{ps(pos_ok)}]")
    print(f" {'Build cooldown':<30}{'OK' if cool_ok else 'COOLING':<26}[{ps(cool_ok)}]")
    print(f" {'Kill switch off':<30}{str(no_kill):<26}[{ps(no_kill)}]")
    print(f" {'Daily halt off':<30}{str(no_halt):<26}[{ps(no_halt)}]")

    print("\u2500" * W)
    if all_ok:
        print(f" TRADE DECISION: {G}ATTEMPTING {intended}{E}")
    elif not is_tday:
        print(f" TRADE DECISION: {Y}WAITING — Not a trading day{E}")
    elif not is_mkt:
        print(f" TRADE DECISION: {Y}WAITING — Market closed{E}")
    elif not time_gate:
        if now_time < config.EXEC_START_TIME:
            print(f" TRADE DECISION: {Y}WAITING — Entry opens at {config.EXEC_START_TIME}{E}")
        else:
            print(f" TRADE DECISION: {Y}NO TRADES — Entry window closed after {config.EXEC_END_TIME}{E}")
    elif not reg_trade:
        print(f" TRADE DECISION: {Y}NO TRADES — Regime={regime} not tradeable{E}")
    else:
        blocked = []
        if not vix_gate:  blocked.append(f"VIX={dm.vix:.1f}>={config.VIX_SELL_VOL_MAX}")
        if not pos_ok:    blocked.append(f"positions={len(se.open_positions)}/{config.MAX_CONCURRENT_POSITIONS}")
        if not cool_ok:   blocked.append("build_cooldown")
        if not no_kill:   blocked.append("kill_switch")
        if not no_halt:   blocked.append("daily_halted")
        print(f" TRADE DECISION: {R}BLOCKED — {', '.join(blocked) if blocked else 'unknown'}{E}")

    print("\u2500" * W)
    print(
        f" Positions: {len(se.open_positions)}  "
        f"Daily P&L: Rs{se.daily_pnl:>10,.2f}  "
        f"Net: Rs{net_pnl:>10,.2f}"
    )
    print(
        f" Capital:   Rs{se.current_capital:>12,.2f}  "
        f"Peak: Rs{se.peak_capital:>12,.2f}"
    )
    if se.open_positions:
        print("\u2500" * W)
        for pos in se.open_positions:
            pt = se._estimate_costs(pos)
            pn = pos.realized_pnl - pt
            pc = G if pn >= 0 else R
            print(
                f"  {pos.strategy_name:<26}"
                f"exp={pos.expiry_date}  "
                f"Net: {pc}Rs{pn:>8,.2f}{E}"
            )
    print("\u2501" * W)


def _generate_eod_report(
    se: StrategyEngine,
    dm: DataManager,
) -> None:
    """Generate EOD report. Guards against None RegimeEngine."""
    IST       = pytz.timezone(config.TZ)
    now_str   = datetime.now(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    total_trades = len(se.closed_positions)
    winning = [
        p for p in se.closed_positions
        if p.net_pnl > 0
    ]
    losing  = [
        p for p in se.closed_positions
        if p.net_pnl <= 0
    ]

    win_rate = (
        len(winning) / total_trades * 100
        if total_trades > 0 else 0.0
    )

    total_gross_pnl = sum(
        p.realized_pnl for p in se.closed_positions
    )
    total_tx_costs  = sum(
        p.transaction_costs for p in se.closed_positions
    )
    total_net_pnl   = sum(
        p.net_pnl for p in se.closed_positions
    )

    avg_win_net  = (
        float(np.mean([p.net_pnl for p in winning]))
        if winning else 0.0
    )
    avg_loss_net = (
        float(np.mean([p.net_pnl for p in losing]))
        if losing else 0.0
    )

    gross_wins   = sum(p.net_pnl for p in winning)
    gross_losses = abs(sum(p.net_pnl for p in losing))
    profit_factor = (
        gross_wins / gross_losses
        if gross_losses > 0
        else float("inf")
    )

    # Pre-compute to avoid f-string format error
    profit_factor_str = (
        f"{profit_factor:.2f}"
        if profit_factor != float("inf")
        else "inf"
    )

    drawdown = se.peak_capital - se.current_capital

    regime_str = (
        se.re.confirmed_regime
        if se.re is not None
        else "N/A"
    )
    active_expiry = dm._active_expiry or "N/A"

    report = (
        f"\n{'=' * 60}\n"
        f"END OF DAY REPORT — {now_str}\n"
        f"{'=' * 60}\n"
        f"Mode              : "
        f"{'PAPER' if config.PAPER_TRADING_MODE else 'LIVE'}\n"
        f"Total Trades      : {total_trades}\n"
        f"Winning Trades    : {len(winning)}\n"
        f"Losing Trades     : {len(losing)}\n"
        f"Win Rate          : {win_rate:.1f}%\n"
        f"Profit Factor     : {profit_factor_str}\n"
        f"{'─' * 60}\n"
        f"Gross P&L         : ₹{total_gross_pnl:,.2f}\n"
        f"Transaction Costs : ₹{total_tx_costs:,.2f}\n"
        f"Net P&L           : ₹{total_net_pnl:,.2f}\n"
        f"{'─' * 60}\n"
        f"Avg Win (net)     : ₹{avg_win_net:,.2f}\n"
        f"Avg Loss (net)    : ₹{avg_loss_net:,.2f}\n"
        f"{'─' * 60}\n"
        f"Capital           : ₹{se.current_capital:,.2f}\n"
        f"Peak Capital      : ₹{se.peak_capital:,.2f}\n"
        f"Drawdown          : ₹{drawdown:,.2f}\n"
        f"Open Positions    : {len(se.open_positions)}\n"
        f"Regime at EOD     : {regime_str}\n"
        f"Spot at EOD       : {dm.spot or 'N/A'}\n"
        f"VIX at EOD        : {dm.vix  or 'N/A'}\n"
        f"Active Expiry     : {active_expiry}\n"
        f"{'=' * 60}\n"
    )

    logger.info(report)

    try:
        audit_file = os.path.join(
            config.LOG_DIR,
            f"audit_log_{today_str}.log",
        )
        with open(
            audit_file, "a", encoding="utf-8"
        ) as f:
            f.write(report)
    except Exception as e:
        logger.warning(f"EOD report write failed: {e}")


async def main() -> None:
    """Main orchestration coroutine."""

    # STEP 1: Setup logging FIRST
    setup_logging()
    IST = pytz.timezone(config.TZ)

    logger.info("=" * 60)
    logger.info("NIFTY OPTIONS ALGO TRADING ENGINE STARTING")
    logger.info(
        f"Mode: "
        f"{'PAPER' if config.PAPER_TRADING_MODE else '*** LIVE ***'}"
    )
    logger.info(f"Capital: ₹{config.TOTAL_CAPITAL:,}")
    logger.info(
        f"Time: "
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    logger.info(
        f"Entry window: "
        f"{config.EXEC_START_TIME} - {config.EXEC_END_TIME}"
    )
    logger.info(
        f"NSE weekly expiry: weekday="
        f"{config.NSE_WEEKLY_EXPIRY_WEEKDAY} (Tuesday)"
    )
    logger.info("=" * 60)

    # STEP 2: Load access token
    access_token = _load_access_token()
    if access_token is None:
        logger.critical("Cannot load access token — exiting")
        sys.exit(1)

    # STEP 3: Pre-flight checks
    preflight_ok = await _run_preflight_checks(
        access_token
    )
    if not preflight_ok:
        logger.critical(
            "Pre-flight checks failed — exiting"
        )
        sys.exit(1)

    # STEP 4: Initialize components
    dm = DataManager(access_token, config.STATE_DB)
    await dm.initialize()

    re = RegimeEngine(dm, config.STATE_DB)
    re.load_buffers_from_sqlite()

    se = StrategyEngine(dm, re, config.STATE_DB)
    se._load_positions_from_sqlite()

    if not config.PAPER_TRADING_MODE:
        logger.info("STARTUP: Checking for stale orders...")
        try:
            stale = await se.startup_cancel_stale_orders()
            if stale > 0:
                logger.warning(
                    f"STARTUP: Cancelled {stale} "
                    f"stale orders"
                )
            else:
                logger.info(
                    "STARTUP: No stale orders — clean start"
                )
        except Exception as e:
            logger.error(
                f"STARTUP: Stale order check failed: {e}"
            )

    if not config.PAPER_TRADING_MODE:
        await se._reconcile_with_broker()

    # STEP 5: Register signal handlers
    shutdown_event = asyncio.Event()
    loop           = asyncio.get_running_loop()

    def _signal_handler(sig, frame):
        logger.warning(
            f"Signal {sig} received — "
            f"initiating graceful shutdown"
        )
        loop.call_soon_threadsafe(shutdown_event.set)

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # STEP 6: Wait for market open
    await _wait_for_market_open(shutdown_event)
    if shutdown_event.is_set():
        await _graceful_shutdown(se, dm, shutdown_event)
        return

    # STEP 7: Start WebSocket feed
    ws_task        = asyncio.create_task(
        dm.start_websocket()
    )
    ws_health_task = asyncio.create_task(
        dm.monitor_ws_health()
    )

    logger.info("Waiting for WebSocket data...")
    for _ in range(30):
        if dm.ws_connected:
            break
        await asyncio.sleep(1)
    if not dm.ws_connected:
        logger.warning(
            "WebSocket not connected within 30s "
            "— proceeding with REST"
        )

    # STEP 8: Initial data fetch
    await dm.fetch_spot_and_vix()

    if dm.spot is None or dm.vix is None:
        logger.critical(
            "Cannot fetch spot/VIX — aborting"
        )
        ws_task.cancel()
        ws_health_task.cancel()
        try:
            await asyncio.gather(
                ws_task, ws_health_task,
                return_exceptions=True,
            )
        except Exception:
            pass
        await _graceful_shutdown(se, dm, shutdown_event)
        return

    logger.info(
        f"Initial data: "
        f"spot={dm.spot:.2f} vix={dm.vix:.2f}"
    )

    # Find nearest expiry (scans forward, no weekday hardcode)
    initial_expiry = await _find_nearest_expiry(dm)
    expiries       = dm.get_available_expiries()

    if not expiries:
        logger.warning(
            "No expiries found — trying fallback dates"
        )
        for days_ahead in [7, 14, 21, 30]:
            exp_str = (
                date.today()
                + timedelta(days=days_ahead)
            ).strftime("%Y-%m-%d")
            await dm.fetch_option_chain(exp_str)
            await asyncio.sleep(0.3)
        expiries = dm.get_available_expiries()

    # Avoid double-fetching initial_expiry
    for expiry in expiries[:3]:
        if expiry != initial_expiry:
            await dm.fetch_option_chain(expiry)
            await asyncio.sleep(0.3)

    # LIVE FIX: use _last_trading_day() for to_date
    # Upstox returns 0 candles when to_date is weekend
    _last_td   = _last_trading_day()
    _from_date = (
        datetime.strptime(_last_td, "%Y-%m-%d").date()
        - timedelta(days=config.CANDLE_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")

    logger.info(
        f"Fetching candles: "
        f"{_from_date} → {_last_td} "
        f"(last trading day)"
    )

    await dm.fetch_historical_candles(
        config.INSTRUMENT_NIFTY,
        config.ADX_CANDLE_TIMEFRAME,   # "30minute"
        _from_date,
        _last_td,
    )

    await dm.fetch_historical_candles(
        config.INSTRUMENT_NIFTY,
        config.DAILY_CANDLE_TIMEFRAME,  # "day"
        _from_date,
        _last_td,
    )

    await dm.compute_realized_vol()
    await asyncio.to_thread(dm.compute_adx)
    await asyncio.to_thread(dm.compute_ema_slope)

    logger.info(
        f"Initial indicators: "
        f"rv={dm.rv_20d} adx={dm.adx} "
        f"ema50={dm.ema_50} iv_atm={dm.iv_atm} "
        f"active_expiry={dm._active_expiry}"
    )
    logger.info(
        "Initial data loaded — entering main loop"
    )

    # STEP 9: Main trading loop
    last_regime_refresh  = datetime.now(IST) - timedelta(
        seconds=config.REGIME_REFRESH_SECONDS
    )
    last_data_refresh    = datetime.now(IST) - timedelta(
        seconds=config.REGIME_REFRESH_SECONDS
    )
    last_candle_refresh  = datetime.now(IST) - timedelta(
        seconds=config.CANDLE_REFRESH_SECONDS
    )
    last_term_structure_check = datetime.now(IST) - timedelta(
        seconds=21600
    )
    last_console_update  = datetime.now(IST)
    last_heartbeat       = datetime.now(IST)
    last_trading_date    = date.today()

    eod_cancel_sweep_done = False
    eod_done_today        = False   # PATCH: EOD tight-loop guard

    cached_greeks:     Optional[dict] = None
    last_greeks_update = datetime.now(IST)

    # Regime refresh only runs after data refresh completes
    data_refresh_complete = True

    while not shutdown_event.is_set():
        try:
            now      = datetime.now(IST)
            now_time = now.time()

            # Kill switch checks
            if dm.kill_switch_triggered:
                logger.warning(
                    "WS disconnected — engine continues "
                    "with REST data, attempting reconnect"
                )
                dm.kill_switch_triggered = False
                asyncio.create_task(
                    dm._reconnect_websocket()
                )

            if se.kill_switch_active:
                logger.critical(
                    "Strategy kill switch ACTIVE — "
                    "no new trades, monitoring continues"
                )
                await se.cancel_all_open_orders(
                    context="KILL_SWITCH_SWEEP"
                )
                # Do NOT break — engine keeps running
                await asyncio.sleep(60)
                continue

            # Daily reset
            today = date.today()
            if today != last_trading_date:
                se.reset_daily_state()
                last_trading_date     = today
                eod_cancel_sweep_done = False
                eod_done_today        = False   # PATCH
                cached_greeks         = None
                data_refresh_complete = True
                logger.info(f"New trading day: {today}")

            # Market hours check — EOD
            # PATCH: guarded with eod_done_today to stop the
            # previous tight loop that re-ran _end_of_day() every
            # ~1s from 15:15 until midnight.
            if now_time >= config.TIME_EXIT_NORMAL:
                if not eod_done_today:
                    logger.info(
                        "Market closing time reached — EOD"
                    )
                    await _end_of_day(se, dm)
                    eod_done_today = True
                    logger.info(
                        "EOD complete — engine monitoring, "
                        "waiting for next trading day"
                    )
                await asyncio.sleep(60)
                continue

            # Expiry day check
            if _is_expiry_day(dm):
                if (
                    now_time >= config.TIME_EXIT_EXPIRY
                    and not eod_done_today
                ):
                    logger.info(
                        "Expiry day exit time reached "
                        f"({config.TIME_EXIT_EXPIRY})"
                    )
                    await _expiry_day_close_all(se)
                    await _end_of_day(se, dm)
                    eod_done_today = True   # PATCH
                    logger.info(
                        "Expiry day EOD complete — "
                        "engine monitoring"
                    )
                    await asyncio.sleep(300)
                    continue

            # Pre-EOD cancel sweep
            pre_eod_naive = datetime.combine(
                now.date(), config.TIME_EXIT_NORMAL
            )
            pre_eod_dt = (
                pytz.timezone(config.TZ).localize(
                    pre_eod_naive
                ) - timedelta(minutes=5)
            )
            if (
                not eod_cancel_sweep_done
                and now >= pre_eod_dt
                and not config.PAPER_TRADING_MODE
            ):
                logger.info(
                    "Pre-EOD cancel sweep "
                    "(5 min before close)"
                )
                await se.cancel_all_open_orders(
                    context="PRE_EOD_SWEEP"
                )
                eod_cancel_sweep_done = True

            # ─────────────────────────────────────────────────────
            # DATA REFRESH (every 60s)
            # ─────────────────────────────────────────────────────
            data_elapsed = (
                now - last_data_refresh
            ).total_seconds()

            if data_elapsed >= config.REGIME_REFRESH_SECONDS:
                logger.info("Starting data refresh cycle")
                data_refresh_complete = False

                try:
                    try:
                        await dm.fetch_spot_and_vix()
                    except Exception as e:
                        logger.error(
                            f"fetch_spot_and_vix: {e}"
                        )

                    current_expiries = (
                        dm.get_available_expiries()
                    )
                    if not current_expiries:
                        nearest = await _find_nearest_expiry(
                            dm
                        )
                        current_expiries = (
                            [nearest] if nearest else []
                        )

                    for expiry in current_expiries[:3]:
                        try:
                            await dm.fetch_option_chain(
                                expiry
                            )
                            await asyncio.sleep(0.2)
                        except Exception as e:
                            logger.error(
                                f"fetch_option_chain "
                                f"{expiry}: {e}"
                            )

                    # PATCH: proactively keep next expiry loaded
                    # so the engine never gets stuck with only a
                    # too-close-to-expire contract
                    try:
                        await _ensure_future_expiry_coverage(dm)
                    except Exception as e:
                        logger.error(
                            f"_ensure_future_expiry_coverage: {e}"
                        )

                    # Candles every 30 min only
                    candle_elapsed = (
                        now - last_candle_refresh
                    ).total_seconds()
                    if candle_elapsed >= (
                        config.CANDLE_REFRESH_SECONDS
                    ):
                        try:
                            # LIVE FIX: fresh dates each time
                            _last_td   = _last_trading_day()
                            _from_date = (
                                datetime.strptime(
                                    _last_td, "%Y-%m-%d"
                                ).date()
                                - timedelta(
                                    days=config.CANDLE_LOOKBACK_DAYS
                                )
                            ).strftime("%Y-%m-%d")

                            await dm.fetch_historical_candles(
                                config.INSTRUMENT_NIFTY,
                                config.ADX_CANDLE_TIMEFRAME,
                                _from_date,
                                _last_td,
                            )
                            await dm.fetch_historical_candles(
                                config.INSTRUMENT_NIFTY,
                                config.DAILY_CANDLE_TIMEFRAME,
                                _from_date,
                                _last_td,
                            )
                            last_candle_refresh = now
                        except Exception as e:
                            logger.error(
                                f"fetch_historical_candles: "
                                f"{e}"
                            )

                    # PATCH: periodically ensure a genuine
                    # 30-45 DTE expiry is loaded so forward_iv
                    # can be a real signal instead of always
                    # falling back to VIX/100.
                    term_elapsed = (
                        now - last_term_structure_check
                    ).total_seconds()
                    if term_elapsed >= 21600:
                        try:
                            await _ensure_term_structure_expiry(
                                dm
                            )
                        except Exception as e:
                            logger.error(
                                f"_ensure_term_structure_expiry: "
                                f"{e}"
                            )
                        last_term_structure_check = now

                    try:
                        await dm.compute_realized_vol()
                    except Exception as e:
                        logger.error(
                            f"compute_realized_vol: {e}"
                        )

                    try:
                        await asyncio.to_thread(
                            dm.compute_adx
                        )
                    except Exception as e:
                        logger.error(f"compute_adx: {e}")

                    try:
                        await asyncio.to_thread(
                            dm.compute_ema_slope
                        )
                    except Exception as e:
                        logger.error(
                            f"compute_ema_slope: {e}"
                        )

                    try:
                        await dm.fetch_oi_snapshot()
                    except Exception as e:
                        logger.error(
                            f"fetch_oi_snapshot: {e}"
                        )

                    try:
                        dm.compute_net_flow()
                    except Exception as e:
                        logger.error(
                            f"compute_net_flow: {e}"
                        )

                    try:
                        dm.compute_spread_ratio()
                    except Exception as e:
                        logger.error(
                            f"compute_spread_ratio: {e}"
                        )

                finally:
                    # LIVE FIX: always set in finally
                    # so regime can run even after errors
                    last_data_refresh     = now
                    data_refresh_complete = True
                    logger.info(
                        "Data refresh cycle complete"
                    )

            # ─────────────────────────────────────────────────────
            # REGIME REFRESH (sequenced after data refresh)
            # ─────────────────────────────────────────────────────
            regime_elapsed = (
                now - last_regime_refresh
            ).total_seconds()

            if (
                regime_elapsed
                >= config.REGIME_REFRESH_SECONDS
            ):
                logger.info("Starting regime refresh cycle")

                data_age = (
                    now - last_data_refresh
                ).total_seconds()

                if dm.spot is None or not dm.option_chain:
                    logger.warning(
                        "Regime refresh skipped: "
                        "incomplete data"
                    )
                elif not data_refresh_complete:
                    logger.warning(
                        "Regime refresh skipped: "
                        "data refresh not complete"
                    )
                elif data_age > (
                    config.REGIME_REFRESH_SECONDS * 2
                ):
                    logger.warning(
                        f"Regime refresh skipped: "
                        f"data age={data_age:.0f}s "
                        f"(too stale)"
                    )
                else:
                    try:
                        if now_time < (
                            config.REGIME_FREEZE_TIME
                        ):
                            regime = await re.refresh()
                            logger.info(
                                f"Regime: {regime} "
                                f"composite="
                                f"{re.raw_composite:.4f}"
                            )
                        else:
                            logger.info(
                                f"Regime frozen at "
                                f"{config.REGIME_FREEZE_TIME}"
                                f" — current: "
                                f"{re.confirmed_regime}"
                            )
                    except Exception as e:
                        logger.error(
                            f"Regime refresh error: {e}"
                        )

                    try:
                        await se.run_cycle()
                    except Exception as e:
                        logger.error(
                            f"Strategy cycle error: {e}"
                        )
                        logger.error(
                            traceback.format_exc()
                        )

                    try:
                        re.save_buffers_to_sqlite()
                    except Exception as e:
                        logger.warning(
                            f"save_buffers error: {e}"
                        )

                    if len(se.closed_positions) > 500:
                        se.closed_positions = (
                            se.closed_positions[-500:]
                        )

                last_regime_refresh = now
                logger.info(
                    "Regime refresh cycle complete"
                )

            # ─────────────────────────────────────────────────────
            # HEARTBEAT
            # ─────────────────────────────────────────────────────
            hb_elapsed = (
                now - last_heartbeat
            ).total_seconds()
            if hb_elapsed >= config.HEARTBEAT_INTERVAL_SEC:
                try:
                    dm.save_state_to_sqlite({
                        "timestamp":       now.isoformat(),
                        "spot":            dm.spot,
                        "vix":             dm.vix,
                        "iv_atm":          dm.iv_atm,
                        "rv_20d":          dm.rv_20d,
                        "skew":            dm.skew,
                        "adx":             dm.adx,
                        "ema_50":          dm.ema_50,
                        "composite_score": re.raw_composite,
                        "regime":          re.confirmed_regime,
                    })
                except Exception as e:
                    logger.warning(
                        f"Heartbeat save error: {e}"
                    )
                last_heartbeat = now

            # ─────────────────────────────────────────────────────
            # CONSOLE DISPLAY
            # ─────────────────────────────────────────────────────
            greeks_age = (
                now - last_greeks_update
            ).total_seconds()
            if greeks_age >= 30:
                cached_greeks      = (
                    se._get_portfolio_greeks()
                )
                last_greeks_update = now

            console_elapsed = (
                now - last_console_update
            ).total_seconds()
            if (
                console_elapsed
                >= config.CONSOLE_REFRESH_SECONDS
            ):
                try:
                    _display_console(
                        dm, re, se, cached_greeks
                    )
                except Exception as e:
                    logger.warning(
                        f"Console display error: {e}"
                    )
                last_console_update = now

            await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("Main loop cancelled")
            break
        except Exception as e:
            logger.error(
                f"Main loop unhandled error: {e}"
            )
            logger.error(traceback.format_exc())
            await asyncio.sleep(5)
            continue

    # STEP 10: Graceful shutdown
    logger.info(
        "Main loop exited — starting shutdown sequence"
    )

    for task in [ws_task, ws_health_task]:
        try:
            task.cancel()
            await asyncio.gather(
                task, return_exceptions=True
            )
        except Exception:
            pass

    await _graceful_shutdown(se, dm, shutdown_event)


if __name__ == "__main__":
    # Windows ProactorEventLoop compatibility
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()
        )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(
            "\nKeyboardInterrupt — shutdown complete"
        )
    except SystemExit as e:
        sys.exit(e.code)
    except BaseException as e:
        print(f"FATAL ERROR: {e}")
        print(traceback.format_exc())
        sys.exit(1)