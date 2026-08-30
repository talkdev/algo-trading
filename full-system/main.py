# ============ FILE: main.py ============
"""
Orchestrator — starts and manages all components.
Handles pre-flight checks, main trading loop, graceful shutdown,
and real-time console display.

FIXES IN THIS VERSION:
  CRITICAL FIX 1: Cancel sweep before EOD procedures
  CRITICAL FIX 2: Cancel sweep on graceful shutdown
  CRITICAL FIX 3: Startup stale order cancellation
  CRITICAL FIX 4: Cancel sweep on expiry day close
  CRITICAL FIX 5: Cancel sweep on kill switch
  HIGH FIX 6:     Net PnL (after transaction costs) in EOD report
  HIGH FIX 7:     Net PnL in console display
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
from typing import Optional
import pytz
import config
from data_manager import DataManager
from regime_engine import RegimeEngine
from strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)


def setup_logging() -> logging.Logger:
    """Configure file and console logging with IST timestamps."""
    IST = pytz.timezone(config.TZ)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
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
    module_logger.info(f"Logging initialized: {audit_file}")
    return module_logger


def _load_access_token() -> Optional[str]:
    """Load Upstox access token from env.txt file."""
    try:
        with open(config.TOKEN_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("UPSTOX_ACCESS_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    if token:
                        logger.info(
                            "Access token loaded successfully"
                        )
                        return token
        logger.critical(
            f"Token not found in {config.TOKEN_FILE} — "
            f"ensure line: UPSTOX_ACCESS_TOKEN=your_token"
        )
        return None
    except FileNotFoundError:
        logger.critical(
            f"env.txt not found at {config.TOKEN_FILE} — "
            f"create file with UPSTOX_ACCESS_TOKEN=your_token"
        )
        return None
    except Exception as e:
        logger.critical(f"Token load error: {e}")
        return None


async def _run_preflight_checks(
    access_token: str,
) -> bool:
    """Run all pre-flight validation checks."""
    checks_passed = True
    IST = pytz.timezone(config.TZ)

    # CHECK 1 — Trading day
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    if today_str in config.NSE_MARKET_HOLIDAYS:
        if not config.ALLOW_NON_TRADING_DAY_RUN:
            logger.critical(
                f"Today {today_str} is an NSE holiday — "
                f"set ALLOW_NON_TRADING_DAY_RUN=True to override"
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
                    f"Weekend ({today.strftime('%A')}) — "
                    f"market closed"
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
            client = ntplib.NTPClient()
            response = client.request(
                config.NTP_SERVER, version=3
            )
            offset = abs(response.offset)
            if offset > config.NTP_MAX_OFFSET_SEC:
                logger.critical(
                    f"Clock offset {offset:.3f}s > "
                    f"max {config.NTP_MAX_OFFSET_SEC}s — "
                    f"sync system clock before trading"
                )
                return False
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
                    "Authorization": f"Bearer {access_token}"
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
                        "Token expired or invalid — "
                        "generate new token from Upstox"
                    )
                    return False
                else:
                    logger.warning(
                        f"Profile check returned {resp.status}"
                        f" — proceeding with caution"
                    )
    except Exception as e:
        logger.warning(
            f"Token validation error: {e} — proceeding"
        )

    # CHECK 4 — Execution window check
    now_time = datetime.now(IST).time()
    if (
        now_time > config.EXEC_END_TIME
        and now_time < config.MARKET_CLOSE
    ):
        logger.warning(
            f"Entry window {config.EXEC_START_TIME}-"
            f"{config.EXEC_END_TIME} already closed "
            f"for today — monitoring only"
        )

    # CHECK 5 — Log directory
    if not os.path.exists(config.LOG_DIR):
        os.makedirs(config.LOG_DIR, exist_ok=True)
        logger.info(
            f"Created log directory: {config.LOG_DIR}"
        )

    # CHECK 6 — Holiday calendar review date
    days_since_review = (
        today - config.HOLIDAY_CALENDAR_REVIEWED_ON
    ).days
    if days_since_review > 180:
        logger.warning(
            f"Holiday calendar last reviewed "
            f"{days_since_review} days ago — "
            f"please update NSE_MARKET_HOLIDAYS in config.py"
        )

    # CHECK 7 — High impact events today
    if today_str in config.HIGH_IMPACT_EVENTS:
        event_name = config.HIGH_IMPACT_EVENTS[today_str]
        logger.warning(
            f"HIGH IMPACT EVENT TODAY: {event_name} — "
            f"EVENT_HEDGE regime may activate"
        )

    logger.info(
        f"Pre-flight checks completed — "
        f"mode={'PAPER' if config.PAPER_TRADING_MODE else 'LIVE'}"
    )
    return checks_passed


async def _wait_for_market_open(
    shutdown_event: asyncio.Event,
) -> None:
    """Wait until market opens at MARKET_OPEN time."""
    IST = pytz.timezone(config.TZ)

    while not shutdown_event.is_set():
        now = datetime.now(IST)
        now_time = now.time()

        if now_time >= config.MARKET_OPEN:
            logger.info(
                f"Market open — starting engine "
                f"(time={now_time})"
            )
            return

        market_open_dt = datetime.combine(
            now.date(), config.MARKET_OPEN
        ).replace(tzinfo=IST)
        wait_sec = (market_open_dt - now).total_seconds()

        if wait_sec > 0:
            logger.info(
                f"Waiting {wait_sec:.0f}s for market open "
                f"at {config.MARKET_OPEN}"
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


async def _end_of_day(
    se: StrategyEngine,
    dm: DataManager,
) -> None:
    """
    Execute end-of-day procedures.
    CRITICAL FIX 1: Cancel all open orders BEFORE closing positions.
    """
    IST = pytz.timezone(config.TZ)
    logger.info("=" * 60)
    logger.info("END OF DAY PROCEDURES STARTING")
    logger.info("=" * 60)

    # CRITICAL FIX 1: Cancel all open orders first
    # This ensures no limit orders fill after we start closing
    logger.info("EOD: Running cancel sweep before position close")
    cancelled = await se.cancel_all_open_orders(
        context="EOD_CANCEL_SWEEP"
    )
    if cancelled > 0:
        logger.info(
            f"EOD: Cancelled {cancelled} open orders "
            f"before position close"
        )
    # Allow broker to process cancellations
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
        ):
            should_close = False
            logger.info(
                f"Keeping overnight: {position.strategy_name} "
                f"trade_id={position.trade_id[:8]} dte={dte}"
            )

        if should_close:
            await se._close_position(
                position, config.EXIT_REASONS["EOD"]
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
    Force close all expiring positions on expiry day.
    CRITICAL FIX 4: Cancel open orders before force close.
    """
    logger.info(
        "EXPIRY DAY: Force closing all expiring positions"
    )

    # CRITICAL FIX 4: Cancel all open orders first
    logger.info(
        "EXPIRY DAY: Running cancel sweep before force close"
    )
    cancelled = await se.cancel_all_open_orders(
        context="EXPIRY_CANCEL_SWEEP"
    )
    if cancelled > 0:
        logger.info(
            f"EXPIRY DAY: Cancelled {cancelled} open orders"
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
                f"Expiry closed: {position.strategy_name} "
                f"trade_id={position.trade_id[:8]}"
            )

    logger.info("Expiry day close complete")


async def _graceful_shutdown(
    se: StrategyEngine,
    dm: DataManager,
    shutdown_event: asyncio.Event,
) -> None:
    """
    Execute graceful shutdown with position square-off.
    CRITICAL FIX 2: Cancel all open orders BEFORE closing positions.
    """
    IST = pytz.timezone(config.TZ)
    logger.info("=" * 60)
    logger.info("GRACEFUL SHUTDOWN INITIATED")
    logger.info("=" * 60)

    # CRITICAL FIX 2: Cancel ALL open orders first
    # This is the most important step — nothing should be
    # left open at the broker after shutdown
    logger.info(
        "SHUTDOWN: Running cancel sweep before position close"
    )
    try:
        cancelled = await se.cancel_all_open_orders(
            context="SHUTDOWN_CANCEL_SWEEP"
        )
        if cancelled > 0:
            logger.warning(
                f"SHUTDOWN: Cancelled {cancelled} open orders "
                f"— these would have been orphaned"
            )
        else:
            logger.info(
                "SHUTDOWN: No open orders to cancel — clean"
            )
        # Allow broker to process cancellations
        await asyncio.sleep(1.0)
    except Exception as e:
        logger.error(
            f"SHUTDOWN: Cancel sweep failed: {e} — "
            f"proceeding with position close anyway"
        )

    # Close positions
    if not config.PAPER_TRADING_MODE:
        logger.info(
            "Live mode: squaring off all open positions"
        )
        for position in list(se.open_positions):
            try:
                await se._close_position(
                    position,
                    config.EXIT_REASONS["MANUAL"],
                    use_market=True,
                )
            except Exception as e:
                logger.error(
                    f"Shutdown close error for "
                    f"{position.trade_id[:8]}: {e}"
                )
    else:
        logger.info(
            "Paper mode: logging open positions at shutdown"
        )
        for position in se.open_positions:
            logger.info(
                f"Open at shutdown: {position.strategy_name} "
                f"trade_id={position.trade_id[:8]} "
                f"gross_pnl=₹{position.realized_pnl:,.2f} "
                f"net_pnl=₹{position.net_pnl:,.2f}"
            )

    # Close WebSocket
    if dm.ws is not None and dm.ws_connected:
        try:
            await dm.ws.close()
            logger.info("WebSocket closed")
        except Exception as e:
            logger.warning(f"WebSocket close error: {e}")

    # Close aiohttp session
    if dm.session is not None and not dm.session.closed:
        try:
            await dm.session.close()
            logger.info("HTTP session closed")
        except Exception as e:
            logger.warning(f"HTTP session close error: {e}")

    # Final state save
    try:
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
    except Exception as e:
        logger.warning(f"Final state save error: {e}")

    _generate_eod_report(se, dm)

    logger.info("Shutdown complete")
    shutdown_event.set()


def _display_console(
    dm: DataManager,
    re: RegimeEngine,
    se: StrategyEngine,
) -> None:
    """
    Render real-time console display.
    HIGH FIX 7: Shows net PnL (after transaction costs).
    """
    IST = pytz.timezone(config.TZ)
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    COLORS = {
        config.REGIME_STRONG_SELL: "\033[92m",
        config.REGIME_MILD_SELL:   "\033[32m",
        config.REGIME_NEUTRAL:     "\033[93m",
        config.REGIME_BUY_VOL:     "\033[91m",
        config.REGIME_STRONG_BUY:  "\033[31m",
        config.REGIME_EVENT:       "\033[95m",
    }
    RESET = "\033[0m"
    regime = re.confirmed_regime
    color = COLORS.get(regime, "")

    greeks = se._get_portfolio_greeks()

    spot_str = (
        f"{dm.spot:>10.2f}" if dm.spot else "       N/A"
    )
    vix_str = (
        f"{dm.vix:>6.2f}" if dm.vix else "   N/A"
    )
    iv_atm_str = (
        f"{dm.iv_atm:>10.4f}" if dm.iv_atm else "       N/A"
    )
    rv_str = (
        f"{dm.rv_20d:>6.4f}" if dm.rv_20d else "   N/A"
    )
    adx_str = (
        f"{dm.adx:>10.2f}" if dm.adx else "       N/A"
    )
    skew_str = (
        f"{dm.skew:>6.4f}" if dm.skew else "   N/A"
    )

    try:
        print("\033[2J\033[H", end="", flush=True)
    except Exception:
        pass

    print("=" * 65)
    print(
        f" NIFTY OPTIONS ALGO — "
        f"{'PAPER TRADING' if config.PAPER_TRADING_MODE else '*** LIVE TRADING ***'}"
    )
    print("=" * 65)
    print(f" Time    : {now_str}")
    print(f" Spot    : {spot_str}  VIX  : {vix_str}")
    print(f" IV_ATM  : {iv_atm_str}  RV   : {rv_str}")
    print(f" ADX     : {adx_str}  Skew : {skew_str}")
    print("-" * 65)
    print(
        f" Vol     : {re.confirmed_vol:>+6.2f}  "
        f"Edge : {re.confirmed_edge:>+6.2f}  "
        f"Trend: {re.confirmed_trend:>+6.2f}  "
        f"Flow : {re.confirmed_flow:>+6.2f}"
    )
    print(
        f" Composite Score : {re.raw_composite:>+8.4f}"
    )
    print(
        f" Regime  : {color}{regime}{RESET}  "
        f"(persist={re.persistence_count})"
    )
    print(
        f" Action  : {re.get_regime_action_description()}"
    )
    print("-" * 65)

    # HIGH FIX 7: Show both gross and net PnL
    # Calculate estimated transaction costs for open positions
    estimated_costs = sum(
        se._estimate_costs(p) for p in se.open_positions
    )
    net_daily_pnl = se.daily_pnl - estimated_costs

    print(
        f" Positions: {len(se.open_positions)}  "
        f"Daily P&L (gross): ₹{se.daily_pnl:>10,.2f}"
    )
    print(
        f" Est. Tx Costs:    ₹{estimated_costs:>10,.2f}  "
        f"Net P&L: ₹{net_daily_pnl:>10,.2f}"
    )
    print(
        f" Capital (net): ₹{se.current_capital:>12,.2f}"
    )
    print(
        f" Delta  : {greeks['delta']:>8.3f}  "
        f"Gamma: {greeks['gamma']:>10.5f}"
    )
    print(
        f" Vega   : ₹{greeks['vega']:>8,.0f}  "
        f"Theta: ₹{greeks['theta']:>8,.0f}/day"
    )
    print("-" * 65)

    if se.open_positions:
        print(" OPEN POSITIONS:")
        for pos in se.open_positions:
            # HIGH FIX 7: Show net PnL per position
            pos_tx_costs = se._estimate_costs(pos)
            pos_net_pnl = pos.realized_pnl - pos_tx_costs
            pnl_color = (
                "\033[92m" if pos_net_pnl >= 0
                else "\033[91m"
            )
            print(
                f"  {pos.strategy_name:<25} "
                f"Entry: {pos.entry_spot:>8.2f}  "
                f"Net P&L: {pnl_color}"
                f"₹{pos_net_pnl:>8,.2f}{RESET}"
            )
    else:
        print(" No open positions")

    print("-" * 65)

    # Session order registry status
    session_order_count = len(se._session_orders)
    if session_order_count > 0:
        filled = sum(
            1 for o in se._session_orders.values()
            if o.get("filled")
        )
        cancelled = sum(
            1 for o in se._session_orders.values()
            if o.get("cancelled")
        )
        open_orders = (
            session_order_count - filled - cancelled
        )
        print(
            f" Orders this session: {session_order_count} "
            f"(filled={filled} cancelled={cancelled} "
            f"open={open_orders})"
        )

    alerts = []
    if se.daily_trading_halted:
        alerts.append(
            " \033[91m⚠ DAILY TRADING HALTED (CB L2)\033[0m"
        )
    if se.kill_switch_active:
        alerts.append(
            " \033[91m⚠ KILL SWITCH ACTIVE (CB L4)\033[0m"
        )
    if dm.kill_switch_triggered:
        alerts.append(
            " \033[91m⚠ WEBSOCKET KILL SWITCH\033[0m"
        )
    if se.cooling_period_end:
        IST = pytz.timezone(config.TZ)
        if datetime.now(IST) < se.cooling_period_end:
            alerts.append(
                f" \033[93m⚠ COOLING PERIOD ACTIVE until "
                f"{se.cooling_period_end.strftime('%H:%M:%S')}"
                f"\033[0m"
            )

    for alert in alerts:
        print(alert)

    if not alerts:
        print(" System: \033[92mOPERATIONAL\033[0m")

    print("=" * 65)


def _generate_eod_report(
    se: StrategyEngine,
    dm: DataManager,
) -> None:
    """
    Generate and log end-of-day performance report.
    HIGH FIX 6: Reports both gross and net PnL.
    """
    IST = pytz.timezone(config.TZ)
    now_str = datetime.now(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    total_trades = len(se.closed_positions)
    winning = [
        p for p in se.closed_positions
        if p.net_pnl > 0
    ]
    losing = [
        p for p in se.closed_positions
        if p.net_pnl <= 0
    ]

    win_rate = (
        len(winning) / total_trades * 100
        if total_trades > 0 else 0.0
    )

    # HIGH FIX 6: Use net_pnl for all calculations
    total_gross_pnl = sum(
        p.realized_pnl for p in se.closed_positions
    )
    total_tx_costs = sum(
        p.transaction_costs for p in se.closed_positions
    )
    total_net_pnl = sum(
        p.net_pnl for p in se.closed_positions
    )

    avg_win_net = (
        float(np.mean([p.net_pnl for p in winning]))
        if winning else 0.0
    )
    avg_loss_net = (
        float(np.mean([p.net_pnl for p in losing]))
        if losing else 0.0
    )

    # Profit factor using net PnL
    gross_wins = sum(p.net_pnl for p in winning)
    gross_losses = abs(sum(p.net_pnl for p in losing))
    profit_factor = (
        gross_wins / gross_losses
        if gross_losses > 0 else float("inf")
    )

    drawdown = se.peak_capital - se.current_capital

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
        f"Profit Factor     : "
        f"{profit_factor:.2f if profit_factor != float('inf') else 'inf'}\n"
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
        f"Regime at EOD     : {se.re.confirmed_regime}\n"
        f"Spot at EOD       : {dm.spot or 'N/A'}\n"
        f"VIX at EOD        : {dm.vix or 'N/A'}\n"
        f"{'=' * 60}\n"
    )

    logger.info(report)

    try:
        with open(
            config.AUDIT_CSV, "a", encoding="utf-8"
        ) as f:
            f.write(report)
    except Exception as e:
        logger.warning(f"EOD report write failed: {e}")


def _is_expiry_day() -> bool:
    """Check if today is Nifty expiry day (Thursday)."""
    return date.today().weekday() == 3


async def main() -> None:
    """Main orchestration coroutine."""
    # STEP 1 — Setup logging
    setup_logging()
    IST = pytz.timezone(config.TZ)

    logger.info("=" * 60)
    logger.info("NIFTY OPTIONS ALGO TRADING ENGINE STARTING")
    logger.info(
        f"Mode: "
        f"{'PAPER TRADING' if config.PAPER_TRADING_MODE else '*** LIVE TRADING ***'}"
    )
    logger.info(f"Capital: ₹{config.TOTAL_CAPITAL:,}")
    logger.info(
        f"Time: "
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    logger.info("FIXES ACTIVE:")
    logger.info(
        "  CRITICAL: Idempotent orders (tag-based dedup)"
    )
    logger.info(
        "  CRITICAL: Cancel sweep (EOD + shutdown + expiry)"
    )
    logger.info(
        "  CRITICAL: Startup stale order cancellation"
    )
    logger.info(
        "  HIGH: Transaction costs in PnL reporting"
    )
    logger.info("=" * 60)

    # STEP 2 — Load access token
    access_token = _load_access_token()
    if access_token is None:
        logger.critical("Cannot load access token — exiting")
        sys.exit(1)

    # STEP 3 — Pre-flight checks
    preflight_ok = await _run_preflight_checks(access_token)
    if not preflight_ok:
        logger.critical(
            "Pre-flight checks failed — exiting"
        )
        sys.exit(1)

    # STEP 4 — Initialize components
    dm = DataManager(access_token, config.STATE_DB)
    await dm.initialize()

    re = RegimeEngine(dm, config.STATE_DB)
    re.load_buffers_from_sqlite()

    se = StrategyEngine(dm, re, config.STATE_DB)
    se._load_positions_from_sqlite()

    # CRITICAL FIX 3: Cancel stale orders from previous session
    # This must happen BEFORE any new orders are placed
    if not config.PAPER_TRADING_MODE:
        logger.info(
            "STARTUP: Checking for stale orders from "
            "previous session..."
        )
        try:
            stale_cancelled = (
                await se.startup_cancel_stale_orders()
            )
            if stale_cancelled > 0:
                logger.warning(
                    f"STARTUP: Cancelled {stale_cancelled} "
                    f"stale orders from previous session — "
                    f"these would have been orphaned"
                )
            else:
                logger.info(
                    "STARTUP: No stale orders found — "
                    "clean start"
                )
        except Exception as e:
            logger.error(
                f"STARTUP: Stale order check failed: {e} — "
                f"proceeding (manual check recommended)"
            )

    # Broker reconciliation on startup (live mode)
    if not config.PAPER_TRADING_MODE:
        await se._reconcile_with_broker()

    # STEP 5 — Register signal handlers
    shutdown_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.warning(
            f"Signal {sig} received — "
            f"initiating graceful shutdown"
        )
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # STEP 6 — Wait for market open
    await _wait_for_market_open(shutdown_event)
    if shutdown_event.is_set():
        await _graceful_shutdown(se, dm, shutdown_event)
        return

    # STEP 7 — Start WebSocket feed
    ws_task = asyncio.create_task(dm.start_websocket())
    ws_health_task = asyncio.create_task(
        dm.monitor_ws_health()
    )

    logger.info("Waiting for WebSocket data...")
    await asyncio.sleep(3)

    # STEP 8 — Initial data fetch
    await dm.fetch_spot_and_vix()

    if dm.spot is None or dm.vix is None:
        logger.critical(
            "Cannot fetch spot/VIX — aborting"
        )
        ws_task.cancel()
        ws_health_task.cancel()
        await _graceful_shutdown(se, dm, shutdown_event)
        return

    logger.info(
        f"Initial data: spot={dm.spot:.2f} vix={dm.vix:.2f}"
    )

    today_str = date.today().strftime("%Y-%m-%d")
    from_date = (
        date.today() - timedelta(days=30)
    ).strftime("%Y-%m-%d")

    await dm.fetch_option_chain(
        (date.today() + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )
    )
    expiries = dm.get_available_expiries()

    if not expiries:
        logger.warning(
            "No expiries found — fetching with default dates"
        )
        for days_ahead in [7, 14, 30, 45]:
            exp_str = (
                date.today() + timedelta(days=days_ahead)
            ).strftime("%Y-%m-%d")
            await dm.fetch_option_chain(exp_str)
            await asyncio.sleep(0.3)
        expiries = dm.get_available_expiries()

    for expiry in expiries[:3]:
        await dm.fetch_option_chain(expiry)
        await asyncio.sleep(0.3)

    await dm.fetch_historical_candles(
        config.INSTRUMENT_NIFTY,
        config.ADX_CANDLE_TIMEFRAME,
        from_date,
        today_str,
    )

    await dm.compute_realized_vol()
    dm.compute_adx()
    dm.compute_ema_slope()

    logger.info(
        f"Initial indicators: rv={dm.rv_20d} "
        f"adx={dm.adx} ema50={dm.ema_50} "
        f"iv_atm={dm.iv_atm}"
    )
    logger.info(
        "Initial data loaded — entering main loop"
    )

    # STEP 9 — Main trading loop
    last_regime_refresh = datetime.now(IST) - timedelta(
        seconds=config.REGIME_REFRESH_SECONDS
    )
    last_data_refresh = datetime.now(IST) - timedelta(
        seconds=config.REGIME_REFRESH_SECONDS
    )
    last_console_update = datetime.now(IST)
    last_heartbeat = datetime.now(IST)
    last_trading_date = date.today()

    # Track if cancel sweep has been run today
    eod_cancel_sweep_done = False

    while not shutdown_event.is_set():
        try:
            now = datetime.now(IST)
            now_time = now.time()

            # Check WebSocket kill switch
            if dm.kill_switch_triggered:
                logger.critical(
                    "WebSocket kill switch triggered — "
                    "initiating graceful shutdown"
                )
                break

            # Check strategy engine kill switch
            if se.kill_switch_active:
                logger.critical(
                    "Strategy engine kill switch active — "
                    "initiating graceful shutdown"
                )
                # CRITICAL FIX 5: Cancel sweep on kill switch
                await se.cancel_all_open_orders(
                    context="KILL_SWITCH_SWEEP"
                )
                break

            # Daily reset check
            today = date.today()
            if today != last_trading_date:
                se.reset_daily_state()
                last_trading_date = today
                eod_cancel_sweep_done = False
                if today.weekday() == 0:
                    se.reset_weekly_state()
                logger.info(f"New trading day: {today}")

            # Market hours check — EOD
            if now_time >= config.TIME_EXIT_NORMAL:
                logger.info(
                    f"Market closing time reached "
                    f"({config.TIME_EXIT_NORMAL}) — "
                    f"initiating EOD procedures"
                )
                await _end_of_day(se, dm)
                break

            # Expiry day check
            if _is_expiry_day():
                if now_time >= config.TIME_EXIT_EXPIRY:
                    logger.info(
                        f"Expiry day exit time reached "
                        f"({config.TIME_EXIT_EXPIRY})"
                    )
                    await _expiry_day_close_all(se)
                    await _end_of_day(se, dm)
                    break

            # CRITICAL FIX 1: Pre-EOD cancel sweep
            # Run 5 minutes before TIME_EXIT_NORMAL
            # to cancel any stale limit orders before
            # we start closing positions
            pre_eod_time = datetime.combine(
                now.date(), config.TIME_EXIT_NORMAL
            ).replace(tzinfo=IST) - timedelta(minutes=5)
            if (
                not eod_cancel_sweep_done
                and now >= pre_eod_time
                and not config.PAPER_TRADING_MODE
            ):
                logger.info(
                    "Pre-EOD cancel sweep (5 min before close)"
                )
                await se.cancel_all_open_orders(
                    context="PRE_EOD_SWEEP"
                )
                eod_cancel_sweep_done = True

            # ─────────────────────────────────────────
            # DATA REFRESH (every 5 minutes)
            # ─────────────────────────────────────────
            data_elapsed = (
                now - last_data_refresh
            ).total_seconds()
            if data_elapsed >= config.REGIME_REFRESH_SECONDS:
                logger.info("Starting data refresh cycle")

                try:
                    await dm.fetch_spot_and_vix()
                except Exception as e:
                    logger.error(
                        f"fetch_spot_and_vix error: {e}"
                    )

                current_expiries = dm.get_available_expiries()
                if not current_expiries:
                    current_expiries = expiries

                for expiry in current_expiries[:3]:
                    try:
                        await dm.fetch_option_chain(expiry)
                        await asyncio.sleep(0.2)
                    except Exception as e:
                        logger.error(
                            f"fetch_option_chain error "
                            f"for {expiry}: {e}"
                        )

                try:
                    await dm.fetch_historical_candles(
                        config.INSTRUMENT_NIFTY,
                        config.ADX_CANDLE_TIMEFRAME,
                        from_date,
                        today_str,
                    )
                except Exception as e:
                    logger.error(
                        f"fetch_historical_candles error: {e}"
                    )

                try:
                    await dm.compute_realized_vol()
                except Exception as e:
                    logger.error(
                        f"compute_realized_vol error: {e}"
                    )

                try:
                    dm.compute_adx()
                except Exception as e:
                    logger.error(f"compute_adx error: {e}")

                try:
                    dm.compute_ema_slope()
                except Exception as e:
                    logger.error(
                        f"compute_ema_slope error: {e}"
                    )

                try:
                    await dm.fetch_oi_snapshot()
                except Exception as e:
                    logger.error(
                        f"fetch_oi_snapshot error: {e}"
                    )

                try:
                    dm.compute_net_flow()
                except Exception as e:
                    logger.error(
                        f"compute_net_flow error: {e}"
                    )

                try:
                    dm.compute_spread_ratio()
                except Exception as e:
                    logger.error(
                        f"compute_spread_ratio error: {e}"
                    )

                last_data_refresh = now
                logger.info("Data refresh cycle complete")

            # ─────────────────────────────────────────
            # REGIME REFRESH (every 5 minutes)
            # ─────────────────────────────────────────
            regime_elapsed = (
                now - last_regime_refresh
            ).total_seconds()
            if (
                regime_elapsed
                >= config.REGIME_REFRESH_SECONDS
            ):
                logger.info("Starting regime refresh cycle")

                try:
                    if now_time < config.REGIME_FREEZE_TIME:
                        regime = await re.refresh()
                        logger.info(
                            f"Regime refreshed: {regime} "
                            f"composite="
                            f"{re.raw_composite:.4f}"
                        )
                    else:
                        logger.info(
                            f"Regime frozen at "
                            f"{config.REGIME_FREEZE_TIME} "
                            f"— current: "
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
                    logger.error(traceback.format_exc())

                try:
                    re.save_buffers_to_sqlite()
                except Exception as e:
                    logger.warning(
                        f"save_buffers_to_sqlite error: {e}"
                    )

                last_regime_refresh = now
                logger.info(
                    "Regime refresh cycle complete"
                )

            # ─────────────────────────────────────────
            # HEARTBEAT (every 10 seconds)
            # ─────────────────────────────────────────
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

            # ─────────────────────────────────────────
            # CONSOLE DISPLAY (every 5 seconds)
            # ─────────────────────────────────────────
            console_elapsed = (
                now - last_console_update
            ).total_seconds()
            if (
                console_elapsed
                >= config.CONSOLE_REFRESH_SECONDS
            ):
                try:
                    _display_console(dm, re, se)
                except Exception as e:
                    logger.warning(
                        f"Console display error: {e}"
                    )
                last_console_update = now

            # Sleep 1 second between loop iterations
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

    # STEP 10 — Graceful shutdown
    logger.info(
        "Main loop exited — starting shutdown sequence"
    )

    try:
        ws_task.cancel()
        await asyncio.gather(
            ws_task, return_exceptions=True
        )
    except Exception:
        pass

    try:
        ws_health_task.cancel()
        await asyncio.gather(
            ws_health_task, return_exceptions=True
        )
    except Exception:
        pass

    await _graceful_shutdown(se, dm, shutdown_event)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(
            "\nKeyboardInterrupt received — "
            "shutdown complete"
        )
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        print(traceback.format_exc())
        sys.exit(1)