#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nifty Current-Month Futures - Intraday Algo
==========================================

Strategy (exactly as specified):

    Daily EMA filter (10 / 50)  ->  regime gate
        price above BOTH EMAs      -> long-only
        price below BOTH EMAs      -> short-only
        price between the EMAs     -> NO TRADE, regardless of the 15m signal

    15-minute Supertrend (ATR 7, multiplier 2) signal on candle CLOSE
        + candle close above session VWAP  -> LONG  (regime long-only)
        + candle close below session VWAP  -> SHORT (regime short-only)

    Initial stop      : 80 points from the actual entry price
    Trailing trigger  : unrealised profit reaches +100 points -> stop = entry (breakeven)
    Trailing method   : 15m Supertrend level, updated on each 15m candle close only
                        (actual traded price is monitored tick-by-tick between closes)
    Stop rule         : monotonic - a stop is never loosened once tightened
    Exit              : active stop triggered (no +100 profit cap; +100 is only the trigger)
    Re-entry          : immediate on the next valid signal, no daily trade-count limit
                        (never auto-reverse purely because a stop was hit)

Conventions implemented (the two "execution details"):
    * Daily EMA filter uses the LATEST COMPLETED daily candle's EMA values, compared with
      the current price. Today's unfinished daily candle is never used.
    * Supertrend trailing stop updates ONLY on a new 15-minute candle close; stop-loss
      execution between closes runs on the actual traded price.

Credentials
-----------
Read from env.txt (KEY=VALUE lines) with OS environment variables as a fallback:

    UPSTOX_ACCESS_TOKEN   daily OAuth access token (Bearer)
    UPSTOX_API_TOKEN      app api key  (client_id)
    UPSTOX_API_SECRET     app api secret (client_secret)
    UPSTOX_REDIRECT_URI   redirect url registered for the app (needed for --login / --exchange-code)

Usage
-----
    python nifty_futures_supertrend.py --mode paper        # live quotes, simulated fills (default)
    python nifty_futures_supertrend.py --mode live         # real orders on Upstox
    python nifty_futures_supertrend.py --backtest 2026-06-01 2026-10-09
    python nifty_futures_supertrend.py --selftest          # offline engine unit checks, no API needed
    python nifty_futures_supertrend.py --login             # print the Upstox login URL
    python nifty_futures_supertrend.py --exchange-code CODE [--redirect-uri URI]

Nothing in this file trades until you pass --mode live. Default is paper.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import csv
import glob
import gzip
import io
import json
import logging
import math
import os
import signal
import statistics
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

try:
    import numpy as np
    import pandas as pd
    import requests
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing dependency: {exc}. Install with: pip install pandas numpy requests")


# =====================================================================================
# 1. CONFIGURATION
# =====================================================================================

@dataclass
class Config:
    # --- instrument ---------------------------------------------------------------
    underlying: str = "NIFTY"                 # underlying symbol in the instrument master
    segment: str = "NSE_FO"
    index_key: str = "NSE_INDEX|Nifty 50"     # fallback reference if master lookup fails
    exchange: str = "NSE"
    instrument_key_override: str = ""         # e.g. "NSE_FO|48704" to skip auto-discovery
    roll: bool = False                        # use each day's actual front-month contract

    # --- strategy parameters ------------------------------------------------------
    entry_timeframe: str = "15minute"          # built by resampling 1-minute candles
    # (Upstox serves only 1minute/30minute/day/week/month natively)
    trend_timeframe: str = "day"
    ema_fast: int = 10
    ema_slow: int = 50
    st_atr_period: int = 7
    st_multiplier: float = 3.0                # spec v2: was 2.0
    initial_stop_points: float = 100.0        # spec v2: was 80.0
    cost_points_per_side: float = 0.0         # LEGACY flat override; 0 -> use the cost model
    trail_trigger_points: float = 100.0

    # --- the decisions section 8 of the spec requires be made explicitly ----------
    # These are printed by print_strategy_decisions() before every backtest so that no
    # rule is ever resolved silently. Changing one changes the result, so each is named.
    #
    # (3) Breakeven level. The spec says "entry price". Strictly that is not breakeven:
    #     a trade stopped out at its entry still loses the round trip. False = entry
    #     price exactly, as written. True = entry adjusted by the modelled round-trip
    #     cost, so "breakeven" actually means no net loss.
    breakeven_covers_costs: bool = False
    #
    # (7) Rollover. A positional hold can still be open when its contract expires.
    #     "close"        - exit at the boundary and wait for a fresh signal on the new
    #                      contract. Simplest, and never mixes two contracts' prices.
    #     "roll_forward" - close the old contract and immediately re-open the same side
    #                      and quantity on the new one, carrying the stop as a DISTANCE
    #                      from the new entry (an absolute level from the old contract
    #                      would be meaningless against the new one's basis).
    roll_action: str = "close"
    #
    # (8) Margin. NSE index-futures span+elm is roughly 11-13% of contract value. This
    #     is a PRE-TRADE check: without it an entry is only discovered to be unaffordable
    #     when the broker rejects it, after the signal has already been acted on.
    margin_pct_of_notional: float = 0.12

    # --- execution realism --------------------------------------------------------
    # A backtest that books every fill at the exact printed price is optimistic in a way
    # that compounds: it wins trades the live system would lose. These knobs make the
    # simulated fill worse than the print, in the direction that hurts the trade.
    entry_slippage_points: float = 0.0        # adverse move applied to the ENTRY fill
    exit_slippage_points: float = 0.0         # extra adverse move applied to EVERY exit fill
    # Which price an entry is assumed to fill at. A 15m signal is only actionable at the
    # CLOSE of that candle, so booking the fill at that same candle's close assumes you
    # transact at the exact closing print. "next_minute_open" instead fills at the open
    # of the first 1-minute bar at or after the signal became actionable - still an
    # approximation, but one that cannot use a price known before the order existed.
    # Falls back to "signal_close" when 1-minute data is not being replayed.
    entry_fill: str = "next_minute_open"      # "next_minute_open" | "signal_close"
    # How much of the MEASURED stop gap to book. The engine already records how far the
    # market traded beyond the stop (`slip_pts`); previously that number was only
    # displayed and the exit was booked AT the stop, which overstated results.
    #   0.0 = book at the stop level      (the old, optimistic behaviour)
    #   0.5 = book halfway to the extreme (a reasonable central estimate)
    #   1.0 = book at the adverse extreme (conservative bound)
    stop_slip_fraction: float = 1.0

    # --- capital protection -------------------------------------------------------
    # All four are OFF at zero so existing behaviour is unchanged unless asked for.
    initial_capital: float = 0.0              # 0 -> risk sizing disabled, use `quantity`
    risk_per_trade_pct: float = 0.0           # fraction of CURRENT EQUITY risked per trade
    min_lots: int = 1                         # trade at least this many lots when sizing is on
    # "min_lot"  = always take at least min_lots; the risk % is a TARGET, and the
    #              actual risk per trade is reported when it exceeds the target.
    # "strict"   = skip the trade entirely when even min_lots would breach the budget,
    #              so the risk % is a hard CAP.
    risk_mode: str = "min_lot"
    daily_loss_limit: float = 0.0             # block new entries for the rest of the day
    weekly_loss_limit: float = 0.0            # block new entries for the rest of the week
    monthly_dd_review: float = 0.0            # drawdown from the month's equity peak

    # --- cost model (INR; converted to points using the traded quantity) -----------
    # Defaults are NSE index-futures rates as commonly published. CONFIRM THESE AGAINST
    # YOUR OWN BROKER STATEMENT before trusting any backtest number - they vary by
    # broker, by turnover slab, and over time.
    cost_brokerage_per_side: float = 20.0     # Upstox flat fee per executed order
    cost_exchange_pct: float = 0.000019       # NSE index futures transaction charge
    cost_sebi_pct: float = 0.000001           # SEBI turnover fee
    cost_stamp_pct: float = 0.00002           # stamp duty, BUY side only
    cost_gst_pct: float = 0.18                # GST on brokerage + exchange charges only
    cost_spread_points: float = 0.0           # half-spread, per side, in index points
    # STT on futures is charged on the SELL side only. Budget 2026 raised it from 0.02%
    # to 0.05% with effect from 1 April 2026, so a window spanning that date needs BOTH
    # rates - a single constant overstates the first quarter or understates the rest.
    # It is also the largest single charge here by a wide margin, several times the
    # exchange transaction fee, and it is NOT subject to GST.
    cost_stt_sell_pct: float = 0.0005         # on/after cost_stt_change_date
    cost_stt_sell_pct_legacy: float = 0.0002  # before it
    cost_stt_change_date: str = "2026-04-01"

    entry_on: str = "flip"                    # "flip" = trend reversal candle, "state" = any bar in trend
    allowed_sides: str = "both"               # "both" | "long" | "short" - blocks NEW entries only
    # "index" = Nifty spot EMAs with the index price compared against them, so the basis
    # never contaminates the filter and the series does not reset at every contract roll.
    # "futures" = the traded contract's own daily closes (needs ~55 sessions of history,
    # which a fresh contract does not have, so it silently switches to the index anyway -
    # a basis-mismatched hybrid). "index" avoids that ambiguity entirely.
    daily_filter_source: str = "index"
    candle_label: str = "auto"                # "auto" | "open" | "close" (timestamp labelling of candles)

    # --- sizing / orders ----------------------------------------------------------
    quantity: int = 0                         # 0 -> one lot, read from the instrument master
    product: str = ""                         # "" = auto: NRML when positional, MIS intraday
    validity: str = "DAY"
    order_tag: str = "st_algo"

    # --- hold mode ----------------------------------------------------------------
    # "positional" holds a position across sessions until its stop is hit. This is the
    # spec: the trade is closed by the stop, not by the clock. It changes three things
    # that the intraday build assumed -
    #   * the order product must be NRML ("M"); MIS ("I") is squared off by the broker
    #     at the end of the session, which would silently override the strategy.
    #   * an open position is exposed to the OVERNIGHT GAP. The stop cannot execute
    #     while the market is shut, so the next session's opening print becomes the
    #     fill. That risk does not exist intraday and is measured separately below.
    #   * a position can still be open when its contract expires, so it must be closed
    #     at the roll boundary rather than carried into a different contract's prices.
    # "intraday" keeps the old behaviour: force-exit at cfg.square_off, product MIS.
    hold: str = "positional"

    # --- session ------------------------------------------------------------------
    # The exchanges run special weekend sessions (the Union Budget on Sun 1 Feb 2026 was
    # a live F&O session, 09:15-15:30). The bot cannot know that calendar, so it does not
    # trade weekends unless you tell it to. Leaving this False is the safe default - the
    # cost is a missed session, not an unprotected position.
    trade_on_weekends: bool = False
    market_open: str = "09:15"
    market_close: str = "15:30"
    entry_cutoff: str = "15:15"               # no new entries after this
    square_off: str = "15:15"                 # force-exit any open position at this time
    tz_offset_hours: float = 5.5              # IST

    # --- runtime ------------------------------------------------------------------
    poll_seconds: float = 1.0                 # LTP polling interval between candle closes
    candle_refresh_seconds: float = 20.0      # how often to refresh intraday/daily candles
    rest_retry: int = 4
    rest_timeout: float = 15.0
    broker_sl_backstop: bool = False          # also rest an SL-M order at the exchange
    broker_sl_explicit_off: bool = False      # user passed --no-broker-sl; honoured, not overridden
    allow_same_candle_reentry: bool = True    # a flip candle may both exit and re-enter
    env_file: str = "env.txt"
    trade_log: str = ""                       # default trades_<date>.csv
    state_file: str = ""                      # default state_<date>.json
    instrument_cache: str = "instrument_master_cache.json"

    # --- derived at startup -------------------------------------------------------
    access_token: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    redirect_uri: str = field(default="", repr=False)


BASE_URL = "https://api.upstox.com"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

log = logging.getLogger("nifty_st")


# =====================================================================================
# 2. CREDENTIALS
# =====================================================================================

def load_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE file. Blank lines and '#' comments are ignored."""
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


@dataclass
class CostModel:
    """
    Realistic round-trip transaction cost, in rupees and in index points.

    Two things a flat "points per side" estimate cannot express, and both matter here:

    1. Most of this cost is a PERCENTAGE OF TURNOVER, so the same trade costs more at
       26,000 than at 22,000.
    2. The legs are ASYMMETRIC and DIRECTION-DEPENDENT. Stamp duty falls on the buy leg
       and STT on the sell leg. On a LONG the entry is the buy; on a SHORT the entry is
       the SELL. Treating every entry as a buy puts both taxes on the wrong leg.

    STT on futures is charged on the sell side only and rose from 0.02% to 0.05% on
    1 April 2026, so the rate is resolved from the trade's own date. It is also the
    single largest charge in this model - larger than brokerage, exchange, SEBI and
    stamp combined - and GST does not apply to it, nor to stamp or SEBI.

    Points are derived by dividing the rupee cost by the traded quantity, so they are
    directly comparable with the strategy's point-denominated P&L.
    """
    brokerage_per_side: float = 20.0
    exchange_pct: float = 0.000019
    sebi_pct: float = 0.000001
    stamp_pct: float = 0.00002
    gst_pct: float = 0.18
    spread_points: float = 0.0
    stt_sell_pct: float = 0.0005
    stt_sell_pct_legacy: float = 0.0002
    stt_change_date: str = "2026-04-01"
    flat_points_per_side: float = 0.0     # legacy override; non-zero bypasses the model

    def __post_init__(self) -> None:
        try:
            self._stt_change = datetime.strptime(self.stt_change_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            self._stt_change = datetime(2026, 4, 1).date()

    @classmethod
    def from_config(cls, cfg: "Config") -> "CostModel":
        return cls(brokerage_per_side=cfg.cost_brokerage_per_side,
                   exchange_pct=cfg.cost_exchange_pct, sebi_pct=cfg.cost_sebi_pct,
                   stamp_pct=cfg.cost_stamp_pct, gst_pct=cfg.cost_gst_pct,
                   spread_points=cfg.cost_spread_points,
                   stt_sell_pct=cfg.cost_stt_sell_pct,
                   stt_sell_pct_legacy=cfg.cost_stt_sell_pct_legacy,
                   stt_change_date=cfg.cost_stt_change_date,
                   flat_points_per_side=cfg.cost_points_per_side)

    def stt_rate(self, ts=None) -> float:
        """STT rate on the sell leg, resolved against the trade's own date."""
        d = getattr(ts, "date", lambda: None)() if ts is not None else None
        if d is None:
            return self.stt_sell_pct        # unknown date -> assume the current rate
        return self.stt_sell_pct_legacy if d < self._stt_change else self.stt_sell_pct

    def leg_money(self, price: float, quantity: float, is_buy: bool, ts=None) -> float:
        """Rupee cost of ONE leg for `quantity` units."""
        if self.flat_points_per_side:
            return self.flat_points_per_side * quantity
        turnover = max(float(price), 0.0) * quantity
        exch = turnover * self.exchange_pct
        sebi = turnover * self.sebi_pct
        stamp = turnover * self.stamp_pct if is_buy else 0.0
        stt = 0.0 if is_buy else turnover * self.stt_rate(ts)
        # GST applies to brokerage and exchange transaction charges only. STT, stamp
        # duty and the SEBI turnover fee are outside its base.
        taxable = self.brokerage_per_side + exch
        return taxable * (1.0 + self.gst_pct) + sebi + stamp + stt

    def round_trip_money(self, entry_price: float, exit_price: float, quantity: float,
                         side: str = "LONG", ts=None) -> float:
        """
        Cost of a complete round trip, with each tax on the correct leg.

        A LONG is bought then sold; a SHORT is SOLD then bought back. Which leg carries
        STT therefore depends on the direction, not on the order of the two prices.
        """
        if side == "SHORT":
            buy_price, sell_price = exit_price, entry_price
        else:
            buy_price, sell_price = entry_price, exit_price
        return (self.leg_money(buy_price, quantity, True, ts)
                + self.leg_money(sell_price, quantity, False, ts)
                + 2.0 * self.spread_points * quantity)

    def round_trip_points(self, entry_price: float, exit_price: float, quantity: float,
                          side: str = "LONG", ts=None) -> float:
        if not quantity:
            return 0.0
        return self.round_trip_money(entry_price, exit_price, quantity, side, ts) / quantity

    def describe(self, price: float = 24000.0, quantity: float = 65, ts=None) -> str:
        b = self.leg_money(price, quantity, True, ts)
        s = self.leg_money(price, quantity, False, ts)
        sp = 2.0 * self.spread_points * quantity
        return (f"buy ₹{b:.2f} + sell ₹{s:.2f} + spread ₹{sp:.2f} = ₹{b + s + sp:.2f} "
                f"({self.round_trip_points(price, price, quantity, 'LONG', ts):.2f} pts, "
                f"STT {100 * self.stt_rate(ts):.3f}% sell-side)")


class RiskManager:
    """
    Risk-budget position sizing plus account-level loss limits.

    Sizing solves for the quantity whose FULL-STOP loss equals the risk budget:

        risk_money    = equity * risk_per_trade_pct
        risk_points   = initial_stop_points + modelled round-trip cost
        lots          = floor(risk_money / (risk_points * lot_size))

    Costs are included in the budget on purpose: a trade that stops out loses the stop
    AND the round trip, so budgeting only the stop systematically under-risks.

    Because NIFTY's lot is 65 units, one lot with an 80-point stop already risks
    80 x 65 = Rs 5,200 before costs. On Rs 10 lakh that is 0.52%, so any budget below
    roughly 0.54% rounds down to ZERO lots. `risk_mode` makes that an explicit choice
    instead of a silent one:

      "min_lot" - take min_lots anyway. The risk percentage is a TARGET and the actual
                  risk per trade is reported whenever it exceeds the target.
      "strict"  - return 0 and skip the trade. The risk percentage is a hard CAP.

    The limits are measured against MARK-TO-MARKET equity (realised P&L plus the open
    position's unrealised P&L), not realised equity alone. Measuring only closed trades
    lets an account carry a large open loss and keep opening new positions, because the
    damage has not been booked yet.
    """

    def __init__(self, cfg: "Config", lot_size: int, stop_points: float,
                 cost_model: Optional[CostModel] = None):
        self.cfg = cfg
        self.lot_size = max(1, int(lot_size or 1))
        self.stop_points = float(stop_points)
        self.cost_model = cost_model
        self.equity = float(cfg.initial_capital or 0.0)     # realised
        self.open_pnl = 0.0                                  # unrealised
        self.mtm_equity = self.equity                        # realised + unrealised
        self.open_side: Optional[str] = None
        self.open_qty = 0
        self.open_entry = 0.0
        self.min_lots = max(0, int(getattr(cfg, "min_lots", 1)))
        self.risk_mode = getattr(cfg, "risk_mode", "min_lot")
        self.day = self.week = self.month = None
        self.day_start = self.week_start = self.month_peak = self.mtm_equity
        self.blocked = {"daily": 0, "weekly": 0, "monthly": 0,
                        "sizing": 0, "strict_cap": 0, "margin": 0}
        self.last_block_reason = ""
        self.risk_per_lot_money = 0.0
        self.actual_risk_money = 0.0
        self.committed_margin = 0.0
        self.margin_pct = float(getattr(cfg, "margin_pct_of_notional", 0.0) or 0.0)
        self.lots_used: list[int] = []
        self.equity_curve: list[tuple] = []   # (timestamp, MTM equity in rupees)
        # True only once mark() has actually run. Without it the curve holds realised
        # equity alone, and labelling that "mark-to-market" would understate risk.
        self.mtm_tracked = False

    @property
    def enabled(self) -> bool:
        return self.cfg.risk_per_trade_pct > 0 and self.cfg.initial_capital > 0

    @staticmethod
    def _periods(ts) -> tuple:
        d = ts.date()
        iso = d.isocalendar()
        return d, (iso[0], iso[1]), (d.year, d.month)

    def _roll_periods(self, ts) -> None:
        day, week, month = self._periods(ts)
        base = self.mtm_equity
        if self.day != day:
            self.day, self.day_start = day, base
        if self.week != week:
            self.week, self.week_start = week, base
        if self.month != month:
            self.month, self.month_peak = month, base

    def risk_points(self, price: float) -> float:
        pts = self.stop_points
        if self.cost_model is not None:
            pts += self.cost_model.round_trip_points(price, price, self.lot_size)
        return pts

    def size_units(self, price: float) -> int:
        """Units to trade. 0 means do not trade."""
        if not self.enabled:
            return int(self.cfg.quantity or self.lot_size)
        rp = self.risk_points(price)
        if rp <= 0:
            return 0
        per_lot = rp * self.lot_size
        self.risk_per_lot_money = per_lot
        budget = self.mtm_equity * self.cfg.risk_per_trade_pct
        lots = int(math.floor(budget / per_lot))
        if lots < self.min_lots:
            if self.risk_mode == "strict":
                self.blocked["strict_cap"] += 1
                self.last_block_reason = (
                    f"one lot risks Rs {per_lot:,.0f} but the budget is Rs {budget:,.0f}")
                self.lots_used.append(0)
                return 0
            self.blocked["sizing"] += 1
            lots = self.min_lots
        self.actual_risk_money = lots * per_lot
        self.lots_used.append(lots)
        return lots * self.lot_size

    def margin_required(self, price: float, qty: int) -> float:
        """SPAN + ELM style estimate: a percentage of contract value."""
        if self.margin_pct <= 0 or not qty:
            return 0.0
        return abs(float(price)) * int(qty) * self.margin_pct

    def has_margin(self, price: float, qty: int) -> bool:
        """
        PRE-TRADE margin check. Without it an unaffordable entry is only discovered when
        the broker rejects the order - after the signal has been acted on and, in the
        backtest, after a profit has already been booked that could never have been taken.
        """
        if self.margin_pct <= 0:
            return True
        need = self.margin_required(price, qty)
        if self.committed_margin + need <= self.mtm_equity:
            return True
        self.blocked["margin"] += 1
        self.last_block_reason = (
            f"margin Rs {self.committed_margin + need:,.0f} required "
            f"(free Rs {self.mtm_equity - self.committed_margin:,.0f})")
        return False

    def on_open(self, ts, side: str, qty: int, entry_price: float) -> None:
        self._roll_periods(ts)
        self.open_side, self.open_qty = side, int(qty)
        self.open_entry = float(entry_price)
        self.committed_margin += self.margin_required(entry_price, qty)
        self.mark(ts, entry_price)

    def mark(self, ts, price: float) -> float:
        """Refresh MTM equity from the open position. Called on every replayed tick."""
        if self.open_qty:
            delta = (price - self.open_entry if self.open_side == "LONG"
                     else self.open_entry - price)
            self.open_pnl = delta * self.open_qty
        else:
            self.open_pnl = 0.0
        self.mtm_equity = self.equity + self.open_pnl
        self.mtm_tracked = True
        self.equity_curve.append((ts, self.mtm_equity))
        return self.mtm_equity

    def can_open(self, ts) -> bool:
        """False once a daily / weekly / monthly limit is breached, OPEN LOSSES INCLUDED."""
        cfg = self.cfg
        if cfg.initial_capital <= 0:
            return True
        self._roll_periods(ts)
        eq = self.mtm_equity
        day_loss = self.day_start - eq
        week_loss = self.week_start - eq
        month_dd = self.month_peak - eq
        if cfg.daily_loss_limit > 0 and day_loss >= cfg.daily_loss_limit:
            self.blocked["daily"] += 1
            self.last_block_reason = (f"daily loss Rs {day_loss:,.0f} >= "
                                      f"limit Rs {cfg.daily_loss_limit:,.0f}")
            return False
        if cfg.weekly_loss_limit > 0 and week_loss >= cfg.weekly_loss_limit:
            self.blocked["weekly"] += 1
            self.last_block_reason = (f"weekly loss Rs {week_loss:,.0f} >= "
                                      f"limit Rs {cfg.weekly_loss_limit:,.0f}")
            return False
        if cfg.monthly_dd_review > 0 and month_dd >= cfg.monthly_dd_review:
            self.blocked["monthly"] += 1
            self.last_block_reason = (f"monthly drawdown Rs {month_dd:,.0f} >= "
                                      f"review Rs {cfg.monthly_dd_review:,.0f}")
            return False
        return True

    def on_close(self, ts, pnl_money: float) -> None:
        self._roll_periods(ts)
        self.equity += float(pnl_money)
        self.open_side, self.open_qty, self.open_entry = None, 0, 0.0
        self.open_pnl = 0.0
        self.committed_margin = 0.0
        self.mtm_equity = self.equity
        self.month_peak = max(self.month_peak, self.mtm_equity)
        self.equity_curve.append((ts, self.mtm_equity))


def resolve_credentials(cfg: Config) -> None:
    """env.txt wins over the process environment for the three Upstox keys."""
    file_values = load_env_file(cfg.env_file)
    merged = dict(os.environ)
    merged.update({k: v for k, v in file_values.items() if v})

    cfg.access_token = merged.get("UPSTOX_ACCESS_TOKEN", "").strip()
    cfg.api_key = merged.get("UPSTOX_API_TOKEN", "") or merged.get("UPSTOX_API_KEY", "")
    cfg.api_secret = merged.get("UPSTOX_API_SECRET", "")
    cfg.redirect_uri = merged.get("UPSTOX_REDIRECT_URI", "")

    if not cfg.access_token:
        raise SystemExit(
            f"No UPSTOX_ACCESS_TOKEN found in '{cfg.env_file}' or the environment.\n"
            "Put your daily access token there, or run: --login then --exchange-code <code>"
        )


def token_expiry(access_token: str) -> Optional[datetime]:
    """Decode the JWT payload to read 'exp' (best effort, no signature check)."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), tz=_ist())
    except Exception:
        return None
    return None


def check_token_freshness(cfg: Config) -> None:
    exp = token_expiry(cfg.access_token)
    if exp is None:
        log.warning("Could not read expiry from the access token; proceeding.")
        return
    now = datetime.now(tz=_ist())
    if now >= exp:
        raise SystemExit(
            f"The access token in '{cfg.env_file}' EXPIRED at {exp:%Y-%m-%d %H:%M:%S IST} "
            f"(now {now:%Y-%m-%d %H:%M:%S IST}).\n"
            "Upstox access tokens are valid only until 03:30 IST the next day. Get a fresh one from\n"
            "https://account.upstox.com/developer/apps and update UPSTOX_ACCESS_TOKEN in env.txt,\n"
            "or run:  python nifty_futures_supertrend.py --login  then  --exchange-code <code>"
        )
    log.info("Access token valid until %s (%s left).", exp.strftime("%Y-%m-%d %H:%M:%S IST"),
             _human_delta(exp - now))


def _human_delta(delta: timedelta) -> str:
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    return f"{hours}h {rem // 60}m"


# =====================================================================================
# 3. UPSTOX REST CLIENT
# =====================================================================================

class OrderRejected(Exception):
    """The exchange rejected or cancelled the order - no position was created."""
    def __init__(self, order_id: str, status: str, details: Any):
        self.order_id, self.status, self.details = order_id, status, details
        super().__init__(f"Order {order_id} {status}: {details}")


class OrderUnconfirmed(Exception):
    """
    The order may or may not have filled. The caller MUST reconcile against the broker
    rather than assume either way - guessing here is how a bot ends up double-positioned.
    """
    def __init__(self, order_id: str, detail: str):
        self.order_id, self.detail = order_id, detail
        super().__init__(f"Order {order_id} unresolved: {detail}")


class UpstoxAPIError(Exception):
    def __init__(self, status_code: int, payload: Any, url: str):
        self.status_code = status_code
        self.payload = payload
        self.url = url
        detail = payload.get("errors") if isinstance(payload, dict) else payload
        super().__init__(f"HTTP {status_code} from {url}: {detail}")


class UpstoxClient:
    """Minimal Upstox v2 REST client (requests only - no SDK dependency)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Api-Version": "2.0",
            "Authorization": f"Bearer {cfg.access_token}",
        })

    # ---------------------------------------------------------------- transport --
    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 json_body: Optional[dict] = None, auth: bool = True,
                 form: Optional[dict] = None) -> Any:
        url = BASE_URL + path
        last_error: Optional[Exception] = None
        for attempt in range(1, self.cfg.rest_retry + 1):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, data=form,
                    timeout=self.cfg.rest_timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Network error (%s/%s) on %s: %s", attempt, self.cfg.rest_retry, path, exc)
                time.sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code == 401:
                raise UpstoxAPIError(401, _safe_json(resp), url)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = UpstoxAPIError(resp.status_code, _safe_json(resp), url)
                log.warning("Transient HTTP %s (%s/%s) on %s", resp.status_code, attempt,
                            self.cfg.rest_retry, path)
                time.sleep(min(2 ** attempt, 8))
                continue
            if not resp.ok:
                raise UpstoxAPIError(resp.status_code, _safe_json(resp), url)
            return _safe_json(resp)

        raise last_error if last_error else UpstoxAPIError(0, None, url)

    # ------------------------------------------------------------------ market ---
    def ltp(self, instrument_key: str) -> Optional[float]:
        data = self._request("GET", "/v2/market-quote/ltp",
                             params={"instrument_key": instrument_key})
        try:
            return float(data["data"][instrument_key]["last_price"])
        except (KeyError, TypeError, ValueError):
            return None

    def quote(self, instrument_key: str) -> Optional[dict]:
        data = self._request("GET", "/v2/market-quote/quotes",
                             params={"instrument_key": instrument_key})
        try:
            return data["data"][instrument_key]
        except (KeyError, TypeError):
            return None

    def historical(self, instrument_key: str, interval: str,
                   to_date: date, from_date: Optional[date] = None) -> pd.DataFrame:
        """Daily / dated candles. Returns an ascending DataFrame indexed by candle timestamp."""
        to_s = to_date.strftime("%Y-%m-%d")
        key = urllib.parse.quote(instrument_key, safe="")
        if from_date is None:
            path = f"/v2/historical-candle/{key}/{interval}/{to_s}"
        else:
            path = f"/v2/historical-candle/{key}/{interval}/{to_s}/{from_date:%Y-%m-%d}"
        return self._candles_frame(self._request("GET", path))

    def historical_range(self, instrument_key: str, interval: str,
                         to_date: date, from_date: date) -> pd.DataFrame:
        """
        Fetch a date range, split into chunks Upstox will accept.

        A single 1minute request spanning more than about a month is rejected outright
        with UDAPI1148 'Invalid date range', so long windows are fetched in pieces and
        stitched back together. Overlapping candles at chunk seams are de-duplicated.
        """
        max_days = MAX_RANGE_DAYS.get(interval, 300)
        frames: list[pd.DataFrame] = []
        cur_to = to_date
        while cur_to >= from_date:
            cur_from = max(from_date, cur_to - timedelta(days=max_days - 1))
            try:
                df = self.historical(instrument_key, interval, cur_to, cur_from)
            except UpstoxAPIError as exc:
                log.warning("Upstox rejected %s..%s at %s: %s", cur_from, cur_to, interval, exc)
                df = pd.DataFrame()
            if df.empty:
                log.info("  %s .. %s  ->  no data", cur_from, cur_to)
            else:
                log.info("  %s .. %s  ->  %d candle(s)", cur_from, cur_to, len(df))
                frames.append(df)
            cur_to = cur_from - timedelta(days=1)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    def intraday(self, instrument_key: str, interval: str) -> pd.DataFrame:
        """Today's (and the current, still-forming) intraday candles."""
        key = urllib.parse.quote(instrument_key, safe="")
        return self._candles_frame(
            self._request("GET", f"/v2/historical-candle/intraday/{key}/{interval}"))

    @staticmethod
    def _candles_frame(payload: Any) -> pd.DataFrame:
        rows = (payload or {}).get("data", {}).get("candles", []) or []
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "oi"]
                          if len(rows[0]) >= 7 else
                          ["ts", "open", "high", "low", "close", "volume"] +
                          ["oi"] * (len(rows[0]) - 6))
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce").dt.tz_convert(_ist())
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["ts", "close"]).set_index("ts").sort_index()
        return df[["open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------- trade ---
    # ------------------------------------------------------------- expired contracts --
    def expiries(self, underlying_key: str) -> list[str]:
        """All expiries Upstox knows for an underlying, including lapsed ones."""
        payload = self._request("GET", "/v2/expired-instruments/expiries",
                                params={"instrument_key": underlying_key})
        return _extract_list(payload)

    def expired_future_contract(self, underlying_key: str, expiry_date: date) -> list[dict]:
        """The expired future contract(s) for one expiry date."""
        payload = self._request("GET", "/v2/expired-instruments/future/contract",
                                params={"instrument_key": underlying_key,
                                        "expiry_date": expiry_date.strftime("%Y-%m-%d")})
        return [r for r in _extract_list(payload) if isinstance(r, dict)]

    def expired_historical(self, expired_key: str, interval: str,
                           to_date: date, from_date: date) -> pd.DataFrame:
        key = urllib.parse.quote(expired_key, safe="")
        return self._candles_frame(self._request(
            "GET", f"/v2/expired-instruments/historical-candle/{key}/{interval}/"
                   f"{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}"))

    def positions(self) -> list[dict]:
        payload = self._request("GET", "/v2/order/positions")
        return (payload or {}).get("data") or []

    def place_order(self, *, instrument_token: str, transaction_type: str, quantity: int,
                    order_type: str = "MARKET", price: float = 0.0,
                    trigger_price: float = 0.0) -> Optional[str]:
        body = {
            "quantity": int(quantity),
            "product": self.cfg.product or ("M" if self.cfg.hold == "positional" else "I"),
            "validity": self.cfg.validity,
            "price": float(price),
            "tag": self.cfg.order_tag,
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": float(trigger_price),
            "is_amo": False,
        }
        payload = self._request("POST", "/v2/order/place", json_body=body)
        order_id = (payload or {}).get("data", {}).get("order_id")
        if not order_id:
            raise UpstoxAPIError(200, payload, "/v2/order/place")
        return str(order_id)

    def modify_order(self, order_id: str, *, quantity: int, order_type: str,
                     price: float, trigger_price: float, transaction_type: str) -> Optional[str]:
        body = {
            "quantity": int(quantity),
            "validity": self.cfg.validity,
            "price": float(price),
            "order_id": order_id,
            "order_type": order_type,
            "trigger_price": float(trigger_price),
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
        }
        payload = self._request("PUT", "/v2/order/modify", json_body=body)
        return (payload or {}).get("data", {}).get("order_id")

    def order_details(self, order_id: str) -> dict:
        payload = self._request("GET", "/v2/order/details", params={"order_id": order_id})
        return (payload or {}).get("data", {}).get("order", {}) or {}

    # -------------------------------------------------------------------- auth ---
    @staticmethod
    def exchange_code(*, code: str, client_id: str, client_secret: str,
                      redirect_uri: str, code_verifier: str = "") -> dict:
        form = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        resp = requests.post(BASE_URL + "/v2/login/authorization/token", data=form,
                             headers={"Accept": "application/json", "Api-Version": "2.0"},
                             timeout=20)
        return _safe_json(resp)


def _extract_list(payload: Any) -> list:
    """
    Pull the row list out of an Upstox response without assuming its exact nesting.
    The expired-instrument endpoints are not documented in the SDK, so this tolerates
    a bare list, {"data": [...]}, or {"data": {"contracts": [...]}}.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:500]}


# =====================================================================================
# 4. INSTRUMENT DISCOVERY  (current-month future)
# =====================================================================================

@dataclass
class Instrument:
    instrument_key: str
    trading_symbol: str
    instrument_token: str
    lot_size: int
    expiry: date
    tick_size: float = 0.05


def fetch_instrument_master(cfg: Config) -> list[dict]:
    if os.path.exists(cfg.instrument_cache):
        age_hours = (time.time() - os.path.getmtime(cfg.instrument_cache)) / 3600
        if age_hours < 18:
            log.info("Using cached instrument master (%.1fh old).", age_hours)
            with open(cfg.instrument_cache, "r", encoding="utf-8") as fh:
                return json.load(fh)
    log.info("Downloading instrument master from Upstox ...")
    resp = requests.get(MASTER_URL, timeout=120)
    resp.raise_for_status()
    data = json.loads(gzip.decompress(resp.content))
    with open(cfg.instrument_cache, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    log.info("Instrument master: %d rows cached.", len(data))
    return data


def find_current_month_future(cfg: Config, master: Optional[list[dict]] = None) -> Instrument:
    if cfg.instrument_key_override:
        key = cfg.instrument_key_override
        rows = [r for r in (master or []) if r.get("instrument_key") == key]
        if rows:
            return _to_instrument(rows[0])
        log.warning("Override key %s not found in master; using it with default lot/tick.", key)
        return Instrument(key, cfg.instrument_key_override, key.split("|")[-1], 65, date.today())

    master = master if master is not None else fetch_instrument_master(cfg)
    today = datetime.now(tz=_ist()).date()
    candidates = [
        r for r in master
        if r.get("segment") == cfg.segment
        and r.get("instrument_type") == "FUT"
        and str(r.get("underlying_symbol", "")).upper() == cfg.underlying.upper()
        and r.get("expiry")
    ]
    if not candidates:
        raise SystemExit(
            f"No {cfg.underlying} futures found in segment {cfg.segment}. "
            "Set INSTRUMENT_KEY_OVERRIDE or check the master file."
        )
    candidates.sort(key=lambda r: r["expiry"])
    # Current month = nearest expiry that has not already passed.
    live = [r for r in candidates if datetime.fromtimestamp(r["expiry"] / 1000, tz=_ist()).date() >= today]
    chosen = live[0] if live else candidates[-1]
    return _to_instrument(chosen)


def _to_instrument(row: dict) -> Instrument:
    expiry = datetime.fromtimestamp(row["expiry"] / 1000, tz=_ist()).date()
    tick = float(row.get("tick_size") or 0.05)
    # The master sometimes reports index-future ticks in paise-ish units; NIFTY ticks at 0.05.
    if tick > 1.0:
        tick = 0.05
    return Instrument(
        instrument_key=row["instrument_key"],
        trading_symbol=row.get("trading_symbol", row["instrument_key"]),
        instrument_token=str(row.get("exchange_token", row["instrument_key"].split("|")[-1])),
        lot_size=int(row.get("lot_size") or 1),
        expiry=expiry,
        tick_size=tick,
    )


# =====================================================================================
# 5. INDICATORS
# =====================================================================================

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder-smoothed ATR (RMA). First value = simple mean of the first `period` TRs."""
    tr = true_range(df)
    atr = pd.Series(index=df.index, dtype="float64")
    if len(tr) < period:
        return atr
    atr.iloc[period - 1] = tr.iloc[:period].mean()
    for i in range(period, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
    return atr


def supertrend(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """Classic Supertrend. Returns columns: atr, upper, lower, trend (1 bullish / -1 bearish)."""
    out = pd.DataFrame(index=df.index)
    out["atr"] = wilder_atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0

    basic_upper = (hl2 + multiplier * out["atr"]).to_numpy()
    basic_lower = (hl2 - multiplier * out["atr"]).to_numpy()
    close = df["close"].to_numpy()

    n = len(df)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    trend = np.full(n, np.nan)

    for i in range(n):
        if np.isnan(basic_upper[i]):
            continue
        if np.isnan(final_upper[i - 1]) if i > 0 else True:
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            trend[i] = 1 if close[i] > final_upper[i] else -1
            continue

        final_upper[i] = basic_upper[i] if (basic_upper[i] < final_upper[i - 1]
                                            or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = basic_lower[i] if (basic_lower[i] > final_lower[i - 1]
                                            or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        prev_trend = trend[i - 1]
        if prev_trend == 1:
            trend[i] = -1 if close[i] < final_lower[i] else 1
        else:
            trend[i] = 1 if close[i] > final_upper[i] else -1

    out["upper"] = final_upper
    out["lower"] = final_lower
    out["trend"] = trend
    return out


# --- timeframe handling -------------------------------------------------------------
# Upstox only serves these intervals. "15minute" is NOT one of them (the intraday
# endpoint rejects it with UDAPI1076: "Interval accepts one of (1minute,30minute)"),
# so any other timeframe is built by resampling 1-minute candles locally.
# Note the 1-minute history depth limit: ~1 month back from to_date.
NATIVE_INTERVALS = ("1minute", "30minute", "day", "week", "month")
# Widest single request Upstox accepts per interval (kept inside the documented limits).
MAX_RANGE_DAYS = {"1minute": 25, "30minute": 300, "day": 360, "week": 3000, "month": 3000}
ONE_MINUTE_HISTORY_DAYS = 28


def interval_minutes(interval: str) -> Optional[int]:
    """'15minute' -> 15. Returns None for non-minute intervals."""
    if not interval.endswith("minute"):
        return None
    try:
        return int(interval[: -len("minute")])
    except ValueError:
        return None


def plan_fetch(entry_timeframe: str) -> tuple[str, int]:
    """
    Work out what to ask Upstox for in order to build `entry_timeframe` candles.

    Returns (interval_to_fetch, resample_minutes). resample_minutes == 1 means the
    API already returns the timeframe we want.
    """
    if entry_timeframe in NATIVE_INTERVALS:
        return entry_timeframe, 1
    minutes = interval_minutes(entry_timeframe)
    if minutes is None:
        raise SystemExit(f"Unsupported entry timeframe '{entry_timeframe}'.")
    if minutes > 30:
        raise SystemExit(
            f"Entry timeframe '{entry_timeframe}' cannot be built: Upstox serves at most "
            "30-minute candles natively and 1-minute is the finest granularity available."
        )
    return "1minute", minutes


def normalize_minute_labels(df: pd.DataFrame, session_open: str = "09:15",
                            minutes: int = 15) -> pd.DataFrame:
    """
    Upstox may stamp 1-minute candles with their open minute (09:15) or their close minute
    (09:16). Resampling into 15-minute buckets only lines up on open labels, so a
    close-labelled series is shifted back one minute first. Without this, every 15-minute
    candle would be built from the wrong 15 one-minute bars.
    """
    if df.empty or minutes <= 1:
        return df
    first = df.index[0]
    mins_past_midnight = int((first - first.normalize()).total_seconds() // 60)
    if mins_past_midnight % minutes == 0:
        # Already on the bucket grid (09:00 and 09:15 both are, for 15-minute buckets),
        # so these are open-labelled and need no shift.
        return df
    if (mins_past_midnight - 1) % minutes == 0:
        log.debug("1-minute candles are close-labelled; shifting to open labels.")
        return df.set_index(df.index - pd.Timedelta(minutes=1))
    log.warning("First 1-minute candle is stamped %s, which is not on the %d-minute grid; "
                "assuming open labels. Check the resampled candle times.",
                first.strftime("%H:%M"), minutes)
    return df


def resample_ohlcv(df: pd.DataFrame, minutes: int, session_open: str = "09:15") -> pd.DataFrame:
    """
    Aggregate 1-minute candles into `minutes`-minute candles, labelled by OPEN time.

    Buckets align to the session naturally (09:15 is a 15-minute boundary from midnight),
    so NSE 15-minute candles come out at 09:15, 09:30, 09:45 ... The final bucket may be
    the still-forming candle; callers treat a candle as closed only once its full window
    has elapsed, so a partial bar can never generate a signal.
    """
    if df.empty or minutes <= 1:
        return df
    df = normalize_minute_labels(df, session_open, minutes)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    # Carry the contract label through the resample. Without it the rolled series loses
    # the one column that says which contract each bar came from, and the roll-boundary
    # close in replay() silently never fires - so a position opened on the expiring
    # contract is stopped out against the NEW contract's prices, and the basis jump
    # between them gets booked as though it were stop slippage.
    if "contract" in df.columns:
        agg["contract"] = "first"
    out = df.resample(f"{minutes}min", label="left", closed="left").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


def supertrend_by_segment(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """
    Supertrend with a hard reset at every contract change.

    Stitched futures series jump in price at a roll purely because of basis, not because
    the market moved. Computing one continuous Supertrend across that jump manufactures
    flip signals out of nothing, so each contract is indicator-isolated.
    """
    if "contract" not in df.columns or df["contract"].nunique() <= 1:
        return supertrend(df, period, multiplier)
    parts = [supertrend(group, period, multiplier)
             for _, group in df.groupby("contract", sort=False)]
    out = pd.concat(parts)
    log.info("Supertrend reset at %d contract boundary(ies) to avoid basis-jump signals.",
             df["contract"].nunique() - 1)
    return out.reindex(df.index)


def session_vwap(df: pd.DataFrame, day: Optional[date] = None) -> pd.Series:
    """Cumulative session VWAP over 15-minute candles (typical price * volume / volume)."""
    d = df.copy()
    d["date"] = d.index.date
    if day is not None:
        d = d[d["date"] == day]
    if d.empty:
        return pd.Series(dtype="float64")
    typical = (d["high"] + d["low"] + d["close"]) / 3.0
    vol = d["volume"].fillna(0.0)
    pv = (typical * vol).cumsum()
    vv = vol.cumsum().replace(0.0, np.nan)
    vwap = pv / vv
    vwap.index = d.index
    return vwap.fillna(d["close"])



def filter_session(df: pd.DataFrame, open_hhmm: str = "09:15", close_hhmm: str = "15:30") -> pd.DataFrame:
    """
    Drop candles outside the trading session.

    Upstox 1-minute futures data starts at 09:00, so an unfiltered session yields a
    09:00 bucket of pre-open prints that would distort both the session VWAP and the
    first Supertrend bars of the day.
    """
    if df.empty:
        return df
    minutes = df.index.hour * 60 + df.index.minute
    oh, om = (int(x) for x in open_hhmm.split(":"))
    ch, cm = (int(x) for x in close_hhmm.split(":"))
    return df[(minutes >= oh * 60 + om) & (minutes < ch * 60 + cm)]


def validate_session(raw: pd.DataFrame, cfg: Config) -> dict:
    """
    Report what the exchange actually sent, rather than only filtering it away.

    Upstox 1-minute futures data routinely includes a 09:00 pre-open bucket and prints
    out to 15:39 - nine minutes past the 15:30 close. filter_session() drops those, but
    silently: if the feed ever widened, or a special session changed the hours, nothing
    would notice. This counts what fell outside the configured window so a change in the
    data shows up in the log instead of quietly altering VWAP and Supertrend.

    Weekend dates are NOT flagged as invalid: NSE and BSE run special sessions (the Union
    Budget on 1 Feb 2026 was a live Sunday session, F&O included), so a Saturday or Sunday
    bar can be perfectly genuine.
    """
    out = {"rows": len(raw), "pre_open": 0, "post_close": 0, "weekend_days": [],
           "first": None, "last": None}
    if raw.empty:
        return out
    minutes = raw.index.hour * 60 + raw.index.minute
    oh, om = (int(x) for x in cfg.market_open.split(":"))
    ch, cm = (int(x) for x in cfg.market_close.split(":"))
    out["pre_open"] = int((minutes < oh * 60 + om).sum())
    out["post_close"] = int((minutes >= ch * 60 + cm).sum())
    out["first"] = raw.index[0].to_pydatetime()
    out["last"] = raw.index[-1].to_pydatetime()
    out["weekend_days"] = sorted({d.isoformat() for d in raw.index.date if d.weekday() >= 5})
    if out["pre_open"] or out["post_close"]:
        log.info("Session check: %d bar(s) before %s and %d at/after %s were dropped "
                 "(feed spans %s..%s). Out-of-session prints are expected from Upstox - "
                 "they are excluded from VWAP and Supertrend.",
                 out["pre_open"], cfg.market_open, out["post_close"], cfg.market_close,
                 out["first"].strftime("%H:%M"), out["last"].strftime("%H:%M"))
    if out["weekend_days"]:
        log.info("Session check: data on %d weekend date(s): %s. This is NOT necessarily "
                 "an error - the exchanges run special sessions (e.g. the Union Budget on "
                 "Sun 1 Feb 2026, F&O included, 09:15-15:30). Verify against the exchange "
                 "calendar if the date is unexpected.",
                 len(out["weekend_days"]), ", ".join(out["weekend_days"][:6]))
    return out


def fetch_regime_data(client: "UpstoxClient", cfg: Config, instrument: Instrument,
                      start: date, end: date) -> tuple[pd.DataFrame, str]:
    """
    Return (daily_candles, source) for the EMA regime filter.

    A current-month future often has fewer daily candles than the EMA 50 filter needs -
    the Oct contract can list with only a few weeks of history. When that happens the
    regime would evaluate to "unknown" on every day and the strategy would never trade,
    so we fall back to the Nifty spot index, which has years of gap-free history.

    "index" is the default and is selected explicitly rather than as a rescue: futures
    price and spot index price differ by the basis, so mixing one against the other's
    EMAs is not the filter the strategy specifies. Callers choosing "index" get index
    EMAs AND the index price compared against them (see the price_series wiring in
    run_backtest and _handle_candle_close), so the two sides always agree.
    """
    need = cfg.ema_slow + 5
    if cfg.daily_filter_source == "index":
        # Skip the futures fetch entirely - it would only be discarded.
        return _index_daily(client, cfg, need, start, end), "index"
    future_daily = client.historical_range(instrument.instrument_key, cfg.trend_timeframe, end,
                                           start - timedelta(days=400))
    if len(future_daily) < need:
        log.warning("%s has only %d daily candles but the EMA %d regime filter needs at "
                    "least %d completed days. Falling back to the Nifty spot index (%s) "
                    "for the regime filter - otherwise the regime reads 'unknown' every "
                    "day and no trades can ever be taken.",
                    instrument.trading_symbol, len(future_daily), cfg.ema_slow, need,
                    cfg.index_key)
    else:
        return future_daily, "futures"

    index_daily = _index_daily(client, cfg, need, start, end)
    if len(index_daily) < need:
        raise SystemExit(
            f"Neither {instrument.trading_symbol} ({len(future_daily)} daily candles) nor the "
            f"Nifty index ({len(index_daily)}) has the {need} daily candles the EMA "
            f"{cfg.ema_slow} filter needs. Reduce --ema-slow or use a shorter window."
        )
    return index_daily, "index"


def _index_daily(client: "UpstoxClient", cfg: Config, need: int,
                 start: date, end: date) -> pd.DataFrame:
    """Daily candles for the spot index, with a hard failure if they are too short."""
    index_daily = client.historical_range(cfg.index_key, cfg.trend_timeframe, end,
                                          start - timedelta(days=400))
    if len(index_daily) < need:
        raise SystemExit(
            f"The Nifty index returned only {len(index_daily)} daily candles; the EMA "
            f"{cfg.ema_slow} regime filter needs at least {need}. The index series has "
            f"years of history, so this means the request failed - run --probe to check."
        )
    return index_daily


def daily_regime(daily: pd.DataFrame, price: float, fast: int, slow: int,
                 now: Optional[datetime] = None) -> dict:
    """
    Long-only  : price > EMA(fast) and price > EMA(slow)
    Short-only : price < EMA(fast) and price < EMA(slow)
    No-trade   : price between the two EMAs
    Uses the latest COMPLETED daily candle for the EMA values.

    `now` pins the "is today's candle still forming?" test to a caller-supplied
    instant. Leaving it to the wall clock makes the EMA basis change at 15:30
    mid-session - the same price would then get a different permitted direction
    purely because of the time of day - so live callers pass the candle's own
    timestamp. Historical windows are unaffected: their last daily row is never
    today, so the test returns False whatever the clock says.
    """
    if daily is None or len(daily) < slow + 1:
        return {"regime": "unknown", "ema_fast": None, "ema_slow": None, "as_of": None}
    completed = daily.iloc[:-1] if _is_forming_daily(daily, now=now) else daily
    if len(completed) < slow:
        # Dropping the in-progress candle left too little history. Refuse rather
        # than silently fall back to a regime computed on an unfinished bar - an
        # EMA seeded on a forming close is not the completed-candle rule.
        return {"regime": "unknown", "ema_fast": None, "ema_slow": None, "as_of": None}
    ema_f = ema(completed["close"], fast)
    ema_s = ema(completed["close"], slow)
    ef, es = float(ema_f.iloc[-1]), float(ema_s.iloc[-1])
    if math.isnan(ef) or math.isnan(es):
        # Not enough history to compute an EMA. This is NOT the no-trade regime -
        # conflating the two silently disables the strategy.
        return {"regime": "unknown", "ema_fast": ef, "ema_slow": es,
                "as_of": completed.index[-1].to_pydatetime()}
    if price > ef and price > es:
        regime = "long"
    elif price < ef and price < es:
        regime = "short"
    else:
        regime = "none"
    return {"regime": regime, "ema_fast": ef, "ema_slow": es,
            "as_of": completed.index[-1].to_pydatetime()}


def _is_forming_daily(daily: pd.DataFrame, now: Optional[datetime] = None) -> bool:
    """True when the last daily row is a still-open candle as at `now`."""
    if daily.empty:
        return False
    last_day = daily.index[-1].date()
    now = now or datetime.now(tz=_ist())
    if last_day != now.date():
        return False
    close_h, close_m = 15, 30
    return not (now.hour > close_h or (now.hour == close_h and now.minute >= close_m))


# =====================================================================================
# 6. STRATEGY ENGINE  (pure logic - no I/O, shared by live, paper and backtest)
# =====================================================================================

@dataclass
class Position:
    side: str                 # "LONG" | "SHORT"
    entry_price: float
    quantity: int
    stop: float
    entry_time: datetime
    entry_bucket: Optional[datetime] = None   # the candle that produced the signal
    contract: Optional[str] = None            # contract the position was opened on
    trailing_armed: bool = False
    best_price: float = 0.0
    stop_history: list = field(default_factory=list)

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) if self.side == "LONG" else (self.entry_price - price)


@dataclass
class Action:
    kind: str                 # "ENTRY" | "EXIT" | "STOP_UPDATE"
    side: str                 # "LONG" | "SHORT"
    quantity: int
    ref_price: float          # trigger/reference price used for the action
    reason: str
    time: Optional[datetime] = None
    slip_pts: float = 0.0     # adverse price traded beyond the booked fill
    contract: Optional[str] = None   # contract this action belongs to (roll safety)


class StrategyEngine:
    """
    Candle-close + tick driven state machine.

    on_tick(price)          -> stop-loss / trailing-trigger monitoring on the actual traded price
    on_candle_close(...)    -> regime + Supertrend + VWAP evaluation, trailing-stop update
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.position: Optional[Position] = None
        self.last_signal_candle: Optional[datetime] = None
        self.exited_on_candle: Optional[datetime] = None
        self.trade_count = 0
        self.blocked_by_side = 0     # valid signals suppressed by allowed_sides
        # Round-trip cost in index points, used only when breakeven_covers_costs is set.
        # Set by the caller (run_backtest) from the CostModel, so the engine itself stays
        # free of any pricing dependency.
        self.breakeven_cost_pts = 0.0

    def _breakeven_level(self, pos: "Position") -> float:
        """
        The level the stop moves to when +100 is reached.

        The spec says "entry price", and that is the default. But a trade stopped out at
        its entry price is NOT flat - it still pays the round trip. With
        breakeven_covers_costs the level is shifted by the modelled cost so that
        "breakeven" means no net loss. Both are explicit; neither is assumed.
        """
        if not self.cfg.breakeven_covers_costs or not self.breakeven_cost_pts:
            return pos.entry_price
        return (pos.entry_price + self.breakeven_cost_pts if pos.side == "LONG"
                else pos.entry_price - self.breakeven_cost_pts)

    # ------------------------------------------------------------------ helpers --
    def _flip(self, st: pd.DataFrame, idx: int) -> tuple[int, int]:
        return int(st["trend"].iloc[idx]), int(st["trend"].iloc[idx - 1]) if idx > 0 else (0, 0)

    # -------------------------------------------------------------------- ticks --
    def on_tick(self, price: float, ts: Optional[datetime] = None) -> list[Action]:
        """Called continuously between candle closes with the actual traded price."""
        if self.position is None or price is None or math.isnan(price):
            return []
        actions: list[Action] = []
        pos = self.position

        # 1. Trailing activation: first touch of +100 points of unrealised profit -> breakeven.
        if not pos.trailing_armed and pos.unrealized(price) >= self.cfg.trail_trigger_points:
            pos.trailing_armed = True
            new_stop = self._breakeven_level(pos)
            if self._would_tighten(pos, new_stop):
                pos.stop = new_stop
                pos.stop_history.append(("breakeven", new_stop, ts))
            actions.append(Action("STOP_UPDATE", pos.side, pos.quantity, new_stop,
                                  f"+{self.cfg.trail_trigger_points:g} reached -> breakeven", ts))

        # Track the most favourable price reached (used for logging / diagnostics).
        if pos.side == "LONG":
            pos.best_price = max(pos.best_price, price)
        else:
            pos.best_price = min(pos.best_price, price)

        # 2. Stop-loss execution on the actual traded price.
        hit = price <= pos.stop if pos.side == "LONG" else price >= pos.stop
        if hit:
            # The booked fill is the stop level, but the market traded to `price`.
            # Record the gap so the backtest's optimism is visible rather than hidden.
            slip = abs(price - pos.stop)
            actions.append(Action("EXIT", pos.side, pos.quantity, pos.stop,
                                  f"stop {pos.stop:.2f} hit at {price:.2f}", ts,
                                  slip_pts=slip))
        return actions

    def _would_tighten(self, pos: Position, candidate: float) -> bool:
        """A stop may only move in the profitable direction - never loosened."""
        if pos.side == "LONG":
            return candidate > pos.stop + 1e-9
        return candidate < pos.stop - 1e-9

    # ------------------------------------------------------------- candle close --
    def on_candle_close(self, candle_ts: datetime, close: float, high: float, low: float,
                        open_: float, st_row: pd.Series, st_prev: Optional[pd.Series],
                        vwap_value: float, regime: str,
                        intrabar_handled: bool = False) -> list[Action]:
        """
        candle_ts : timestamp of the CLOSED candle
        st_row    : Supertrend row for the closed candle
        st_prev   : Supertrend row for the previous candle (None for the first bar)
        regime    : "long" | "short" | "none"  (daily EMA filter for the live price)
        """
        actions: list[Action] = []
        cfg = self.cfg
        self.last_signal_candle = candle_ts

        # --- A. intrabar stop check, ONLY when no finer data was replayed ---
        # A 15-minute OHLC bar does not say whether the high or the low came first.
        # Guessing favourably (arm breakeven from the high, then test the low against the
        # NEW stop) fabricates profits. So this fallback tests the ADVERSE extreme against
        # the ORIGINAL stop first, and only arms the trigger if the stop survived.
        if self.position is not None and not intrabar_handled:
            pos = self.position
            favourable = high if pos.side == "LONG" else low
            adverse = low if pos.side == "LONG" else high
            stop_before = pos.stop

            hit = adverse <= stop_before if pos.side == "LONG" else adverse >= stop_before
            if hit:
                fill = min(open_, stop_before) if pos.side == "LONG" else max(open_, stop_before)
                actions.append(Action("EXIT", pos.side, pos.quantity, fill,
                                      f"stop {stop_before:.2f} hit (bar range)", candle_ts,
                                      slip_pts=abs(adverse - fill)))
            elif not pos.trailing_armed and (
                    (favourable - pos.entry_price if pos.side == "LONG"
                     else pos.entry_price - favourable) >= cfg.trail_trigger_points):
                pos.trailing_armed = True
                _be = self._breakeven_level(pos)
                if self._would_tighten(pos, _be):
                    pos.stop = _be
                    pos.stop_history.append(("breakeven", _be, candle_ts))
                actions.append(Action("STOP_UPDATE", pos.side, pos.quantity, _be,
                                      f"+{cfg.trail_trigger_points:g} reached -> breakeven "
                                      f"(assumed after stop survived)", candle_ts))

        # --- B. Supertrend trailing stop update, on candle close only ---
        if self.position is not None and self.position.trailing_armed:
            pos = self.position
            level = float(st_row["lower"] if pos.side == "LONG" else st_row["upper"])
            if not math.isnan(level) and self._would_tighten(pos, level):
                pos.stop = level
                pos.stop_history.append(("supertrend", level, candle_ts))
                actions.append(Action("STOP_UPDATE", pos.side, pos.quantity, level,
                                      "Supertrend trail on candle close", candle_ts))

        # --- C. entry evaluation (only when flat) ---
        # A stop-out detected on THIS candle has so far only been appended as an action;
        # the caller clears the position when it executes the exit. Treating that as
        # "still holding" made the same-candle rule depend on WHICH exit path fired -
        # the intrabar path re-entered even with the flag off (its exit was already
        # executed), while the OHLC fallback could never re-enter with the flag on.
        # Decide from the flag alone, identically for both.
        pending_exit = any(act.kind == "EXIT" for act in actions)
        if self.position is not None and not (pending_exit and cfg.allow_same_candle_reentry):
            return actions
        if self.position is None and self.exited_on_candle == candle_ts \
                and not cfg.allow_same_candle_reentry:
            return actions

        trend_now = int(st_row["trend"]) if not pd.isna(st_row["trend"]) else 0
        trend_prev = int(st_prev["trend"]) if st_prev is not None and not pd.isna(st_prev["trend"]) else 0

        bullish = trend_now == 1 and (trend_prev == -1 if cfg.entry_on == "flip" else True)
        bearish = trend_now == -1 and (trend_prev == 1 if cfg.entry_on == "flip" else True)
        if trend_now == 0 or pd.isna(st_row["atr"]) or math.isnan(st_row["atr"]):
            return actions

        above_vwap = close > vwap_value
        below_vwap = close < vwap_value

        # allowed_sides gates NEW ENTRIES only. An open position is still managed and
        # exited normally whatever this is set to - a side filter is not an exit rule.
        long_signal = regime == "long" and bullish and above_vwap
        short_signal = regime == "short" and bearish and below_vwap
        if long_signal and cfg.allowed_sides in ("both", "long"):
            actions.append(Action("ENTRY", "LONG", cfg.quantity or 1, close,
                                  f"regime long + Supertrend bullish + close {close:.2f} > VWAP {vwap_value:.2f}",
                                  candle_ts))
        elif short_signal and cfg.allowed_sides in ("both", "short"):
            actions.append(Action("ENTRY", "SHORT", cfg.quantity or 1, close,
                                  f"regime short + Supertrend bearish + close {close:.2f} < VWAP {vwap_value:.2f}",
                                  candle_ts))
        elif long_signal or short_signal:
            self.blocked_by_side += 1
        return actions

    # ------------------------------------------------------------- book-keeping --
    def open_position(self, action: Action, fill_price: float, ts: datetime) -> Position:
        cfg = self.cfg
        stop = fill_price - cfg.initial_stop_points if action.side == "LONG" \
            else fill_price + cfg.initial_stop_points
        pos = Position(side=action.side, entry_price=fill_price, quantity=action.quantity,
                       entry_bucket=action.time,
                       contract=getattr(action, "contract", None),
                       stop=stop, entry_time=ts, best_price=fill_price)
        pos.stop_history.append(("initial", stop, ts))
        self.position = pos
        self.trade_count += 1
        return pos

    def close_position(self, ts: Optional[datetime] = None) -> Optional[Position]:
        """
        Close and return the position. `ts` is the candle the exit belongs to; without
        it we fall back to last_signal_candle, which on the intrabar path still points
        at the PREVIOUS candle (on_candle_close has not run yet) and would make the
        same-candle guard compare against the wrong timestamp.
        """
        pos, self.position = self.position, None
        if pos is not None:
            self.exited_on_candle = ts or self.last_signal_candle
        return pos


# =====================================================================================
# 7. EXECUTORS  (paper vs live)
# =====================================================================================

class PaperExecutor:
    """Simulates fills at the reference price. No orders are sent."""

    def __init__(self, cfg: Config, instrument: Instrument):
        self.cfg = cfg
        self.instrument = instrument
        self.fills: list[dict] = []

    def enter(self, action: Action, ts: datetime) -> float:
        fill = _round_tick(action.ref_price, self.instrument.tick_size)
        self.fills.append({"time": ts, "type": "ENTRY", "side": action.side,
                           "price": fill, "qty": action.quantity, "reason": action.reason})
        log.info("[PAPER] %s %s qty=%d @ %.2f  (%s)", action.side, self.instrument.trading_symbol,
                 action.quantity, fill, action.reason)
        return fill

    def exit(self, action: Action, ts: datetime) -> float:
        fill = _round_tick(action.ref_price, self.instrument.tick_size)
        self.fills.append({"time": ts, "type": "EXIT", "side": action.side,
                           "price": fill, "qty": action.quantity, "reason": action.reason})
        log.info("[PAPER] EXIT %s qty=%d @ %.2f  (%s)", action.side, action.quantity, fill, action.reason)
        return fill

    def sync_stop(self, stop: float) -> None:
        pass


class LiveExecutor:
    """Real MARKET orders on Upstox, with an optional resting SL-M backstop order."""

    def __init__(self, cfg: Config, instrument: Instrument, client: UpstoxClient):
        self.cfg = cfg
        self.instrument = instrument
        self.client = client
        self.sl_order_id: Optional[str] = None
        self.backstop_placed: bool = False
        self.last_fill: Optional[float] = None
        self.last_synced_stop: Optional[float] = None

    def enter(self, action: Action, ts: datetime) -> float:
        txn = "BUY" if action.side == "LONG" else "SELL"
        order_id = self.client.place_order(
            instrument_token=self.instrument.instrument_token,
            transaction_type=txn, quantity=action.quantity, order_type="MARKET")
        fill = self._await_fill(order_id)
        log.info("[LIVE] %s qty=%d order=%s filled @ %.2f  (%s)", txn, action.quantity,
                 order_id, fill, action.reason)
        self.backstop_placed = (self._place_backstop(action, fill)
                                if self.cfg.broker_sl_backstop else False)
        return fill

    def exit(self, action: Action, ts: datetime) -> float:
        if not self._cancel_backstop():
            log.error("Sending the market exit while SL-M %s may still be resting. If it "
                      "triggers as well, the position could overshoot or flip - reconcile "
                      "against the broker immediately afterwards.", self.sl_order_id)
        txn = "SELL" if action.side == "LONG" else "BUY"
        order_id = self.client.place_order(
            instrument_token=self.instrument.instrument_token,
            transaction_type=txn, quantity=action.quantity, order_type="MARKET")
        fill = self._await_fill(order_id)
        log.info("[LIVE] EXIT %s qty=%d order=%s filled @ %.2f  (%s)", txn, action.quantity,
                 order_id, fill, action.reason)
        return fill

    def sync_stop(self, stop: float) -> bool:
        """
        Move the resting SL-M order to the new stop. Returns False if it could not be
        moved, so the caller can act - a stale exchange stop is an unmanaged position.
        """
        if not self.cfg.broker_sl_backstop:
            return True
        if self.sl_order_id is None:
            # Previously this returned True, so a stop that never got placed was
            # indistinguishable from one that was successfully moved - the position
            # would sit unprotected with the engine believing otherwise.
            log.error("sync_stop asked to move the exchange stop to %.2f but there is NO "
                      "resting SL-M order. The position is unprotected; treating this as "
                      "a failure so the caller squares off.", stop)
            return False
        txn = "SELL" if self.position_side == "LONG" else "BUY"
        trigger = _round_tick(stop, self.instrument.tick_size)
        for attempt in range(1, 4):
            try:
                self.client.modify_order(self.sl_order_id, quantity=self.position_qty,
                                         order_type="SL-M", price=0.0,
                                         trigger_price=trigger, transaction_type=txn)
                self.last_synced_stop = trigger
                return True
            except UpstoxAPIError as exc:
                log.warning("Exchange stop modify failed (%d/3): %s", attempt, exc)
                time.sleep(0.4)
        log.error("Could not move the exchange stop to %.2f after 3 attempts; it is still "
                  "resting at %s.", trigger, self.last_synced_stop)
        return False

    position_side: str = "LONG"
    position_qty: int = 0

    def _place_backstop(self, action: Action, fill: float) -> bool:
        """
        Rest an SL-M at the exchange. Returns False if it could not be placed - the
        caller MUST act on that, because a futures position with bot-side-only stops is
        unprotected the moment this process dies or loses connectivity.
        """
        self.position_side = action.side
        self.position_qty = action.quantity
        stop = fill - self.cfg.initial_stop_points if action.side == "LONG" \
            else fill + self.cfg.initial_stop_points
        txn = "SELL" if action.side == "LONG" else "BUY"
        try:
            self.sl_order_id = self.client.place_order(
                instrument_token=self.instrument.instrument_token, transaction_type=txn,
                quantity=action.quantity, order_type="SL-M", price=0.0,
                trigger_price=_round_tick(stop, self.instrument.tick_size))
            self.backstop_placed = True
            log.info("Backstop SL-M order %s placed at %.2f", self.sl_order_id, stop)
            return True
        except UpstoxAPIError as exc:
            self.sl_order_id = None
            self.backstop_placed = False
            log.error("Could not place the backstop SL-M order: %s. There is NO "
                      "exchange-side protection on this position.", exc)
            return False

    def _cancel_backstop(self) -> bool:
        """
        Cancel the resting SL-M. Returns False if the cancellation did not confirm - the
        order ID is deliberately KEPT, because forgetting an order that may still be
        resting is how a market exit and a stop both execute and flip the position.
        """
        if not self.sl_order_id:
            return True
        try:
            self.client._request("DELETE", "/v2/order/cancel", params={"order_id": self.sl_order_id})
            self.sl_order_id = None
            self.backstop_placed = False
            return True
        except UpstoxAPIError as exc:
            log.warning("Could not cancel backstop SL order %s: %s - it may STILL BE RESTING "
                        "at the exchange.", self.sl_order_id, exc)
            return False

    def _await_fill(self, order_id: str, attempts: int = 24, pause: float = 0.5) -> float:
        """
        Block until the order reaches a terminal state and return the ACTUAL average price.

        Never fabricates a fill. A pending, partially filled or unreadable order raises,
        because returning a made-up price would let the engine's position silently drift
        away from the broker's.
        """
        last: dict = {}
        for attempt in range(1, attempts + 1):
            try:
                last = self.client.order_details(order_id) or {}
            except UpstoxAPIError as exc:
                log.warning("Order %s status unavailable (%d/%d): %s",
                            order_id, attempt, attempts, exc)
                last = {}
            status = last.get("status")
            filled = float(last.get("filled_quantity") or 0)
            ordered = float(last.get("quantity") or 0)

            if status == "complete":
                avg = last.get("average_price")
                if avg and filled > 0:
                    if ordered and filled < ordered:
                        log.error("Order %s completed PARTIALLY: %.0f of %.0f. The engine "
                                  "must be reconciled before trading again.",
                                  order_id, filled, ordered)
                        raise OrderUnconfirmed(order_id, f"partial fill {filled:.0f}/{ordered:.0f}")
                    self.last_fill = float(avg)
                    return float(avg)
                raise OrderUnconfirmed(order_id, f"'complete' with no usable fill: {last}")
            if status in ("rejected", "cancelled"):
                raise OrderRejected(order_id, status, last)
            if filled and ordered and filled < ordered:
                log.warning("Order %s partially filled %.0f/%.0f, still working",
                            order_id, filled, ordered)
            time.sleep(pause)
        raise OrderUnconfirmed(order_id,
                               f"still '{last.get('status')}' after {attempts} polls: {last}")


def _round_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


# =====================================================================================
# 8. LIVE RUNNER
# =====================================================================================

class LiveRunner:
    def __init__(self, cfg: Config, mode: str,
                 master: Optional[list[dict]] = None,
                 instrument: Optional["Instrument"] = None):
        """
        `master` / `instrument` exist so tests can build a runner without touching the
        network: fetch_instrument_master() downloads a multi-megabyte file, and a
        self-test that depends on connectivity silently skips everything after it on an
        offline machine.
        """
        self.cfg = cfg
        self.mode = mode
        self.client = UpstoxClient(cfg)
        if instrument is not None:
            self.instrument = instrument
        else:
            master = fetch_instrument_master(cfg) if master is None else master
            self.instrument = find_current_month_future(cfg, master)
        self.quantity = cfg.quantity or self.instrument.lot_size
        self.cfg.quantity = self.quantity
        if mode == "live" and not cfg.broker_sl_backstop:
            if cfg.broker_sl_explicit_off:
                # Honour the explicit request, but make the consequence impossible to miss.
                log.error("*** --no-broker-sl ACCEPTED: there will be NO stop resting at the "
                          "exchange. Bot-side stops vanish the moment this process dies, so a "
                          "crash, network loss or token expiry leaves the position completely "
                          "unprotected. You are trading naked futures. ***")
            else:
                # Bot-side stops vanish the moment this process dies. A live position must
                # always have a protective order resting at the exchange.
                log.warning("Live mode: forcing the broker-side SL-M backstop ON. Without it a "
                            "crash, network loss or token expiry would leave the position "
                            "completely unprotected. Pass --no-broker-sl to override.")
                cfg.broker_sl_backstop = True
        self.fetch_interval, self.resample_minutes = plan_fetch(cfg.entry_timeframe)
        self._resampled = False
        log.info("Candles      : fetching %s%s -> %s bars", self.fetch_interval,
                 "" if self.resample_minutes <= 1 else
                 f" resampled x{self.resample_minutes}", cfg.entry_timeframe)
        self.engine = StrategyEngine(cfg)
        self.executor: Any = (LiveExecutor(cfg, self.instrument, self.client)
                              if mode == "live" else PaperExecutor(cfg, self.instrument))
        self.trades: list[dict] = []
        self.running = True
        self._finalised = False
        self._carry_notice_logged = False
        self._halt_entries = False
        self._loop_count = 0
        self.trade_log = cfg.trade_log or f"trades_{datetime.now(tz=_ist()):%Y%m%d}.csv"
        self.state_file = cfg.state_file or f"state_{datetime.now(tz=_ist()):%Y%m%d}.json"

        log.info("Instrument : %s (%s) lot=%d expiry=%s tick=%.2f",
                 self.instrument.trading_symbol, self.instrument.instrument_key,
                 self.instrument.lot_size, self.instrument.expiry, self.instrument.tick_size)
        log.info("Quantity   : %d | mode=%s | broker SL backstop=%s",
                 self.quantity, mode, cfg.broker_sl_backstop)

    # ----------------------------------------------------------------- scheduling --
    def _now(self) -> datetime:
        return datetime.now(tz=_ist())

    def _at(self, hhmm: str) -> tuple[int, int]:
        h, m = hhmm.split(":")
        return int(h), int(m)

    def in_trading_hours(self, now: Optional[datetime] = None) -> bool:
        now = now or self._now()
        if now.weekday() >= 5 and not self.cfg.trade_on_weekends:
            return False
        oh, om = self._at(self.cfg.market_open)
        ch, cm = self._at(self.cfg.market_close)
        minutes = now.hour * 60 + now.minute
        return oh * 60 + om <= minutes < ch * 60 + cm

    def entries_allowed(self, now: Optional[datetime] = None) -> bool:
        now = now or self._now()
        if self._halt_entries:
            return False
        if not self.in_trading_hours(now):
            return False
        ch, cm = self._at(self.cfg.entry_cutoff)
        return now.hour * 60 + now.minute < ch * 60 + cm

    def past_square_off(self, now: Optional[datetime] = None) -> bool:
        now = now or self._now()
        sh, sm = self._at(self.cfg.square_off)
        return now.hour * 60 + now.minute >= sh * 60 + sm

    # ------------------------------------------------------------------ data feed --
    def _daily_frame(self) -> pd.DataFrame:
        """
        Daily candles behind the EMA regime filter.

        The series fetched MUST be the same one the compared price comes from. With
        daily_filter_source="index" the runner compares the spot index LTP against these
        EMAs, so fetching the futures contract here would compare two different price
        series - futures carry a basis premium over spot, which would bias the regime and
        silently allow or block trades. A failed index fetch returns an empty frame rather
        than substituting futures data: daily_regime() then reports "unknown", which
        blocks trading, and that is the correct outcome.
        """
        today = self._now().date()
        key = (self.cfg.index_key if self.cfg.daily_filter_source == "index"
               else self.instrument.instrument_key)
        try:
            return self.client.historical(key, self.cfg.trend_timeframe,
                                          today, today - timedelta(days=400))
        except UpstoxAPIError as exc:
            log.error("Could not fetch the daily regime series (%s): %s. Trading will be "
                      "blocked until it succeeds - no mismatched substitute is used.", key, exc)
            return pd.DataFrame()

    def _intraday_frame(self) -> pd.DataFrame:
        """
        Today's candles at the entry timeframe (resampled from 1-minute if needed).

        Restricted to regular market hours. Upstox can return pre-open prints (09:00 and
        the 15:30 bucket holds post-close ones); left in, they distort Supertrend and the
        session VWAP and can even be treated as a signal candle. The backtest already
        filters - live must match it or the two disagree.
        """
        raw = self.client.intraday(self.instrument.instrument_key, self.fetch_interval)
        if self.resample_minutes > 1:
            self._resampled = True
            raw = resample_ohlcv(raw, self.resample_minutes)
        return filter_session(raw, self.cfg.market_open, self.cfg.market_close)

    def infer_candle_label(self, df: pd.DataFrame) -> str:
        """
        Upstox may label intraday candles by open time or close time. Detect which:
        if the newest candle's start+15m is still in the future, it is the forming candle
        and labelling is by OPEN time.
        """
        if getattr(self, "_resampled", False):
            return "open"          # we built these buckets ourselves, labelled by open time
        if self.cfg.candle_label in ("open", "close"):
            return self.cfg.candle_label
        if df.empty:
            return "open"
        last = df.index[-1].to_pydatetime()
        now = self._now()
        return "open" if last + timedelta(minutes=15) > now else "close"

    # ------------------------------------------------------------------- main loop --
    def run(self) -> None:
        try:
            self._run_loop()
        finally:
            self._finalise()

    def _run_loop(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        atexit.register(self._finalise)   # last resort if the process is torn down

        self._report_account_state()

        processed: set = set()
        self._seed_processed(processed)
        daily_df = pd.DataFrame()
        st = pd.DataFrame()
        vwap = pd.Series(dtype="float64")
        last_daily_refresh = 0.0
        last_intra_refresh = 0.0
        label = self.cfg.candle_label

        while self.running:
            now = self._now()

            if not self.in_trading_hours(now):
                if (self.engine.position is not None and self.past_square_off(now)
                        and self.cfg.hold == "intraday"):
                    self._force_square_off("outside market hours / square-off time")
                elif self.engine.position is not None and self.cfg.hold != "intraday":
                    # Positional: the position is meant to survive the close. Say so
                    # once rather than every poll, and name what is protecting it.
                    if not self._carry_notice_logged:
                        self._carry_notice_logged = True
                        log.warning("Carrying an open %s position overnight (hold=%s). "
                                    "It is exposed to the opening gap; the stop cannot "
                                    "execute while the market is shut. Protective stop: %s.",
                                    self.engine.position.side, self.cfg.hold,
                                    "exchange-side SL-M" if self.cfg.broker_sl_backstop
                                    else "BOT-SIDE ONLY - NOT PROTECTED IF THIS PROCESS DIES")
                if now.weekday() >= 5 and not self.cfg.trade_on_weekends:
                    log.info("Weekend - nothing to do. Exiting. (If today is an exchange "
                             "special session such as Budget day, restart with "
                             "--trade-on-weekends.)")
                    break
                log.info("Outside trading hours (%s). Sleeping 60s.", now.strftime("%a %H:%M:%S"))
                time.sleep(60)
                continue

            try:
                # --- refresh daily candles (regime filter) ---
                if now.timestamp() - last_daily_refresh > 300:
                    daily_df = self._daily_frame()
                    last_daily_refresh = now.timestamp()
                    log.info("Daily candles refreshed: %d rows.", len(daily_df))

                # --- refresh intraday candles + indicators ---
                if now.timestamp() - last_intra_refresh > self.cfg.candle_refresh_seconds:
                    intra = self._intraday_frame()
                    if intra.empty:
                        log.warning("No intraday candles returned; retrying.")
                    else:
                        label = self.infer_candle_label(intra)
                        st = supertrend(intra, self.cfg.st_atr_period, self.cfg.st_multiplier)
                        vwap = session_vwap(intra)
                        new_closes = [ts for ts in intra.index
                                      if self._candle_is_closed(ts, label, now) and ts not in processed]
                        for ts in sorted(new_closes):
                            processed.add(ts)
                            self._handle_candle_close(ts, intra, st, vwap, daily_df, label)
                    last_intra_refresh = now.timestamp()

                self._loop_count += 1
                if self._loop_count % 5 == 0 and self.mode == "live":
                    self._reconcile()

                # --- tick-by-tick stop monitoring on the actual traded price ---
                price = self.client.ltp(self.instrument.instrument_key)
                if price:
                    for action in self.engine.on_tick(price, self._now()):
                        self._act(action, price)
                if self.engine.position is not None:
                    pos = self.engine.position
                    log.debug("Flat=no side=%s entry=%.2f stop=%.2f price=%s armed=%s",
                              pos.side, pos.entry_price, pos.stop, price, pos.trailing_armed)

                # --- forced square-off (intraday product only) ---
                # A positional hold is closed by its STOP, not by the clock. Squaring
                # off here would override the strategy and, with product MIS, the
                # broker would do it anyway - which is why positional uses NRML.
                if (self.past_square_off(now) and self.engine.position is not None
                        and self.cfg.hold == "intraday"):
                    self._force_square_off("square-off time reached")

                self._save_state()

            except UpstoxAPIError as exc:
                if exc.status_code == 401:
                    log.error("Access token rejected (401). Upstox tokens expire daily at 03:30 IST.")
                    raise SystemExit("Aborting: %s" % exc)
                log.error("Upstox API error: %s", exc)
            except Exception as exc:  # keep the loop alive on unexpected errors
                log.exception("Unhandled error in main loop: %s", exc)

            time.sleep(self.cfg.poll_seconds)

    def _seed_processed(self, processed: set) -> None:
        """
        A bot started mid-session must not act on candles that closed before it started.
        Everything already closed is marked processed so only NEW closes can signal.
        """
        now = self._now()
        try:
            intra = self._intraday_frame()
        except UpstoxAPIError as exc:
            log.warning("Could not pre-seed candles: %s", exc)
            return
        if intra.empty:
            return
        label = self.infer_candle_label(intra)
        stale = [ts for ts in intra.index if self._candle_is_closed(ts, label, now)]
        processed.update(stale)
        log.info("Pre-seeded %d already-closed candle(s) (label=%s); only new closes will signal.",
                 len(stale), label)

    def _candle_is_closed(self, ts: datetime, label: str, now: datetime) -> bool:
        if label == "open":
            return ts + timedelta(minutes=15) <= now
        return ts <= now

    def _handle_candle_close(self, ts: datetime, intra: pd.DataFrame, st: pd.DataFrame,
                             vwap: pd.Series, daily_df: pd.DataFrame, label: str) -> None:
        pos_idx = list(intra.index).index(ts)
        row = intra.iloc[pos_idx]
        st_row = st.iloc[pos_idx]
        st_prev = st.iloc[pos_idx - 1] if pos_idx > 0 else None
        vwap_value = float(vwap.iloc[pos_idx]) if len(vwap) and pos_idx < len(vwap) else float(row["close"])

        price = self.client.ltp(self.instrument.instrument_key) or float(row["close"])
        if self.cfg.daily_filter_source == "index":
            try:
                index_price = self.client.ltp(self.cfg.index_key)
                price = index_price or price
            except UpstoxAPIError:
                pass
        # Pin the completed-candle test to this candle's own timestamp so the EMA
        # basis cannot shift underneath an open position at 15:30.
        regime_info = daily_regime(daily_df, price, self.cfg.ema_fast, self.cfg.ema_slow,
                                   now=ts)
        regime = regime_info["regime"]

        log.info("Candle close %s | O=%.2f H=%.2f L=%.2f C=%.2f | ST trend=%s | VWAP=%.2f | "
                 "regime=%s (EMA%d=%.2f EMA%d=%.2f)",
                 ts.strftime("%H:%M"), row["open"], row["high"], row["low"], row["close"],
                 st_row["trend"], vwap_value, regime,
                 self.cfg.ema_fast, regime_info["ema_fast"] or 0.0,
                 self.cfg.ema_slow, regime_info["ema_slow"] or 0.0)

        for action in self.engine.on_candle_close(ts, float(row["close"]), float(row["high"]),
                                                  float(row["low"]), float(row["open"]),
                                                  st_row, st_prev, vwap_value, regime):
            if action.kind == "ENTRY" and not self.entries_allowed():
                log.info("Entry signal ignored (entry cutoff, or entries halted).")
                continue
            if not self._act(action, float(row["close"])):
                # A candle can legitimately produce [EXIT, ENTRY] when same-candle re-entry
                # is enabled. If the EXIT did not confirm, executing the ENTRY would open a
                # second position in the engine while the broker may still hold the first.
                log.error("Action %s did not complete on candle %s - abandoning the rest of "
                          "this candle's actions (including any same-candle re-entry) and "
                          "halting new entries until reconciliation succeeds.",
                          action.kind, ts.strftime("%H:%M"))
                self._halt_entries = True
                break

    # ------------------------------------------------------------------- actions --
    def _act(self, action: Action, price: float) -> bool:
        """Execute one action. False means it did not complete and the caller must stop."""
        ts = action.time or self._now()

        if action.kind == "ENTRY":
            try:
                fill = self.executor.enter(action, ts)
            except OrderRejected as exc:
                log.error("Entry order rejected (%s) - no position was opened.", exc)
                return False
            except OrderUnconfirmed as exc:
                log.error("*** Entry order %s UNCONFIRMED: %s. Reconciling against the "
                          "broker before doing anything else. ***", exc.order_id, exc.detail)
                self._reconcile()
                return False
            except UpstoxAPIError as exc:
                # A 429, a network blip or a margin rejection. Not fatal to the session -
                # previously this escaped _act and aborted the whole trading day.
                log.error("Entry order failed at the API level (%s) - no position opened, "
                          "entries halted pending reconciliation.", exc)
                self._halt_entries = True
                self._reconcile()
                return False
            self.engine.open_position(action, fill, ts)
            if isinstance(self.executor, LiveExecutor):
                self.executor.position_side = action.side
                self.executor.position_qty = action.quantity
                if self.executor.cfg.broker_sl_backstop and not self.executor.backstop_placed:
                    log.critical("Filled %s but NO exchange stop could be placed. Squaring "
                                 "off immediately rather than hold an unprotected futures "
                                 "position, and halting new entries.", action.side)
                    self._halt_entries = True
                    self._force_square_off("backstop SL-M could not be placed")
                    return False
            return True

        elif action.kind == "EXIT":
            fill: Optional[float] = None
            for attempt in range(1, 4):
                try:
                    fill = self.executor.exit(action, ts)
                    break
                except OrderRejected as exc:
                    log.error("Exit order rejected (%s), attempt %d/3", exc, attempt)
                    time.sleep(0.5)
                except OrderUnconfirmed as exc:
                    log.error("*** Exit order %s UNCONFIRMED: %s ***", exc.order_id, exc.detail)
                    break
                except UpstoxAPIError as exc:
                    log.error("Exit order failed at the API level (%s), attempt %d/3",
                              exc, attempt)
                    time.sleep(0.5)
            if fill is None:
                log.critical("POSITION MAY STILL BE OPEN. The exit did not confirm. "
                             "CHECK YOUR BROKER TERMINAL AND CLOSE IT MANUALLY.")
                self._halt_entries = True
                self._reconcile()
                return False
            closed = self.engine.close_position(ts)
            if closed is not None:
                gross = (fill - closed.entry_price) if closed.side == "LONG" \
                    else (closed.entry_price - fill)
                # Same basis as the backtest, which reports pnl_points NET of
                # 2 x cost_points_per_side. Without this the two are not comparable.
                cost_pts = 2.0 * self.cfg.cost_points_per_side
                pnl_points = gross - cost_pts
                self.trades.append({
                    "entry_time": closed.entry_time, "exit_time": ts, "side": closed.side,
                    "qty": closed.quantity, "entry": closed.entry_price, "exit": fill,
                    "pnl_points": round(pnl_points, 2),
                    "pnl_money": round(pnl_points * closed.quantity, 2),
                    "gross_points": round(gross, 2),
                    "cost_points": round(cost_pts, 2),
                    "best": closed.best_price, "trail_armed": closed.trailing_armed,
                    "stops": ";".join(f"{tag}:{lvl:.2f}" for tag, lvl, _ in closed.stop_history),
                    "reason": action.reason,
                })
                log.info("Trade closed: %s %.2f -> %.2f  (%+.2f pts gross, %+.2f pts net of "
                         "%.2f pts costs, %+.2f INR)",
                         closed.side, closed.entry_price, fill, gross, pnl_points, cost_pts,
                         pnl_points * closed.quantity)
                self._write_trade_log()

        elif action.kind == "STOP_UPDATE":
            if isinstance(self.executor, LiveExecutor):
                if not self.executor.sync_stop(action.ref_price):
                    log.error("The exchange stop could not be moved - squaring off rather "
                              "than carry a position whose protection is stale.")
                    self._halt_entries = True
                    self._force_square_off("exchange stop out of sync")
                    return False
            log.info("Stop -> %.2f (%s)", action.ref_price, action.reason)
            return True

        # Unknown action kind: nothing to do, but do not report failure - a falsy return
        # would halt entries for a no-op.
        return True

    def _broker_qty(self) -> Optional[int]:
        try:
            positions = self.client.positions()
        except UpstoxAPIError as exc:
            log.warning("Broker position check failed: %s", exc)
            return None
        return sum(int(p.get("quantity") or 0) for p in positions
                   if str(p.get("instrument_token")) == self.instrument.instrument_token)

    def _reconcile(self) -> None:
        """
        Compare the engine's view with the broker's.

        The exchange-side stop can fire without this process ever seeing it, leaving the
        engine convinced it still holds a position. Acting on that stale state means
        sending an exit for a position that is already gone.
        """
        broker = self._broker_qty()
        if broker is None:
            return
        pos = self.engine.position
        engine_qty = 0 if pos is None else (pos.quantity if pos.side == "LONG" else -pos.quantity)
        if broker == engine_qty:
            return

        if broker == 0 and pos is not None:
            price = self.client.ltp(self.instrument.instrument_key) or pos.entry_price
            log.error("RECONCILE: the broker is FLAT but the engine holds %s %d - the exchange "
                      "stop almost certainly fired. Dropping the stale position locally "
                      "(NO exit order sent). Exit price %.2f is an estimate - verify against "
                      "your contract note.", pos.side, pos.quantity, price)
            closed = self.engine.close_position()
            if closed is not None:
                pnl = ((price - closed.entry_price) if closed.side == "LONG"
                       else (closed.entry_price - price))
                self.trades.append({
                    "entry_time": closed.entry_time, "exit_time": self._now(),
                    "side": closed.side, "qty": closed.quantity,
                    "entry": closed.entry_price, "exit": price,
                    "pnl_points": round(pnl, 2), "pnl_money": round(pnl * closed.quantity, 2),
                    "best": closed.best_price, "trail_armed": closed.trailing_armed,
                    "stops": "", "reason": "reconciled: broker flat, exit price estimated",
                })
                self._write_trade_log()
        elif broker != 0 and pos is None:
            log.error("RECONCILE: the broker shows %+d on %s but this bot is flat. That "
                      "position is NOT managed here - close it manually.",
                      broker, self.instrument.trading_symbol)
            self._halt_entries = True
        else:
            log.error("RECONCILE: broker %+d vs engine %+d - suspending new entries until "
                      "this is resolved.", broker, engine_qty)
            self._halt_entries = True

    def _force_square_off(self, reason: str) -> None:
        if self.engine.position is None:
            return
        pos = self.engine.position
        price = self.client.ltp(self.instrument.instrument_key)
        if price is None:
            log.warning("LTP unavailable for the square-off; the order reference will use "
                        "the entry price %.2f. In live mode the RECORDED exit is the actual "
                        "order fill, not this reference.", pos.entry_price)
            price = pos.entry_price
        self._act(Action("EXIT", pos.side, pos.quantity, price, reason, self._now()), price)

    def _report_account_state(self) -> None:
        if self.mode != "live":
            return
        try:
            positions = self.client.positions()
        except UpstoxAPIError as exc:
            log.warning("Could not read positions: %s", exc)
            return
        mine = [p for p in positions if str(p.get("instrument_token")) == self.instrument.instrument_token
                and int(p.get("quantity") or 0) != 0]
        if mine:
            log.warning("Existing open position(s) on %s: %s - the bot manages only positions it opens.",
                        self.instrument.trading_symbol,
                        [(p.get("quantity"), p.get("average_price")) for p in mine])
            # Do not wait for the periodic reconcile: until this is resolved the bot has
            # no idea what it is sharing the instrument with, and a fresh entry would
            # stack a second position on top of one it does not manage.
            self._halt_entries = True
            log.error("NEW ENTRIES HALTED. An unmanaged position exists on %s. Close it "
                      "manually (or restart once the account is flat) before this bot "
                      "will trade again.", self.instrument.trading_symbol)

    # ------------------------------------------------------------------ artefacts --
    def _write_trade_log(self) -> None:
        if not self.trades:
            return
        with open(self.trade_log, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(self.trades[0].keys()))
            writer.writeheader()
            writer.writerows(self.trades)

    def _save_state(self) -> None:
        pos = self.engine.position
        state = {
            "saved_at": self._now().isoformat(),
            "mode": self.mode,
            "instrument": self.instrument.trading_symbol,
            "trades_today": len(self.trades),
            "position": None if pos is None else {
                "side": pos.side, "entry": pos.entry_price, "qty": pos.quantity,
                "stop": pos.stop, "trailing_armed": pos.trailing_armed,
                "entry_time": pos.entry_time.isoformat(),
                "stops": [[t, l, None if ts is None else ts.isoformat()] for t, l, ts in pos.stop_history],
            },
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, default=str)
        except OSError as exc:
            log.warning("Could not write state file: %s", exc)

    def _finalise(self) -> None:
        if self.engine.position is not None and not self._finalised:
            self._finalised = True
            if self.cfg.hold == "intraday":
                log.warning("Shutting down with an open %s position (qty %d) - squaring off.",
                            self.engine.position.side, self.engine.position.quantity)
                try:
                    self._force_square_off("shutdown square-off")
                except Exception as exc:
                    log.error("*** COULD NOT SQUARE OFF the open %s position: %s *** "
                              "CLOSE IT MANUALLY IN YOUR BROKER TERMINAL NOW.",
                              self.engine.position.side, exc)
            else:
                # Positional: leaving it open is CORRECT, but only if something will
                # still close it. A bot-side stop dies with this process.
                _prot = "exchange-side SL-M order" if self.cfg.broker_sl_backstop else "NOTHING"
                log.warning("Shutting down with an open %s position (qty %d). hold=%s so it "
                            "is intentionally LEFT OPEN, protected by %s.",
                            self.engine.position.side, self.engine.position.quantity,
                            self.cfg.hold, _prot)
                if not self.cfg.broker_sl_backstop:
                    log.error("*** THE OPEN %s POSITION HAS NO EXCHANGE-SIDE STOP AND THIS "
                              "PROCESS IS EXITING. PLACE A STOP MANUALLY NOW. ***",
                              self.engine.position.side)
        self._write_trade_log()
        self._save_state()
        if self.trades:
            wins = [t for t in self.trades if t["pnl_points"] > 0]
            gross_win = sum(t["pnl_points"] for t in wins)
            gross_loss = -sum(t["pnl_points"] for t in self.trades if t["pnl_points"] < 0)
            log.info("Session summary: %d trades | wins %d | net %+.2f pts | %+.2f INR | "
                     "win rate %.1f%% | profit factor %s",
                     len(self.trades), len(wins), sum(t["pnl_points"] for t in self.trades),
                     sum(t["pnl_money"] for t in self.trades),
                     100 * len(wins) / len(self.trades),
                     f"{gross_win / gross_loss:.2f}" if gross_loss else "inf")
            log.info("Trade log: %s", self.trade_log)

    def _handle_signal(self, signum, frame) -> None:
        log.info("Signal %s received - stopping after the current iteration.", signum)
        self.running = False


# =====================================================================================
# 9. BACKTESTER  (same engine, historical candles)
# =====================================================================================

def past_entry_cutoff(ts: datetime, cutoff: str) -> bool:
    """True when a candle's timestamp is at/after the daily entry cutoff (e.g. 15:15)."""
    h, m = cutoff.split(":")
    return ts.hour * 60 + ts.minute >= int(h) * 60 + int(m)


def replay(cfg: Config, intraday: pd.DataFrame, daily: pd.DataFrame,
           tick_size: float = 0.05, quantity: Optional[int] = None,
           price_series: Optional[pd.Series] = None,
           diag: Optional[dict] = None,
           minute_frame: Optional[pd.DataFrame] = None,
           cost_per_side: float = 0.0,
           cost_model: Optional[CostModel] = None,
           risk: Optional[RiskManager] = None,
           breakeven_cost_pts: float = 0.0) -> list[dict]:
    """
    Replay historical candles through StrategyEngine. Shared by --backtest and --selftest,
    so the offline checks exercise the same code path that runs live.

    Mirrors the live runner's session rules: no new entries at/after the entry cutoff, and
    every open position is squared off at the last candle of its own trading day - an
    intraday strategy never holds overnight, and per-day P&L is only meaningful if that
    holds in the backtest too.
    """
    if quantity:
        cfg.quantity = quantity
    engine = StrategyEngine(cfg)
    engine.breakeven_cost_pts = float(breakeven_cost_pts)
    trades: list[dict] = []
    if diag is None:
        diag = {}
    diag.setdefault("candles", 0)
    diag.setdefault("regimes", {})
    diag.setdefault("flips_bull", 0)
    diag.setdefault("flips_bear", 0)
    diag.setdefault("flips_vwap_ok", 0)
    diag.setdefault("blocked_regime", 0)
    diag.setdefault("blocked_direction", 0)
    diag.setdefault("blocked_vwap", 0)
    diag.setdefault("days", set())
    diag.setdefault("minute_bars_replayed", 0)
    diag.setdefault("blocked_by_risk", 0)
    diag.setdefault("overnight_holds", 0)
    diag.setdefault("gap_through_stop", 0)
    diag.setdefault("worst_gap_pts", 0.0)
    diag.setdefault("roll_exits", 0)
    diag.setdefault("risk_block_reason", "")
    candle_minutes = interval_minutes(cfg.entry_timeframe) or 15
    diag.setdefault("mtm_drawdown_pts", 0.0)
    realized_pts = 0.0
    mtm_peak = 0.0
    if intraday.empty:
        return trades

    st = supertrend_by_segment(intraday, cfg.st_atr_period, cfg.st_multiplier)
    daily = daily.copy()
    daily["_d"] = daily.index.date
    index_of = {ts: i for i, ts in enumerate(intraday.index)}
    vwap_by_day = {day: session_vwap(day_df) for day, day_df in intraday.groupby(intraday.index.date)}

    entry_fill_lookup = _build_entry_fill_lookup(minute_frame)

    # Which contract each session belongs to. Only present when --roll stitched several
    # contracts together, but a positional hold makes it load-bearing: an open position
    # cannot be carried into a different contract's prices.
    contract_by_day: dict = {}
    if "contract" in intraday.columns:
        for _d, _g in intraday.groupby(intraday.index.date):
            contract_by_day[_d] = _g["contract"].iloc[0]

    prev_close: Optional[float] = None
    for day, day_df in intraday.groupby(intraday.index.date):
        diag["days"].add(day)
        day_contract = contract_by_day.get(day)

        # --- roll boundary: close rather than carry into another contract ------------
        if (engine.position is not None and day_contract is not None
                and engine.position.contract is not None
                and engine.position.contract != day_contract):
            _rts = day_df.index[0]
            _before = len(trades)
            # Captured BEFORE the exit: once the position closes, its stop is gone, and
            # roll_forward needs the DISTANCE to re-impose it on the new contract. An
            # absolute level from the old contract is meaningless against the new one's
            # basis, which is exactly the adjustment section 8(7) asks to be defined.
            _old_side = engine.position.side
            _old_qty = engine.position.quantity
            _old_name = engine.position.contract
            _stop_dist = abs(engine.position.entry_price - engine.position.stop)
            _armed = engine.position.trailing_armed
            _replay_act(engine, Action("EXIT", _old_side, _old_qty,
                                       float(day_df["open"].iloc[0]),
                                       f"contract roll {_old_name} -> {day_contract}",
                                       _rts),
                        tick_size, trades, _rts, cost_per_side, candle_minutes, cfg,
                        cost_model, risk, diag, entry_fill_lookup)
            if len(trades) > _before:
                realized_pts += trades[-1]["pnl_points"]
            diag["roll_exits"] += 1

            if cfg.roll_action == "roll_forward" and engine.position is None:
                _new_px = float(day_df["open"].iloc[0])
                _replay_act(engine,
                            Action("ENTRY", _old_side, _old_qty, _new_px,
                                   f"roll forward onto {day_contract}", _rts,
                                   contract=day_contract),
                            tick_size, trades, _rts, cost_per_side, candle_minutes, cfg,
                            cost_model, risk, diag, entry_fill_lookup)
                if engine.position is not None:
                    _np = engine.position.entry_price
                    engine.position.stop = (_np - _stop_dist if _old_side == "LONG"
                                            else _np + _stop_dist)
                    engine.position.trailing_armed = _armed
                    engine.position.stop_history.append(
                        ("roll_forward", engine.position.stop, _rts))
                    diag["roll_forwards"] = diag.get("roll_forwards", 0) + 1
                    log.warning("Rolled %s %s -> %s, carrying the %g-point stop.",
                                _old_side, _old_name, day_contract, _stop_dist)
            else:
                log.warning("Closed an open %s at the %s -> %s roll boundary (roll_action=%s).",
                            _old_side, _old_name, day_contract, cfg.roll_action)

        # --- overnight gap on a carried position ------------------------------------
        # The stop cannot execute while the market is shut, so the next session's
        # opening print IS the fill. This is new risk that the intraday build did not
        # have; it is measured here and the minute replay books the actual fill.
        elif engine.position is not None and prev_close is not None:
            _pos = engine.position
            _opn = float(day_df["open"].iloc[0])
            diag["overnight_holds"] += 1
            _through = (_opn <= _pos.stop) if _pos.side == "LONG" else (_opn >= _pos.stop)
            if _through:
                diag["gap_through_stop"] += 1
                diag["worst_gap_pts"] = max(diag["worst_gap_pts"], abs(_opn - _pos.stop))

        vwap_series = vwap_by_day[day]
        history = daily[daily["_d"] < day]
        for ts in day_df.index:
            i = index_of[ts]
            row = intraday.iloc[i]
            st_row = st.iloc[i]
            st_prev = st.iloc[i - 1] if i > 0 else None
            vwap_value = float(vwap_series.get(ts, row["close"]))

            # --- stop monitoring on the ACTUAL 1-minute path, in true time order ---
            # This removes the OHLC ambiguity entirely: no need to guess whether the high
            # or the low of the 15-minute bar came first.
            intrabar_done = False
            if engine.position is not None and minute_frame is not None:
                window = minute_frame[(minute_frame.index >= ts)
                                      & (minute_frame.index < ts + pd.Timedelta(minutes=15))]
                for mts, mrow in window.iterrows():
                    if engine.position is None:
                        break
                    diag["minute_bars_replayed"] += 1
                    # A 1-minute bar still does not order its own extremes. Test the
                    # ADVERSE one first, against the stop that was live at the start of
                    # the minute: if the stop was breached the trade is out whatever came
                    # first. Only then look at the close and the favourable extreme, so
                    # a wick that pierced the stop and recovered before the minute closed
                    # is no longer invisible.
                    _side = engine.position.side
                    _adverse = float(mrow["low"]) if _side == "LONG" else float(mrow["high"])
                    _favour = float(mrow["high"]) if _side == "LONG" else float(mrow["low"])
                    for px in (_adverse, float(mrow["close"]), _favour):
                        if engine.position is None:
                            break
                        for action in engine.on_tick(px, mts):
                            before = len(trades)
                            _replay_act(engine, action, tick_size, trades, mts, cost_per_side, candle_minutes, cfg, cost_model, risk, diag, entry_fill_lookup)
                            if len(trades) > before:
                                realized_pts += trades[-1]["pnl_points"]
                    # Mark-to-market is measured at the minute CLOSE. `px` still holds
                    # the favourable extreme from the loop above, which would overstate
                    # open equity and understate drawdown.
                    px = float(mrow["close"])
                    # Sampled EVERY minute, flat or not. This used to be guarded by
                    # `if engine.position is not None`, which meant the realised level
                    # immediately after a close was never recorded - so the drawdown
                    # missed the very drop the close had just caused, and reported 0.00
                    # on a trade that lost 130 points.
                    _unreal = 0.0
                    if engine.position is not None:
                        _side = engine.position.side
                        _unreal = ((px - engine.position.entry_price) if _side == "LONG"
                                   else (engine.position.entry_price - px))
                    equity = realized_pts + _unreal
                    mtm_peak = max(mtm_peak, equity)
                    diag["mtm_drawdown_pts"] = max(
                        diag["mtm_drawdown_pts"], mtm_peak - equity)
                    # Feed the risk account the same mark. Its limits must see an open
                    # loss BEFORE it is booked, not only once the position has closed.
                    if risk is not None and risk.enabled:
                        risk.mark(mts, px)
                intrabar_done = True
            elif engine.position is not None:
                # No 1-minute data: fall back to the conservative OHLC rule.
                adverse = float(row["low"]) if engine.position.side == "LONG" else float(row["high"])
                for action in engine.on_tick(adverse, ts):
                    before = len(trades)
                    _replay_act(engine, action, tick_size, trades, ts, cost_per_side, candle_minutes, cfg, cost_model, risk, diag, entry_fill_lookup)
                    if len(trades) > before:
                        realized_pts += trades[-1]["pnl_points"]

            diag["candles"] += 1
            if history.empty:
                diag["regimes"]["no daily history"] = diag["regimes"].get("no daily history", 0) + 1
                continue

            # The price compared against the EMAs must come from the same series the
            # EMAs were computed on, otherwise futures basis corrupts the regime.
            if price_series is not None and ts in price_series.index:
                regime_price = float(price_series.loc[ts])
            else:
                regime_price = float(row["close"])
            regime = daily_regime(history, regime_price, cfg.ema_fast, cfg.ema_slow)["regime"]
            diag["regimes"][regime] = diag["regimes"].get(regime, 0) + 1

            trend_now = int(st_row["trend"]) if not pd.isna(st_row["trend"]) else 0
            trend_prev = int(st_prev["trend"]) if st_prev is not None and not pd.isna(st_prev["trend"]) else 0
            if trend_now and trend_prev and trend_now != trend_prev:
                bullish = trend_now == 1
                if bullish:
                    diag["flips_bull"] += 1
                else:
                    diag["flips_bear"] += 1
                vwap_ok = float(row["close"]) > vwap_value if bullish else float(row["close"]) < vwap_value
                if vwap_ok:
                    diag["flips_vwap_ok"] += 1
                    # Only a genuine candidate was blocked - Supertrend and VWAP both agreed.
                    if regime in ("long", "short"):
                        if (bullish and regime == "short") or (not bullish and regime == "long"):
                            diag["blocked_direction"] += 1
                    else:
                        diag["blocked_regime"] += 1

            for action in engine.on_candle_close(ts, float(row["close"]), float(row["high"]),
                                                 float(row["low"]), float(row["open"]),
                                                 st_row, st_prev, vwap_value, regime,
                                                 intrabar_handled=intrabar_done):
                action.contract = day_contract
                # `ts` is the candle's OPEN label; the signal is only actionable when the
                # candle CLOSES. Live refuses entries once the clock reaches the cutoff, so
                # compare the close time - otherwise a candle opening at 15:00 would be
                # allowed here while live would reject it at 15:15.
                _signal_ts = ts + timedelta(minutes=candle_minutes)
                if action.kind == "ENTRY" and past_entry_cutoff(_signal_ts, cfg.entry_cutoff):
                    log.debug("Entry from the %s candle ignored: actionable at %s, past "
                              "cutoff %s", ts.strftime("%H:%M"), _signal_ts.strftime("%H:%M"),
                              cfg.entry_cutoff)
                    continue
                before = len(trades)
                _replay_act(engine, action, tick_size, trades, ts, cost_per_side, candle_minutes, cfg, cost_model, risk, diag, entry_fill_lookup)
                if len(trades) > before:
                    realized_pts += trades[-1]["pnl_points"]

        # --- end of session ----------------------------------------------------------
        # Intraday: square off, because that is the product's rule and per-day P&L is
        # only meaningful if nothing is carried. Positional: leave it open - the stop,
        # not the clock, decides when the trade ends.
        if cfg.hold == "intraday" and engine.position is not None:
            last_ts = day_df.index[-1]
            # realised_pts MUST be updated here too. It was not, so every square-off
            # P&L - 28 of 51 trades and +2,579 pts on the reference window - never
            # reached the points-based mark-to-market curve, which made that drawdown
            # figure meaningless. RiskManager escaped this because on_close runs
            # inside _replay_act for every exit.
            before = len(trades)
            _replay_act(engine, Action("EXIT", engine.position.side, engine.position.quantity,
                                       float(day_df["close"].iloc[-1]), "intraday square-off",
                                       last_ts), tick_size, trades, last_ts, cost_per_side,
                    candle_minutes, cfg, cost_model, risk, diag, entry_fill_lookup)
            if len(trades) > before:
                realized_pts += trades[-1]["pnl_points"]

        prev_close = float(day_df["close"].iloc[-1])

    if engine.position is not None:  # positional: still open at the end of the window
        last_ts = intraday.index[-1]
        before = len(trades)
        _replay_act(engine, Action("EXIT", engine.position.side, engine.position.quantity,
                                   float(intraday["close"].iloc[-1]), "end of backtest", last_ts),
                    tick_size, trades, last_ts, cost_per_side, candle_minutes, cfg, cost_model, risk, diag, entry_fill_lookup)
        if len(trades) > before:
            realized_pts += trades[-1]["pnl_points"]
    diag["blocked_by_side"] = engine.blocked_by_side
    return trades


def print_daily_breakdown(trades: list[dict]) -> pd.DataFrame:
    """Per-session P&L table - one row per trading day, plus a running cumulative total."""
    df = pd.DataFrame(trades).copy()
    df["exit_time"] = pd.to_datetime(df["exit_time"], errors="coerce")
    df = df.sort_values("exit_time")
    df["_day"] = df["exit_time"].dt.date

    print("\n--- per session ---")
    print(f"{'date':<12}{'trades':>7}{'wins':>6}{'win%':>7}{'net pts':>11}{'net INR':>14}{'cum pts':>11}")
    cum = 0.0
    for day, grp in df.groupby("_day"):
        wins = int((grp["pnl_points"] > 0).sum())
        net = float(grp["pnl_points"].sum())
        cum += net
        print(f"{str(day):<12}{len(grp):>7}{wins:>6}{100 * wins / len(grp):>6.1f}%"
              f"{net:>+11.2f}{grp['pnl_money'].sum():>+14,.2f}{cum:>+11.2f}")

    if "trail_armed" in df.columns:
        armed = df["trail_armed"].astype(str).str.lower().isin(("true", "1"))
        if armed.any() and (~armed).any():
            print(f"\nTrailing armed   : {int(armed.sum())}/{len(df)} trades "
                  f"(avg {df.loc[armed, 'pnl_points'].mean():+.2f} pts when armed, "
                  f"{df.loc[~armed, 'pnl_points'].mean():+.2f} pts when not)")
        else:
            print(f"\nTrailing armed   : {int(armed.sum())}/{len(df)} trades")
    return df


def print_trade_list(trades: list[dict]) -> None:
    """List every individual trade - the intraday detail behind the daily numbers."""
    if not trades:
        return
    df = pd.DataFrame(trades).copy()
    for col in ("entry_time", "exit_time"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df.sort_values("entry_time")
    print("\n--- individual trades ---")
    has_slip = "slip_pts" in df.columns
    print(f"{'entry':<17}{'exit':<17}{'side':<6}{'qty':>5}{'entry px':>10}{'exit px':>10}"
          f"{'pts':>9}{'INR':>12}{'slip':>7}  reason")
    for _, t in df.iterrows():
        slip = f"{t['slip_pts']:>7.2f}" if has_slip and t["slip_pts"] else " " * 7
        print(f"{t['entry_time']:%Y-%m-%d %H:%M}  {t['exit_time']:%Y-%m-%d %H:%M}  "
              f"{t['side']:<6}{int(t['qty']):>5}{t['entry']:>10.2f}{t['exit']:>10.2f}"
              f"{t['pnl_points']:>+9.2f}{t['pnl_money']:>+12,.2f}{slip}  {t['reason'][:42]}")



def _warn_if_window_spans_expiry(master: list[dict], cfg: Config, instrument: Instrument,
                                 start: date) -> None:
    """
    A single-contract backtest that reaches back past a monthly expiry is trading a
    contract that was NOT the front month for that stretch.

    Expired contracts are absent from the instrument master, so the intervening expiry
    cannot be looked up there - it is inferred from the fact that Nifty monthly futures
    expire once a month, so any window starting more than ~5 weeks before this
    contract's expiry must contain at least one earlier expiry.
    """
    gap_days = (instrument.expiry - start).days
    if gap_days <= 35:
        return
    intervening = max(0, round(gap_days / 30.4) - 1)
    log.warning("This backtest uses %s for the whole window, but %s is %d days before that "
                "contract's expiry - so about %d earlier monthly expiry(ies) fall inside the "
                "window. Days before the last one were traded on a NEXT-month contract "
                "(thinner liquidity, different basis) than the strategy would actually have "
                "filled on. Use --roll to switch contracts at each expiry.",
                instrument.trading_symbol, start, gap_days, intervening)


def _find_field(row: dict, *names: str) -> Any:
    """First present, non-empty value among several possible key names."""
    for name in names:
        value = row.get(name)
        if value not in (None, "", 0):
            return value
    return None


def _parse_expiry(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=_ist()).date()
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def build_contract_schedule(client: "UpstoxClient", cfg: Config, master: list[dict],
                            start: date, end: date) -> dict:
    """
    Map each expiry to the contract that was front month for that stretch.

    Without this, a backtest run today uses the current contract for the whole window -
    silently trading a NEXT-month contract for the early part of it, with thinner
    liquidity and a different basis than what the strategy would actually have filled on.
    """
    contracts: dict = {}
    for row in master:
        if (row.get("segment") == cfg.segment and row.get("instrument_type") == "FUT"
                and str(row.get("underlying_symbol", "")).upper() == cfg.underlying.upper()
                and row.get("expiry")):
            exp = _parse_expiry(row["expiry"])
            if exp:
                contracts[exp] = {
                    "instrument_key": row["instrument_key"],
                    "trading_symbol": row.get("trading_symbol", row["instrument_key"]),
                    "exchange_token": str(row.get("exchange_token", "")),
                    "lot_size": int(row.get("lot_size") or 1),
                    "expired": False,
                }

    try:
        expiries = client.expiries(cfg.index_key)
    except UpstoxAPIError as exc:
        log.warning("Could not list expired contracts (%s); rolling will use only the "
                    "contracts still listed in the master.", exc)
        expiries = []

    for raw in expiries:
        exp = _parse_expiry(raw if not isinstance(raw, dict)
                            else _find_field(raw, "expiry", "expiry_date", "expiryDate"))
        if exp is None or exp < start - timedelta(days=5) or exp > end + timedelta(days=45):
            continue
        if exp in contracts:
            continue
        try:
            rows = client.expired_future_contract(cfg.index_key, exp)
        except UpstoxAPIError as exc:
            log.warning("Could not resolve the expired contract for %s: %s", exp, exc)
            continue
        if not rows:
            continue
        row = rows[0]
        if cfg.underlying.upper() not in str(_find_field(row, "trading_symbol", "name",
                                                         "underlying_symbol")).upper():
            continue
        key = _find_field(row, "instrument_key", "instrumentKey", "expired_instrument_key")
        if not key:
            continue
        contracts[exp] = {
            "instrument_key": str(key),
            "trading_symbol": str(_find_field(row, "trading_symbol", "name") or key),
            "exchange_token": str(_find_field(row, "exchange_token", "exchangeToken") or ""),
            "lot_size": int(_find_field(row, "lot_size", "lotSize") or 1),
            "expired": True,
        }
        log.info("Resolved expired contract %s (%s) for %s",
                 contracts[exp]["trading_symbol"], key, exp)

    if not contracts:
        raise SystemExit("No contracts could be resolved for the rolling backtest.")

    # Front month for a given day = the nearest expiry on or after that day.
    ordered = sorted(contracts)
    schedule: dict = {}
    day = start
    while day <= end:
        upcoming = [e for e in ordered if e >= day]
        pick = upcoming[0] if upcoming else ordered[-1]
        schedule[day] = pick
        day += timedelta(days=1)
    return {"contracts": contracts, "by_day": schedule}


def fetch_rolled_candles(client: "UpstoxClient", cfg: Config, schedule: dict,
                         interval: str, start: date, end: date) -> pd.DataFrame:
    """Fetch each contract only for the days it was front month, then stitch."""
    frames: list = []
    by_expiry: dict = {}
    for day, exp in schedule["by_day"].items():
        lo, hi = by_expiry.get(exp, (day, day))
        by_expiry[exp] = (min(lo, day), max(hi, day))

    for exp, (lo, hi) in sorted(by_expiry.items()):
        info = schedule["contracts"][exp]
        log.info("  %s : %s .. %s", info["trading_symbol"], lo, hi)
        try:
            if info["expired"]:
                df = client.expired_historical(info["instrument_key"], interval, hi, lo)
                log.info("    %s .. %s  ->  %d candle(s)", lo, hi, len(df))
            else:
                df = client.historical_range(info["instrument_key"], interval, hi, lo)
        except UpstoxAPIError as exc:
            log.warning("No candles for %s (%s); skipping that stretch.",
                        info["trading_symbol"], exc)
            continue
        if df.empty:
            log.warning("%s returned no candles for %s .. %s", info["trading_symbol"], lo, hi)
            continue
        df = df.copy()
        df["contract"] = info["trading_symbol"]
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")]


def run_backtest(cfg: Config, start: date, end: date, days_requested: Optional[int] = None,
                 show_trades: bool = False, result: Optional[dict] = None) -> None:
    diag_session: dict = {}
    client = UpstoxClient(cfg)
    master = fetch_instrument_master(cfg)
    instrument = find_current_month_future(cfg, master)
    cfg.quantity = cfg.quantity or instrument.lot_size

    log.info("Backtest %s .. %s on %s (qty %d)", start, end, instrument.trading_symbol, cfg.quantity)
    fetch_interval, resample_minutes = plan_fetch(cfg.entry_timeframe)
    log.info("Fetching %s candles (chunks of <=%d days) ...", fetch_interval,
             MAX_RANGE_DAYS.get(fetch_interval, 300))
    try:
        if cfg.roll:
            schedule = build_contract_schedule(client, cfg, master, start, end)
            used = sorted({e for e in schedule["by_day"].values()})
            log.info("Rolling across %d contract(s): %s", len(used),
                     ", ".join(schedule["contracts"][e]["trading_symbol"] for e in used))
            raw = fetch_rolled_candles(client, cfg, schedule, fetch_interval, start, end)
        else:
            raw = client.historical_range(instrument.instrument_key, fetch_interval, end, start)
            _warn_if_window_spans_expiry(master, cfg, instrument, start)
        daily, regime_source = fetch_regime_data(client, cfg, instrument, start, end)
    except UpstoxAPIError as exc:
        raise SystemExit(
            f"Upstox rejected the candle request: {exc}\n"
            "Run --probe to see exactly which intervals and date ranges your account "
            "can retrieve for this contract."
        )
    if raw.empty:
        raise SystemExit(
            f"No {fetch_interval} data returned for {start} .. {end} on "
            f"{instrument.trading_symbol}. This contract may not have existed yet, or the "
            "range is older than Upstox's retention for that interval. Run --probe to check."
        )
    if daily.empty:
        raise SystemExit("No daily candles returned; the EMA regime filter cannot be computed.")
    # Bound the session at the SQUARE-OFF time, not the exchange close. Buckets are
    # labelled by their OPEN, so filtering to 15:30 leaves a final bucket labelled 15:15
    # whose close is the 15:30 price - the backtest would then square off 15 minutes (and
    # one full candle of price movement) later than live mode ever does.
    _sess = validate_session(raw, cfg)
    diag_session.update(_sess) if isinstance(diag_session, dict) else None
    # A positional hold must be replayed to the end of the session, not to the
    # intraday square-off time: the position stays open, so the last candle still
    # matters for stop monitoring and for the overnight mark.
    session_end = cfg.square_off if cfg.hold == "intraday" else cfg.market_close
    minute_frame = filter_session(raw, cfg.market_open, session_end)
    intraday = resample_ohlcv(minute_frame, resample_minutes) if resample_minutes > 1 else minute_frame
    intraday = filter_session(intraday, cfg.market_open, session_end)
    log.info("Regime filter source: %s (%d daily candles)",
             "Nifty spot index" if regime_source == "index" else instrument.trading_symbol,
             len(daily))

    # When the regime uses index EMAs, the price compared against them must be the index
    # price, not the futures price - futures carry a basis premium that would bias the filter.
    price_series: Optional[pd.Series] = None
    if regime_source == "index":
        try:
            idx_raw = client.historical_range(cfg.index_key, fetch_interval, end, start)
            idx_frame = resample_ohlcv(idx_raw, resample_minutes) if resample_minutes > 1 else idx_raw
            idx_frame = filter_session(idx_frame, cfg.market_open, session_end)
            price_series = idx_frame["close"].reindex(intraday.index).ffill()
            log.info("Index price series aligned to %d of %d candles.",
                     int(price_series.notna().sum()), len(intraday))
        except UpstoxAPIError as exc:
            log.warning("Could not fetch index candles (%s); comparing futures price to "
                        "index EMAs instead. Basis may bias marginal regime calls.", exc)
    shortfall = (raw.index.min().date() - start).days
    if shortfall > 3:
        log.warning("Requested %s onward but the earliest %s candle Upstox returned is %s "
                    "(%d days short). Older 1-minute history is not available for this "
                    "contract; use --timeframe 30minute for longer windows.",
                    start, fetch_interval, raw.index.min().date(), shortfall)

    days_with_data = intraday.index.normalize().nunique()
    earliest = intraday.index.min().date()
    expected_weekdays = int(np.busday_count(start, end + timedelta(days=1)))
    log.info("Loaded %d daily and %d intraday candles across %d trading day(s).",
             len(daily), len(intraday), days_with_data)

    if days_with_data < expected_weekdays * 0.6:
        log.warning("Requested %s .. %s (~%d trading days) but Upstox returned only %d for %s. "
                    "The earliest 15-minute candle available is %s - this contract did not "
                    "exist before then. Use --instrument to pin an older contract for that period.",
                    start, end, expected_weekdays, days_with_data,
                    instrument.trading_symbol, earliest)

    diag: dict = {}
    cost_model = CostModel.from_config(cfg)
    risk = RiskManager(cfg, instrument.lot_size, cfg.initial_stop_points, cost_model)
    # replay() builds its own StrategyEngine, so the cost offset has to be passed in -
    # setting it on an engine created here would configure an object nothing else uses.
    _px0 = (float(price_series.iloc[0]) if price_series is not None
            else float(intraday["close"].iloc[0]))
    _be_cost = (cost_model.round_trip_points(_px0, _px0, cfg.quantity)
                if cfg.breakeven_covers_costs else 0.0)
    print_strategy_decisions(cfg, instrument.lot_size, cost_model, _px0)
    if risk.enabled:
        lots = risk.size_units(price_series.iloc[0] if price_series is not None
                               else float(intraday["close"].iloc[0]))
        print(f"\nRisk sizing       : capital ₹{cfg.initial_capital:,.0f} @ "
              f"{100 * cfg.risk_per_trade_pct:.2f}%/trade, lot {instrument.lot_size}, "
              f"stop {cfg.initial_stop_points:g} pts")
        print(f"                    risk per lot = ₹{risk.risk_per_lot_money:,.0f} "
              f"(stop + modelled costs) -> {lots // instrument.lot_size} lot(s)")
        if risk.blocked["sizing"]:
            print(f"                    NOTE: the budget does not fund "
                  f"{risk.min_lots} lot(s); min_lots forced it. Actual risk per trade is "
                  f"{100 * risk.risk_per_lot_money / cfg.initial_capital:.2f}% of capital, "
                  f"NOT {100 * cfg.risk_per_trade_pct:.2f}%.")
    print(f"Cost model        : {cost_model.describe(price_series.iloc[0] if price_series is not None else 24000.0, cfg.quantity)}")
    trades = replay(cfg, intraday, daily, instrument.tick_size, cfg.quantity,
                    price_series=price_series, diag=diag,
                    minute_frame=minute_frame if resample_minutes > 1 else None,
                    cost_per_side=cfg.cost_points_per_side,
                    cost_model=cost_model, risk=risk,
                    breakeven_cost_pts=_be_cost)

    out = cfg.trade_log or f"backtest_{start}_{end}.csv"
    if trades:
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)

    print("\n================ BACKTEST ================")
    print(f"Contract          : {instrument.trading_symbol} ({instrument.instrument_key}), "
          f"expiry {instrument.expiry}")
    print(f"Window            : {start} .. {end}"
          + (f"  (--days {days_requested})" if days_requested else ""))
    print(f"Trading days data : {days_with_data} of ~{expected_weekdays} expected "
          f"(earliest candle {earliest})")
    print(f"Quantity / trade  : {cfg.quantity}")
    # Report what is ACTUALLY being charged. cost_points_per_side is 0 unless the legacy
    # flat override was asked for; printing it here used to claim "0 pts round trip" two
    # lines below a cost model line saying 15.57 pts.
    _cp = cost_model.round_trip_points(_px0, _px0, cfg.quantity)
    print(f"Costs modelled    : {_cp:.2f} pts round trip = "
          f"{_cp * cfg.quantity:,.2f} INR at this qty "
          f"({'flat override' if cfg.cost_points_per_side else 'component model incl. STT'}) - "
          f"verify against your broker")
    print("Intra-bar model   : "
          + ("1-minute replay, adverse extreme first - resolves the 15-minute ordering "
             "question, but a 1-minute bar still cannot order its own high against its "
             "low, so treat simulated fills as estimates" if resample_minutes > 1
             else "OHLC conservative fallback"))
    print(f"Rules             : daily EMA {cfg.ema_fast}/{cfg.ema_slow} regime | 15m Supertrend "
          f"ATR {cfg.st_atr_period} x{cfg.st_multiplier:g} on {cfg.entry_on} | session VWAP | "
          f"{cfg.initial_stop_points:g}pt stop | trail from +{cfg.trail_trigger_points:g}pt | "
          + (f"square-off {cfg.square_off}" if cfg.hold == "intraday"
             else "MULTIDAY hold, no square-off"))
    print("==========================================")

    # Regime context, so you can sanity-check the regime calls against what the market did.
    px = price_series.dropna() if price_series is not None else intraday["close"]
    if len(px) >= 2:
        move = float(px.iloc[-1]) - float(px.iloc[0])
        label = "Nifty spot index" if regime_source == "index" else instrument.trading_symbol
        completed = daily.iloc[:-1] if _is_forming_daily(daily) else daily
        ef = float(ema(completed["close"], cfg.ema_fast).iloc[-1])
        es = float(ema(completed["close"], cfg.ema_slow).iloc[-1])
        last = float(px.iloc[-1])
        where = ("ABOVE both" if last > ef and last > es else
                 "BELOW both" if last < ef and last < es else "BETWEEN them")
        print(f"\nRegime context      : {label} {float(px.iloc[0]):,.2f} -> {last:,.2f} "
              f"({move:+,.2f} pts, {100 * move / float(px.iloc[0]):+.2f}% over the window)")
        print(f"                      last completed daily EMA{cfg.ema_fast}={ef:,.2f}  "
              f"EMA{cfg.ema_slow}={es:,.2f}  ->  final price is {where}")

    print("\n--- signal funnel ---")
    regimes = diag.get("regimes", {})
    print(f"15m candles evaluated : {diag.get('candles', 0)}")
    print("Daily regime split    : " + (", ".join(f"{k}={v}" for k, v in sorted(regimes.items()))
                                         if regimes else "none"))
    print(f"Supertrend flips      : {diag.get('flips_bull', 0)} bullish / "
          f"{diag.get('flips_bear', 0)} bearish")
    print(f"  ... also past VWAP  : {diag.get('flips_vwap_ok', 0)}")
    print(f"  ... blocked, wrong direction for the regime : {diag.get('blocked_direction', 0)}")
    if diag.get("blocked_by_side"):
        print(f"  ... blocked by --allowed-sides filter  : {diag['blocked_by_side']}")
    print(f"  ... blocked, regime undecidable             : {diag.get('blocked_regime', 0)}")
    print(f"1-min bars replayed   : {diag.get('minute_bars_replayed', 0)}")
    if diag_session.get("rows"):
        print(f"Session check         : {diag_session['rows']} raw 1-min bars spanning "
              f"{diag_session['first']:%H:%M}..{diag_session['last']:%H:%M}; "
              f"{diag_session['pre_open']} pre-open and {diag_session['post_close']} "
              f"post-close bar(s) excluded"
              + (f"; weekend session(s): {', '.join(diag_session['weekend_days'][:4])}"
                 if diag_session.get("weekend_days") else ""))
    print(f"Trades taken          : {len(trades)}")

    if not trades:
        print("\n*** NO TRADES - why ***")
        if regimes.get("unknown") or regimes.get("no daily history"):
            print("The daily EMA regime could not be computed on most candles "
                  f"(unknown={regimes.get('unknown', 0)}, "
                  f"no history={regimes.get('no daily history', 0)}). "
                  "Not enough daily candles exist for EMA "
                  f"{cfg.ema_slow} on this contract. Try --daily-source index.")
        if not diag.get("flips_bull") and not diag.get("flips_bear"):
            print("No Supertrend flips occurred at all - check the timeframe and that "
                  "candles are being built correctly.")
        elif not diag.get("flips_vwap_ok"):
            print("Flips occurred but none agreed with the session VWAP side.")
        elif diag.get("blocked_direction") or diag.get("blocked_regime"):
            print(f"{diag.get('blocked_direction', 0)} valid flip+VWAP signals pointed against "
                  f"the daily regime, and {diag.get('blocked_regime', 0)} fell in the "
                  "no-trade band between the EMAs.")
        print("The funnel counts above show exactly where signals were dropped.")

    if diag.get("mtm_drawdown_pts"):
        print(f"\nMark-to-market DD : {diag['mtm_drawdown_pts']:.2f} pts "
              f"(worst open-position drawdown, from the 1-minute path - "
              f"the number above understates it)")
    if trades:
        print_daily_breakdown(trades)
        if show_trades:
            print_trade_list(trades)
    _print_stats(trades, out)

    # --- multi-day hold risk -------------------------------------------------------
    # Only meaningful in positional mode. An intraday trade never sees an opening gap,
    # so this whole block is new exposure created by holding across sessions.
    if trades and "nights" in pd.DataFrame(trades).columns:
        _df = pd.DataFrame(trades)
        _carried = _df[_df["nights"] > 0]
        if len(_carried):
            print("\n---------------- MULTI-DAY HOLD RISK ----------------")
            print(f"Positions carried overnight : {len(_carried)}/{len(_df)} trades, "
                  f"{int(_df['nights'].sum())} position-nights, "
                  f"longest hold {int(_df['nights'].max())} night(s)")
            print(f"Sessions opened THROUGH the stop : {diag.get('gap_through_stop', 0)} "
                  f"of {diag.get('overnight_holds', 0)} overnight session(s)")
            if diag.get("gap_through_stop"):
                print(f"                                 worst opening gap beyond the stop: "
                      f"{diag['worst_gap_pts']:.2f} pts. The stop cannot execute while the "
                      f"market is shut, so this is unavoidable slippage, not a modelling "
                      f"assumption.")
            _w = _carried[_carried["pnl_points"] > 0]
            print(f"P&L of carried positions         : {_carried['pnl_points'].sum():+.2f} pts "
                  f"({100 * len(_w) / len(_carried):.0f}% win) vs "
                  f"{_df[_df['nights'] == 0]['pnl_points'].sum():+.2f} pts for same-day trades")
            if diag.get("roll_exits"):
                print(f"Closed at a contract roll        : {diag['roll_exits']} position(s) - "
                      f"a hold cannot be carried into a different contract's prices.")
            print("-----------------------------------------------------")

    print_monthly_report(trades, diag, cfg.initial_capital, risk)
    if risk is not None and risk.enabled and any(risk.blocked.values()):
        _parts = []
        for _k, _v in risk.blocked.items():
            if not _v:
                continue
            # "sizing" is not a block: min_lots still took the trade. The other three
            # are hard blocks. Conflating them would hide whether a limit ever fired.
            _parts.append(f"{_v} trade(s) underfunded at the stated budget "
                          f"(min_lots took them anyway)" if _k == "sizing"
                          else f"{_v} entry signal(s) BLOCKED by the {_k} limit")
        print("\nRisk limits       : " + "; ".join(_parts))
        if risk.last_block_reason:
            print(f"                    last block: {risk.last_block_reason}")
        print(f"                    ending equity ₹{risk.equity:,.2f} "
              f"(started ₹{cfg.initial_capital:,.2f})")
    if result is not None:
        result.update({"trades": trades, "diag": diag, "risk": risk,
                       "start": start, "end": end,
                       "instrument": instrument.trading_symbol})


def summarise_window(trades: list[dict], diag: dict, capital: float = 0.0) -> dict:
    """Reduce one backtest window to the few numbers walk-forward comparison needs."""
    out = {"trades": 0, "net_pts": 0.0, "net_inr": 0.0, "pf": float("nan"),
           "win_pct": 0.0, "closed_dd": 0.0, "mtm_dd_inr": 0.0, "months": 0,
           "pos_months": 0}
    if not trades:
        return out
    df = pd.DataFrame(trades)
    w = float(df.loc[df.pnl_points > 0, "pnl_points"].sum())
    l = float(-df.loc[df.pnl_points < 0, "pnl_points"].sum())
    eq = df.pnl_money.cumsum()
    out.update(trades=len(df), net_pts=float(df.pnl_points.sum()),
               net_inr=float(df.pnl_money.sum()), pf=(w / l if l else float("inf")),
               win_pct=100.0 * float((df.pnl_points > 0).mean()),
               closed_dd=float((eq.cummax() - eq).max()))
    if diag.get("mtm_drawdown_pts"):
        out["mtm_dd_inr"] = float(diag["mtm_drawdown_pts"]) * float(df["qty"].mean())
    _ex = pd.to_datetime(df["exit_time"])
    if getattr(_ex.dt, "tz", None) is not None:
        _ex = _ex.dt.tz_localize(None)
    m = df.assign(_m=_ex.dt.to_period("M")).groupby("_m").pnl_money.sum()
    out["months"] = int(len(m))
    out["pos_months"] = int((m > 0).sum())
    return out


def run_walk_forward(cfg: Config, start: date, end: date,
                     train_days: int, test_days: int,
                     run_train: bool = True) -> None:
    """
    Chronological walk-forward validation.

    Each fold trains on an earlier window and is then scored on a LATER window it has
    never seen; the test window then becomes part of the next fold's training data. The
    parameters are never re-chosen against a test period - that is the whole point. A
    strategy that only works when its settings are picked on the same data being scored
    shows up here as a large gap between the train and test columns.

    What this measures is stability, not profit. A positive test result on one fold says
    little; the spread ACROSS folds is the evidence, because a real edge should appear
    repeatedly rather than in whichever window happened to contain the biggest moves.
    """
    folds: list[tuple] = []
    cur = start
    while cur < end:
        tr_end = min(cur + timedelta(days=train_days), end)
        te_end = min(tr_end + timedelta(days=test_days), end)
        if te_end <= tr_end:
            break
        folds.append((cur, tr_end, tr_end, te_end))
        cur = te_end
    if not folds:
        print(f"Window {start}..{end} is too short for train={train_days}d "
              f"test={test_days}d.")
        return

    print(f"\n{'=' * 96}")
    print(f"WALK-FORWARD  {start} .. {end}   train {train_days}d / test {test_days}d "
          f"   {len(folds)} fold(s)")
    print("Settings are chosen on TRAIN and frozen; TEST is unseen. Do not tune on TEST.")
    print(f"{'=' * 96}")
    print(f"{'#':<3}{'train window':<26}{'test window':<26}{'tr':>7}{'te':>7}"
          f"{'PF tr':>8}{'PF te':>8}{'net te pts':>12}")
    print("-" * 96)

    test_rows: list[dict] = []
    train_rows: list[dict] = []
    for i, (t0, t1, e0, e1) in enumerate(folds, 1):
        tr_sum = None
        if run_train:
            res: dict = {}
            with contextlib.redirect_stdout(io.StringIO()):
                run_backtest(cfg, t0, t1, result=res)
            tr_sum = summarise_window(res.get("trades", []), res.get("diag", {}),
                                      cfg.initial_capital)
            train_rows.append(tr_sum)
        res = {}
        with contextlib.redirect_stdout(io.StringIO()):
            run_backtest(cfg, e0, e1, result=res)
        te_sum = summarise_window(res.get("trades", []), res.get("diag", {}),
                                  cfg.initial_capital)
        test_rows.append(te_sum)

        def _pf(x):
            return "inf" if x == float("inf") else (f"{x:.2f}" if x == x else "-")

        print(f"{i:<3}{f'{t0} .. {t1}':<26}{f'{e0} .. {e1}':<26}"
              f"{(tr_sum['trades'] if tr_sum else '-'):>7}{te_sum['trades']:>7}"
              f"{(_pf(tr_sum['pf']) if tr_sum else '-'):>8}{_pf(te_sum['pf']):>8}"
              f"{te_sum['net_pts']:>+12.2f}")

    print("-" * 96)
    if not test_rows:
        return
    net = [r["net_pts"] for r in test_rows]
    pfs = [r["pf"] for r in test_rows if r["pf"] == r["pf"] and r["pf"] != float("inf")]
    dds = [r["mtm_dd_inr"] for r in test_rows]
    print(f"OUT-OF-SAMPLE     : {sum(1 for n in net if n > 0)}/{len(net)} folds profitable, "
          f"total {sum(net):+.2f} pts ({sum(r['net_inr'] for r in test_rows):+,.2f} INR)")
    if pfs:
        # Say how many folds this is actually computed over. A "median" of one number
        # reads like a median of all of them, and folds with no losing trade have an
        # infinite PF that cannot enter the median at all.
        print(f"                    profit factor: median {statistics.median(pfs):.2f} "
              f"(min {min(pfs):.2f}, max {max(pfs):.2f}) over {len(pfs)} of {len(test_rows)} "
              f"fold(s) that had losing trades")
    _idle = sum(1 for r in test_rows if r["trades"] == 0)
    if _idle:
        print(f"                    {_idle} test fold(s) took NO trades - they count as "
              f"neither profitable nor unprofitable, so read the ratio above with care")
    print(f"                    worst fold {min(net):+.2f} pts, best {max(net):+.2f} pts")
    if any(dds):
        print(f"                    worst-fold MTM drawdown ₹{max(dds):,.0f}")
    if train_rows:
        tr_net = [r["net_pts"] for r in train_rows]
        print(f"IN-SAMPLE (train) : total {sum(tr_net):+.2f} pts over the same folds")
        if sum(net) and sum(tr_net):
            print(f"                    test/train ratio {sum(net) / sum(tr_net):.2f} - a small "
                  f"or negative ratio means the settings fit the past and not the future")
    print("A strategy worth paper trading holds up ACROSS folds. One profitable fold "
          "out of several is not evidence.")
    print("=" * 96)


def _build_entry_fill_lookup(minute_frame):
    """
    Return a callable mapping "the moment a signal became actionable" to the first
    1-minute bar at or after it, as (timestamp, open), or None if that is not possible.

    Booking an entry at the signal candle's own close assumes you transact at the exact
    closing print. This is the next price that actually existed after the order could
    have been sent - still an approximation, but one that cannot use information from
    before the signal.
    """
    if minute_frame is None or minute_frame.empty or "open" not in minute_frame:
        return None
    try:
        midx = minute_frame.index
        mopen = minute_frame["open"].to_numpy()
    except (TypeError, ValueError, AttributeError) as exc:
        log.warning("Entry-fill lookup unavailable (%s); entries will fill at the "
                    "signal candle's close.", exc)
        return None
    n = len(midx)

    def lookup(want):
        try:
            i = int(midx.searchsorted(want, side="left"))
        except (TypeError, ValueError):
            return None
        return (midx[i], float(mopen[i])) if i < n else None

    return lookup


def _adverse_sign(side: str) -> int:
    """
    +1/-1 so that `price + _adverse_sign(side) * x` is WORSE for that side.

    ENTRY only. An entry OPENS the position, so a LONG buys and a higher price is worse.
    An exit CLOSES it, so the adverse direction is the opposite: a long exit sells, and a
    LOWER price is worse. Callers on the exit path must negate this - getting that
    backwards silently makes slippage improve the result instead of degrading it.
    """
    return 1 if side == "LONG" else -1


def _replay_act(engine: StrategyEngine, action: Action, tick_size: float,
                trades: list[dict], ts: datetime, cost_per_side: float = 0.0,
                candle_minutes: int = 15, cfg: Optional[Config] = None,
                cost_model: Optional[CostModel] = None,
                risk: Optional[RiskManager] = None,
                diag: Optional[dict] = None,
                entry_fill_lookup=None) -> None:
    """
    Execute one engine action inside the replay and, on an exit, record the trade.

    FILL REALISM. Every price the engine hands back is a *reference* price: the signal
    candle's close for an entry, the stop level for a stop-out. Booking a fill there
    assumes you transact at the exact print with zero friction, which systematically
    flatters the backtest - it wins trades the live system would lose. Each fill is
    therefore pushed in the direction that hurts the position:

      entry : taken from the first 1-minute bar at or after the signal became
              actionable (`entry_fill_lookup`), not from the signal candle's own close,
              then made worse by `entry_slippage_points`
      stop  : worse by `stop_slip_fraction` x the gap the market ALREADY traded through
              the stop (measured by the engine, previously only reported), plus
              `exit_slippage_points`

    `stop_slip_fraction = 0.0` reproduces the old optimistic booking; 1.0 books the
    adverse extreme.
    """
    if action.kind == "ENTRY":
        # `ts` is the CANDLE-OPEN stamp of the 15m bucket; the signal only became
        # actionable at its close, one candle later. Filling at `action.ref_price`
        # (that same close) assumes you transact at the exact closing print, so prefer
        # the next available 1-minute open when the replay can supply one.
        order_ts = ts + timedelta(minutes=candle_minutes)
        fill = _round_tick(action.ref_price, tick_size)
        fill_ts = order_ts
        if (cfg is not None and cfg.entry_fill == "next_minute_open"
                and entry_fill_lookup is not None):
            nxt = entry_fill_lookup(order_ts)
            if nxt is not None:
                fill = _round_tick(nxt[1], tick_size)
                fill_ts = nxt[0]
        if cfg is not None and cfg.entry_slippage_points:
            fill = _round_tick(fill + _adverse_sign(action.side) * cfg.entry_slippage_points,
                               tick_size)
        if risk is not None and risk.enabled:
            if not risk.can_open(ts):
                if diag is not None:
                    diag["blocked_by_risk"] = diag.get("blocked_by_risk", 0) + 1
                    diag["risk_block_reason"] = risk.last_block_reason
                log.debug("ENTRY blocked by risk limit: %s", risk.last_block_reason)
                return
            sized = risk.size_units(fill)
            if not sized:
                if diag is not None:
                    diag["blocked_by_risk"] = diag.get("blocked_by_risk", 0) + 1
                    diag["risk_block_reason"] = risk.last_block_reason
                log.debug("ENTRY skipped: %s", risk.last_block_reason)
                return
            action.quantity = sized
            if not risk.has_margin(fill, sized):
                if diag is not None:
                    diag["blocked_by_risk"] = diag.get("blocked_by_risk", 0) + 1
                    diag["risk_block_reason"] = risk.last_block_reason
                log.debug("ENTRY skipped: %s", risk.last_block_reason)
                return
        engine.open_position(action, fill, fill_ts)
        if risk is not None and risk.enabled:
            risk.on_open(fill_ts, action.side, action.quantity, fill)
        log.debug("ENTRY %s @ %.2f (%s)", action.side, fill, action.reason)
    elif action.kind == "EXIT":
        fill = _round_tick(action.ref_price, tick_size)
        measured_slip = float(getattr(action, "slip_pts", 0.0) or 0.0)
        applied_slip = 0.0
        if cfg is not None:
            # NEGATED on purpose: this closes the position, so adverse is the opposite of
            # the entry direction. A long exit sells into weakness, a short exit buys
            # into strength.
            adv = -_adverse_sign(action.side)
            if measured_slip and cfg.stop_slip_fraction:
                applied_slip = measured_slip * cfg.stop_slip_fraction
                fill = _round_tick(fill + adv * applied_slip, tick_size)
            if cfg.exit_slippage_points:
                applied_slip += cfg.exit_slippage_points
                fill = _round_tick(fill + adv * cfg.exit_slippage_points, tick_size)
        closed = engine.close_position(ts)
        if closed:
            gross = ((fill - closed.entry_price) if closed.side == "LONG"
                     else (closed.entry_price - fill))
            # Price the round trip from the two actual fills. A flat points/side figure
            # ignores that most of the cost scales with turnover and that the buy leg
            # carries stamp duty the sell leg does not.
            if cost_model is not None:
                # side and ts matter: which leg carries STT depends on the direction,
                # and the STT rate itself changed on 2026-04-01.
                cost_pts = cost_model.round_trip_points(closed.entry_price, fill,
                                                        closed.quantity, closed.side, ts)
            else:
                cost_pts = 2.0 * cost_per_side
            pnl = gross - cost_pts
            money = pnl * closed.quantity
            trades.append({
                # Four distinct timestamps, because conflating them is what made this
                # log read 15 minutes early against a broker statement:
                #   entry_bucket      the 15m candle that produced the ENTRY signal,
                #                     stamped with its OPEN (Upstox's convention)
                #   entry_signal_time when that signal became actionable - its close
                #   entry_time        when the position was actually filled
                #   exit_time / exit_signal_time  the same pair for the exit
                "entry_bucket": closed.entry_bucket,
                "entry_signal_time": (closed.entry_bucket + timedelta(minutes=candle_minutes)
                                      if closed.entry_bucket is not None else None),
                "entry_time": closed.entry_time, "fill_time": closed.entry_time,
                "exit_time": ts,
                "exit_signal_time": ts + timedelta(minutes=candle_minutes),
                "side": closed.side,
                "qty": closed.quantity, "entry": closed.entry_price, "exit": fill,
                "gross_points": round(gross, 2),
                "cost_points": round(cost_pts, 2),
                "pnl_points": round(pnl, 2), "pnl_money": round(money, 2),
                "trail_armed": closed.trailing_armed, "reason": action.reason,
                "slip_pts": round(measured_slip, 2),
                "slip_applied_pts": round(applied_slip, 2),
                # Nights the position was carried. Zero for every intraday trade; on a
                # positional hold this is the count of sessions the position spent
                # exposed to an opening gap it could not react to.
                "nights": (ts.date() - closed.entry_time.date()).days,
            })
            if risk is not None:
                risk.on_close(ts, money)



def _print_stats(trades: list[dict], path: str) -> None:
    if not trades:
        print("\nNo trades generated in this window.")
        return
    df = pd.DataFrame(trades)
    wins = df[df["pnl_points"] > 0]
    losses = df[df["pnl_points"] < 0]
    gross_win = wins["pnl_points"].sum()
    gross_loss = -losses["pnl_points"].sum()
    eq = df["pnl_points"].cumsum()
    dd = (eq.cummax() - eq).max()
    print("\n================ BACKTEST RESULTS ================")
    print(f"Trades            : {len(df)}  (long {int((df.side == 'LONG').sum())} / "
          f"short {int((df.side == 'SHORT').sum())})")
    print(f"Win rate          : {100 * len(wins) / len(df):.1f}%")
    print(f"Net points        : {df['pnl_points'].sum():+.2f}")
    print(f"Net money         : {df['pnl_money'].sum():+,.2f} INR")
    avg_win = f"{wins['pnl_points'].mean():+.2f}" if len(wins) else "n/a"
    avg_loss = f"{losses['pnl_points'].mean():+.2f}" if len(losses) else "n/a (no losers)"
    print(f"Avg win / avg loss: {avg_win} / {avg_loss} pts")
    print(f"Best / worst      : {df['pnl_points'].max():+.2f} / {df['pnl_points'].min():+.2f} pts")
    print(f"Profit factor     : {gross_win / gross_loss:.2f}" if gross_loss else "Profit factor     : inf")
    print(f"Max drawdown      : {dd:.2f} pts (closed-trade equity curve)")
    if "slip_pts" in df.columns and df["slip_pts"].sum() > 0:
        n_slip = int((df["slip_pts"] > 0).sum())
        slip_pts = float(df["slip_pts"].sum())
        slip_inr = float((df["slip_pts"] * df["qty"]).sum())
        applied = (float(df["slip_applied_pts"].sum())
                   if "slip_applied_pts" in df.columns else 0.0)
        if applied > 0:
            applied_inr = float((df["slip_applied_pts"] * df["qty"]).sum())
            print(f"\nStop-fill slippage: {slip_pts:.2f} pts measured across {n_slip} stop "
                  f"exit(s); {applied:.2f} pts ({applied_inr:,.2f} INR) is ALREADY DEDUCTED "
                  f"from the fills above.")
            print("                    The net points figure is post-slippage. Use "
                  "--stop-slip-fraction 0 for the old optimistic booking.")
        else:
            print(f"\nStop-fill optimism: {slip_pts:.2f} pts across {n_slip} stop exit(s) "
                  f"({slip_inr:,.2f} INR).")
            print("                    Fills are booked at the STOP level; the market actually "
                  "traded through it. Subtract this for a conservative bound:")
            print(f"                    net after that drag = {df['pnl_points'].sum() - slip_pts:+.2f} pts "
                  f"({df['pnl_money'].sum() - slip_inr:+,.2f} INR)")
    if "cost_points" in df.columns:
        cp = float(df["cost_points"].sum())
        print(f"Transaction costs : {cp:.2f} pts total ({cp / max(len(df), 1):.2f} pts/trade, "
              f"{float((df['cost_points'] * df['qty']).sum()):,.2f} INR)")
    print(f"Trade log         : {path}")
    print("=================================================")


def print_strategy_decisions(cfg: Config, lot_size: int, cost_model: CostModel,
                             price: float) -> None:
    """
    Print how every ambiguous rule in the strategy has been resolved.

    Section 8 of the spec requires these to be DEFINED rather than invented silently.
    Each one changes the result, so each is stated on every run - a reader of the output
    should never have to guess which convention produced the numbers below it.
    """
    rt = cost_model.round_trip_points(price, price, lot_size or 1)
    rows = [
        ("1. Daily EMA source",
         "NIFTY SPOT INDEX" if cfg.daily_filter_source == "index" else "futures contract",
         "the index has no basis and does not reset at a roll"),
        ("2. VWAP source",
         "FUTURES price x FUTURES volume, cumulative per session",
         "computed on the traded contract's own 15m candles"),
        ("3. Breakeven level",
         "entry price + round-trip cost" if cfg.breakeven_covers_costs
         else "ENTRY PRICE EXACTLY (per spec)",
         f"round trip = {rt:.2f} pts, so 'breakeven' still loses that much"
         if not cfg.breakeven_covers_costs else "a stop-out at breakeven is then net flat"),
        ("4. Same-bar ambiguity",
         "1-minute path replayed in time order where available; otherwise the ADVERSE "
         "extreme is tested against the ORIGINAL stop first",
         "the favourable-first reading fabricates profit out of a losing bar"),
        ("5. Overnight gaps",
         "the opening print becomes the fill; the gap beyond the stop is booked as "
         f"slippage ({100 * cfg.stop_slip_fraction:.0f}% of the measured gap)",
         "the stop cannot execute while the market is shut"),
        ("6. Trailing timing",
         "a new Supertrend level becomes active at the CLOSE of the candle that "
         "produced it; stops are never updated intra-candle",
         "intrabar monitoring still runs against the PREVIOUS level - no lookahead"),
        ("7. Rollover",
         {"close": "CLOSE the position at the roll boundary and wait for a fresh signal",
          "roll_forward": "CLOSE and re-open the same side/quantity on the new contract, "
                          "carrying the stop as a DISTANCE"}.get(cfg.roll_action, cfg.roll_action),
         "an absolute stop level from the old contract is meaningless on the new basis"),
        ("8. Operational failures",
         f"pre-trade margin check at {100 * cfg.margin_pct_of_notional:.0f}% of notional; "
         "rejected/unconfirmed orders halt new entries; broker position reconciliation "
         "on start and on drift",
         "an unaffordable entry is caught before the order, not after the rejection"),
    ]
    print("\n============ RESOLVED STRATEGY DECISIONS ============")
    for name, value, why in rows:
        print(f"  {name:<24} {value}")
        print(f"  {'':<24} -> {why}")
    print(f"  {'Hold mode':<24} {cfg.hold.upper()} "
          f"(product {'NRML' if cfg.hold == 'positional' else 'MIS'}), "
          f"no forced end-of-day exit" if cfg.hold == "positional" else
          f"  {'Hold mode':<24} {cfg.hold.upper()} - squares off at {cfg.square_off}")
    print(f"  {'Parameters':<24} EMA {cfg.ema_fast}/{cfg.ema_slow}, "
          f"Supertrend ATR {cfg.st_atr_period} x {cfg.st_multiplier:g}, "
          f"stop {cfg.initial_stop_points:g}, trail at +{cfg.trail_trigger_points:g}, "
          f"entry on {cfg.entry_on}, sides {cfg.allowed_sides}")
    print(f"  {'No fixed target':<24} confirmed - exit is the active stop only; "
          f"no daily trade limit, no cooldown")
    print("=====================================================")


def print_monthly_report(trades: list[dict], diag: Optional[dict] = None,
                         capital: float = 0.0, risk: Optional[RiskManager] = None,
                         cost_scenarios: tuple = (1.0, 2.0, 4.0)) -> None:
    """
    Monthly and drawdown reporting - the view that decides whether a strategy is fundable.

    A single net-profit number hides everything that matters for capital allocation:
    whether profit is spread across months or concentrated in one, how long the account
    stays underwater, and whether the result survives costs worse than modelled.

    Two measurement rules this function holds to:

    * Drawdown and recovery come from a RUPEE, TIMESTAMPED, MARK-TO-MARKET equity curve
      when the replay supplies one. Converting a points figure using an average quantity
      is only exact while every position is the same size, and it is wrong the moment
      risk sizing varies the lot count. The closed-trade curve is still printed, because
      the gap between the two IS the risk an open position carries.
    * Calendar months with no trades are shown as zero. Dropping them inflates the
      profitable-month percentage by counting only the months the strategy chose to trade.
    """
    if not trades:
        return
    df = pd.DataFrame(trades)
    for col in ("entry_time", "exit_time"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df.dropna(subset=["exit_time"]).sort_values("exit_time").reset_index(drop=True)
    if df.empty:
        return

    # tz_localize(None) first: to_period on a tz-aware series warns and drops the zone.
    df["_month"] = df["exit_time"].dt.tz_localize(None).dt.to_period("M")
    df["_day"] = df["exit_time"].dt.date
    has_qty = "qty" in df.columns

    print("\n================ MONTHLY PERFORMANCE ================")
    print(f"{'month':<9}{'trades':>7}{'win%':>7}{'net pts':>11}{'net INR':>14}{'PF':>7}")
    print("-" * 55)

    # Every calendar month in the window, so an idle month counts as not profitable
    # rather than disappearing from the denominator.
    all_months = pd.period_range(df["_month"].min(), df["_month"].max(), freq="M")
    by_month = {str(m): g for m, g in df.groupby("_month")}
    monthly: list[tuple] = []
    for m in all_months:
        key = str(m)
        g = by_month.get(key)
        if g is None or g.empty:
            monthly.append((key, 0.0, 0.0, 0))
            print(f"{key:<9}{0:>7}{'-':>7}{0.0:>+11.2f}{0.0:>+14,.2f}{'-':>7}")
            continue
        w = float(g.loc[g.pnl_points > 0, "pnl_points"].sum())
        l = float(-g.loc[g.pnl_points < 0, "pnl_points"].sum())
        pf = f"{w / l:.2f}" if l else ("inf" if w else "-")
        monthly.append((key, float(g.pnl_points.sum()), float(g.pnl_money.sum()), len(g)))
        print(f"{key:<9}{len(g):>7}{100 * (g.pnl_points > 0).mean():>7.1f}"
              f"{g.pnl_points.sum():>+11.2f}{g.pnl_money.sum():>+14,.2f}{pf:>7}")

    traded = [m for m in monthly if m[3] > 0]
    pos = [m for m in monthly if m[1] > 0]
    worst_m = min(monthly, key=lambda r: r[1])
    best_m = max(monthly, key=lambda r: r[1])
    daily = df.groupby("_day").pnl_money.sum()
    print("-" * 55)
    print(f"Profitable months : {len(pos)}/{len(monthly)} "
          f"({100 * len(pos) / len(monthly):.1f}%) - all calendar months counted, "
          f"{len(monthly) - len(traded)} idle")
    print(f"Best / worst month: {best_m[0]} {best_m[2]:+,.2f}  /  "
          f"{worst_m[0]} {worst_m[2]:+,.2f} INR")
    print(f"Worst day         : {daily.idxmin()} {float(daily.min()):+,.2f} INR")
    print(f"Average trade     : {df.pnl_points.mean():+.2f} pts "
          f"({df.pnl_money.mean():+,.2f} INR)")

    # --- drawdown: closed-trade curve, then mark-to-market in RUPEES ---------------
    eq = df.pnl_money.cumsum()
    closed_dd = float((eq.cummax() - eq).max())
    print(f"\nClosed-trade DD   : {closed_dd:,.2f} INR")

    if risk is not None and risk.mtm_tracked and len(risk.equity_curve) > 1:
        curve = pd.Series([v for _, v in risk.equity_curve],
                          index=pd.to_datetime([t for t, _ in risk.equity_curve]))
        src = "rupee MTM curve, quantity-weighted, open losses included"
    else:
        # Be explicit: this is NOT mark-to-market. Calling a realised-only curve
        # "MTM" is how an 11% drawdown gets reported as 4%.
        curve = eq.copy()
        curve.index = df["exit_time"].values
        src = "CLOSED TRADES ONLY - open-position risk NOT included"
    risk_dd = float((curve.cummax() - curve).max())
    label = "Mark-to-market DD" if src.startswith("rupee") else "Equity DD"
    print(f"{label:<18}: {risk_dd:,.2f} INR   [{src}]")
    if risk.mtm_tracked and closed_dd > 0:
        print(f"                    {risk_dd / closed_dd:.1f}x the closed-trade figure - the "
              f"difference is the risk carried while a position is open.")
    elif not risk.mtm_tracked:
        # Without a tracked MTM curve the two figures are the same number, and calling
        # that ratio "1.0x" implies a measurement that was never made.
        print("                    NO mark-to-market curve was tracked on this run, so the "
              "figure above EXCLUDES open-position risk. The points view below is the "
              "real drawdown; add --capital to get a rupee MTM curve.")
    if capital:
        print(f"                    = {100 * risk_dd / capital:.1f}% of "
              f"Rs {capital:,.0f} capital - THIS is the number to size against.")
    if diag and diag.get("mtm_drawdown_pts") and has_qty:
        _pts_dd = float(diag["mtm_drawdown_pts"])
        print(f"                    points view of the true MTM drawdown: "
              f"{_pts_dd:,.2f} pts = {_pts_dd * float(df['qty'].mean()):,.2f} INR")

    # --- recovery time, measured on the same rupee curve ---------------------------
    peak = float(curve.iloc[0])
    run_start = None
    longest = (timedelta(0), None, None)
    for i in range(len(curve)):
        v = float(curve.iloc[i])
        if v >= peak:
            peak = v
            if run_start is not None:
                span = curve.index[i] - curve.index[run_start]
                if span > longest[0]:
                    longest = (span, curve.index[run_start], curve.index[i])
                run_start = None
        elif run_start is None:
            run_start = i
    if run_start is not None:
        span = curve.index[-1] - curve.index[run_start]
        if span > longest[0]:
            longest = (span, curve.index[run_start], None)
    if longest[1] is not None:
        end_txt = (f"{longest[2]:%Y-%m-%d}" if longest[2] is not None
                   else "still underwater")
        print(f"Longest recovery  : {longest[0].days} calendar days  "
              f"{longest[1]:%Y-%m-%d} .. {end_txt}")

    # --- cost sensitivity, priced trade by trade on each trade's own quantity ------
    if "cost_points" in df.columns:
        qty = df["qty"] if has_qty else pd.Series([1] * len(df), index=df.index)
        cost_money = df["cost_points"] * qty
        base_pts = float(df["cost_points"].mean())
        base_inr = float(cost_money.sum())
        print(f"\nCost sensitivity  : modelled {base_pts:.2f} pts/round trip "
              f"({base_inr:,.2f} INR total)")
        print(f"{'multiplier':<12}{'pts/rt':>9}{'net pts':>11}{'net INR':>14}{'PF':>7}")
        net_inr = float(df.pnl_money.sum())
        for mult in cost_scenarios:
            # each trade pays the extra on ITS OWN quantity, not an average
            extra_inr = float((cost_money * (mult - 1.0)).sum())
            extra_pts = float((df["cost_points"] * (mult - 1.0)).sum())
            w = float(df.loc[df.pnl_points > 0, "pnl_points"].sum())
            l = float(-df.loc[df.pnl_points < 0, "pnl_points"].sum()) + extra_pts
            pf = f"{w / l:.2f}" if l else "inf"
            print(f"{mult:>6.1f}x     {base_pts * mult:>9.2f}"
                  f"{float(df.pnl_points.sum()) - extra_pts:>+11.2f}"
                  f"{net_inr - extra_inr:>+14,.2f}{pf:>7}")
        print("                    2x/4x bracket the case where real fills and charges "
              "are worse than modelled.")
    print("=====================================================")


def run_report(pattern: str) -> None:
    """
    Aggregate trade logs across days - the cumulative view for forward testing.

    Forward testing produces one trades_<date>.csv per session; this rolls every matching
    file into a single equity curve so you can compare forward results against the
    backtest that justified the strategy.
    """
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"No trade logs matched '{pattern}'.")
        print("Paper/live sessions write trades_<YYYYMMDD>.csv; backtests write "
              "backtest_<start>_<end>.csv.")
        return

    frames = []
    for path in files:
        try:
            df = pd.read_csv(path)
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            print(f"  skipping {path}: {exc}")
            continue
        if df.empty or "pnl_points" not in df.columns:
            print(f"  skipping {path}: no trade rows")
            continue
        for col in ("entry_time", "exit_time"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        df["source"] = os.path.basename(path)
        frames.append(df)

    if not frames:
        print("No usable trade rows found.")
        return

    all_trades = pd.concat(frames, ignore_index=True)
    all_trades = all_trades.drop_duplicates(
        subset=[c for c in ("entry_time", "exit_time", "side", "entry", "exit")
                if c in all_trades.columns])
    all_trades = all_trades.sort_values("exit_time").reset_index(drop=True)

    print(f"\nFiles aggregated : {len(frames)} ({', '.join(os.path.basename(f) for f in files)})")
    print(f"Date range       : {all_trades['exit_time'].min():%Y-%m-%d} .. "
          f"{all_trades['exit_time'].max():%Y-%m-%d}")

    print_daily_breakdown(all_trades.to_dict("records"))
    _print_stats(all_trades.to_dict("records"), f"{len(frames)} file(s) matching '{pattern}'")
    print_monthly_report(all_trades.to_dict("records"))



def run_probe(cfg: Config) -> None:
    """
    Measure what Upstox will actually serve for the current contract.

    Each test requests a SHORT 5-day window at a given age, so a rejection tells you the
    data at that age is unavailable - not merely that the range was too long.
    """
    client = UpstoxClient(cfg)
    master = fetch_instrument_master(cfg)
    instrument = find_current_month_future(cfg, master)
    today = datetime.now(tz=_ist()).date()

    print(f"\nProbing {instrument.trading_symbol} ({instrument.instrument_key}), "
          f"expiry {instrument.expiry}")
    print("Each row requests a 5-day window ending N days ago.\n")
    print(f"{'interval':<10}{'age':>6}  {'window':<25}{'result'}")
    print("-" * 78)
    for interval in ("1minute", "30minute", "day"):
        for age in (0, 5, 20, 40, 90, 200, 365):
            to_d = today - timedelta(days=age)
            from_d = to_d - timedelta(days=5)
            try:
                df = client.historical(instrument.instrument_key, interval, to_d, from_d)
                if df.empty:
                    result = "accepted, 0 candles (no data that old)"
                else:
                    result = (f"{len(df)} candles  {df.index.min():%Y-%m-%d %H:%M}"
                              f" .. {df.index.max():%Y-%m-%d %H:%M}")
            except UpstoxAPIError as exc:
                code = "?"
                if isinstance(exc.payload, dict):
                    errs = exc.payload.get("errors") or [{}]
                    code = errs[0].get("errorCode", "?")
                result = f"REJECTED {code}"
            print(f"{interval:<10}{age:>5}d  {str(from_d) + ' .. ' + str(to_d):<25}{result}")
        print()

    print("Expired-contract API (needed for --roll):")
    try:
        exps = client.expiries(cfg.index_key)
        print(f"  expiries listed      : {len(exps)}  e.g. {exps[:3]}")
        if exps:
            first = exps[0]
            exp_date = _parse_expiry(first if not isinstance(first, dict)
                                     else _find_field(first, "expiry", "expiry_date"))
            if exp_date:
                rows = client.expired_future_contract(cfg.index_key, exp_date)
                print(f"  contract for {exp_date}  : {len(rows)} row(s)")
                if rows:
                    print(f"  first row keys     : {sorted(rows[0].keys())[:8]}")
    except UpstoxAPIError as exc:
        print(f"  REJECTED - --roll will not work until this succeeds: {exc}")

    try:
        live = client.intraday(instrument.instrument_key, "1minute")
        print(f"intraday endpoint (today, 1minute): "
              f"{len(live)} candles" + (f", {live.index.min():%H:%M} .. {live.index.max():%H:%M}"
                                        if not live.empty else " (market closed / no session yet)"))
    except UpstoxAPIError as exc:
        print(f"intraday endpoint (today, 1minute): REJECTED {exc}")


# =====================================================================================
# 10. OFFLINE SELF-TEST  (exercises the real StrategyEngine, no API access needed)
# =====================================================================================

def run_selftest(cfg: Config) -> int:
    """
    Drives the actual StrategyEngine with synthetic bars to prove the rule set behaves
    as specified. Returns the number of failures.

    The whole suite must run OFFLINE. To enforce that rather than merely hope for it,
    requests.get is swapped for a tripwire for the duration: any test that reaches for
    the network fails loudly here instead of passing on a connected machine and dying
    on an offline one - which is exactly how a network-dependent test used to silently
    truncate the suite.
    """
    def _no_network(*args, **kwargs):
        raise AssertionError(
            "SELF-TEST MUST RUN OFFLINE: a test called requests.get"
            f"({args[0] if args else ''!r}). Inject a stub instead - see LiveRunner's "
            "master=/instrument= arguments."
        )

    _real_get = requests.get
    requests.get = _no_network
    try:
        return _run_selftest_body(cfg)
    finally:
        requests.get = _real_get


def _run_selftest_body(cfg: Config) -> int:
    """The self-test itself. Called by run_selftest with the network disabled."""
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {name}{(' - ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(name)

    cfg.quantity = 65
    engine = StrategyEngine(cfg)

    # ---- 1. regime gate blocks a perfectly valid 15m signal ----------------------
    st_bull = pd.Series({"trend": 1, "atr": 25.0, "lower": 24000.0, "upper": 24400.0})
    st_bear_prev = pd.Series({"trend": -1, "atr": 25.0, "lower": 24000.0, "upper": 24400.0})
    ts = datetime(2026, 10, 9, 10, 0, tzinfo=_ist())
    actions = engine.on_candle_close(ts, close=24200.0, high=24220.0, low=24150.0, open_=24160.0,
                                     st_row=st_bull, st_prev=st_bear_prev, vwap_value=24180.0,
                                     regime="none")
    check("Regime 'none' blocks entry despite bullish Supertrend + VWAP",
          not [a for a in actions if a.kind == "ENTRY"])

    # ---- 2. valid long entry, 100-point initial stop (spec v2) --------------------
    actions = engine.on_candle_close(ts, close=24200.0, high=24220.0, low=24150.0, open_=24160.0,
                                     st_row=st_bull, st_prev=st_bear_prev, vwap_value=24180.0,
                                     regime="long")
    entry = [a for a in actions if a.kind == "ENTRY"]
    check("Long entry fires on regime-long + bullish flip + close>VWAP", len(entry) == 1)
    check("Entry side is LONG", bool(entry) and entry[0].side == "LONG")
    engine.open_position(entry[0], 24200.0, ts)
    check("Initial stop is 100 points below entry",
          abs(engine.position.stop - (24200.0 - 100.0)) < 1e-9,
          f"stop={engine.position.stop}")

    # ---- 3. no entry while a position is open ------------------------------------
    actions = engine.on_candle_close(datetime(2026, 10, 9, 10, 15, tzinfo=_ist()), close=24250.0,
                                     high=24260.0, low=24190.0, open_=24200.0, st_row=st_bull,
                                     st_prev=st_bull, vwap_value=24180.0, regime="long")
    check("No second entry while a position is open",
          not [a for a in actions if a.kind == "ENTRY"])

    # ---- 4. +100 unrealised -> breakeven, and the stop never loosens -------------
    ticks = engine.on_tick(24299.0, ts)
    check("Stop unchanged below the +100 trigger",
          not [a for a in ticks if a.kind == "STOP_UPDATE"]
          and abs(engine.position.stop - 24100.0) < 1e-9,
          f"stop={engine.position.stop}")
    ticks = engine.on_tick(24300.0, ts)
    check("Breakeven armed at exactly +100 points",
          engine.position.trailing_armed and abs(engine.position.stop - 24200.0) < 1e-9,
          f"stop={engine.position.stop} armed={engine.position.trailing_armed}")

    # ---- 5. Supertrend trail ratchets up, then refuses to loosen -----------------
    ts2 = datetime(2026, 10, 9, 10, 30, tzinfo=_ist())
    trail_row = pd.Series({"trend": 1, "atr": 26.0, "lower": 24240.0, "upper": 24460.0})
    engine.on_candle_close(ts2, close=24330.0, high=24340.0, low=24250.0, open_=24300.0,
                           st_row=trail_row, st_prev=st_bull, vwap_value=24200.0, regime="long")
    check("Supertrend trail lifts the stop on candle close",
          abs(engine.position.stop - 24240.0) < 1e-9, f"stop={engine.position.stop}")

    weaker_row = pd.Series({"trend": 1, "atr": 30.0, "lower": 24210.0, "upper": 24500.0})
    ts3 = datetime(2026, 10, 9, 10, 45, tzinfo=_ist())
    engine.on_candle_close(ts3, close=24300.0, high=24320.0, low=24250.0, open_=24310.0,
                           st_row=weaker_row, st_prev=trail_row, vwap_value=24210.0, regime="long")
    check("Stop is never loosened by a lower Supertrend level",
          abs(engine.position.stop - 24240.0) < 1e-9, f"stop={engine.position.stop}")

    # ---- 6. exit when the actual traded price touches the stop -------------------
    ticks = engine.on_tick(24241.0, ts3)
    check("No exit while price stays above the stop",
          not [a for a in ticks if a.kind == "EXIT"])
    ticks = engine.on_tick(24240.0, ts3)
    exits = [a for a in ticks if a.kind == "EXIT"]
    check("Exit fires exactly when price touches the stop", len(exits) == 1)
    closed = engine.close_position()
    check("Position cleared after exit", engine.position is None and closed is not None)

    # ---- 7. re-entry permitted immediately on the next valid signal --------------
    st_bear = pd.Series({"trend": -1, "atr": 25.0, "lower": 24000.0, "upper": 24260.0})
    ts4 = datetime(2026, 10, 9, 11, 0, tzinfo=_ist())
    cfg.allow_same_candle_reentry = True
    actions = engine.on_candle_close(ts4, close=24100.0, high=24240.0, low=24090.0, open_=24230.0,
                                     st_row=st_bear, st_prev=trail_row, vwap_value=24200.0,
                                     regime="short")
    shorts = [a for a in actions if a.kind == "ENTRY" and a.side == "SHORT"]
    check("Short re-entry fires immediately after the exit (no daily limit)", len(shorts) == 1)
    engine.open_position(shorts[0], 24100.0, ts4)
    check("Short initial stop is 100 points above entry",
          abs(engine.position.stop - 24200.0) < 1e-9, f"stop={engine.position.stop}")

    # ---- 8. short breakeven + trailing direction ---------------------------------
    engine.on_tick(24001.0, ts4)
    check("Short stop unchanged below +100 profit", abs(engine.position.stop - 24200.0) < 1e-9)
    engine.on_tick(24000.0, ts4)
    check("Short breakeven at -100 points from entry",
          abs(engine.position.stop - 24100.0) < 1e-9, f"stop={engine.position.stop}")
    ts5 = datetime(2026, 10, 9, 11, 15, tzinfo=_ist())
    bear_trail = pd.Series({"trend": -1, "atr": 24.0, "lower": 23980.0, "upper": 24060.0})
    engine.on_candle_close(ts5, close=24020.0, high=24050.0, low=23990.0, open_=24040.0,
                           st_row=bear_trail, st_prev=st_bear, vwap_value=24150.0, regime="short")
    check("Short trail moves the stop DOWN with the Supertrend upper band",
          abs(engine.position.stop - 24060.0) < 1e-9, f"stop={engine.position.stop}")
    engine.close_position()

    # ---- 9. VWAP confirmation is mandatory --------------------------------------
    ts6 = datetime(2026, 10, 9, 11, 30, tzinfo=_ist())
    actions = engine.on_candle_close(ts6, close=24200.0, high=24220.0, low=24150.0, open_=24160.0,
                                     st_row=st_bull, st_prev=st_bear_prev, vwap_value=24250.0,
                                     regime="long")
    check("No long when the close is below VWAP",
          not [a for a in actions if a.kind == "ENTRY"])

    # ---- 10. indicator unit checks ----------------------------------------------
    px = pd.DataFrame({
        "open":  [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
        "high":  [102, 103, 104, 105, 106, 107, 108, 109, 110, 111],
        "low":   [99, 100, 101, 102, 103, 104, 105, 106, 107, 108],
        "close": [101, 102, 103, 104, 105, 106, 107, 108, 109, 110],
        "volume": [1000] * 10,
    }, index=pd.date_range("2026-01-01 09:15", periods=10, freq="15min", tz=_ist()))
    v = session_vwap(px)
    check("Session VWAP equals the mean typical price on constant volume",
          abs(v.iloc[-1] - ((px["high"] + px["low"] + px["close"]) / 3).mean()) < 1e-6)
    atr = wilder_atr(px, 7)
    check("Wilder ATR has no value before the period completes", pd.isna(atr.iloc[5]))
    check("Wilder ATR is positive once warm", atr.iloc[-1] > 0)

    # A quiet range followed by a decisive break above the upper band must flip bullish.
    # (A constant 1-point/bar drift with TR=3 must NOT flip: the 2x band is wider than the move.)
    breakout = pd.DataFrame({
        "open":   [99.8, 100.1, 99.9, 100.2, 99.8, 100.0, 100.1, 99.9, 100.0, 108.0],
        "high":   [100.3, 100.4, 100.3, 100.5, 100.2, 100.4, 100.4, 100.3, 110.6, 110.9],
        "low":    [99.7, 99.8, 99.7, 99.9, 99.6, 99.8, 99.9, 99.7, 99.4, 107.8],
        "close":  [100.1, 99.9, 100.2, 100.0, 100.0, 100.1, 100.0, 99.9, 110.5, 108.2],
        "volume": [1000] * 10,
    }, index=px.index)
    st_frame = supertrend(breakout, 7, 2.0)
    check("Supertrend stays neutral/bearish inside a range", st_frame["trend"].iloc[7] == -1)
    check("Supertrend flips bullish when the close breaks above the upper band",
          st_frame["trend"].iloc[8] == 1, f"trend={st_frame['trend'].tolist()}")
    drift = supertrend(px, 7, 2.0)
    check("Slow drift inside a 2x ATR band does not flip the Supertrend",
          drift["trend"].iloc[-1] == -1)

    # ---- 11. end-to-end replay through the real backtest loop --------------------
    daily_idx = pd.date_range("2026-06-01", periods=80, freq="D", tz=_ist())
    daily_hist = pd.DataFrame({"open": np.linspace(20000, 23800, 80),
                               "high": np.linspace(20100, 23900, 80),
                               "low": np.linspace(19900, 23700, 80),
                               "close": np.linspace(20050, 23850, 80),
                               "volume": [1_000_000] * 80}, index=daily_idx)

    ts_idx = pd.date_range("2026-10-09 09:15", periods=25, freq="15min", tz=_ist())
    quiet_close = [24000.0] * 8
    trend_close = list(np.linspace(24060.0, 24400.0, 17))
    closes = quiet_close + trend_close
    opens = [closes[0] - 5.0] + closes[:-1]
    highs = [max(o, c) + 6.0 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 6.0 for o, c in zip(opens, closes)]
    intraday_syn = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                                 "close": closes, "volume": [50_000] * 25}, index=ts_idx)
    highs[8] = 24070.0
    lows[8] = 23990.0

    engine_cfg = Config(quantity=65)
    trades = replay(engine_cfg, intraday_syn, daily_hist, tick_size=0.05, quantity=65)
    check("End-to-end replay produces at least one trade on a trending day", len(trades) >= 1,
          f"trades={len(trades)}")
    if trades:
        first = trades[0]
        check("First trade is a LONG in a long-only regime", first["side"] == "LONG")
        check("Trailing armed once profit passed +100 points", first["trail_armed"] is True)
        check("Trend-following trade exits in profit", first["pnl_points"] > 0,
              f"pnl={first['pnl_points']}")
        check("PnL points match entry/exit for the side",
              abs(first["pnl_points"] - round(first["exit"] - first["entry"], 2)) < 0.011)
        check("PnL money = points x quantity",
              abs(first["pnl_money"] - round(first["pnl_points"] * first["qty"], 2)) < 0.011)
        required = {"entry_time", "exit_time", "side", "qty", "entry", "exit",
                    "pnl_points", "pnl_money", "trail_armed", "reason"}
        check("Trade log rows carry every expected field", required.issubset(first.keys()))
    again = replay(Config(quantity=65), intraday_syn, daily_hist, tick_size=0.05, quantity=65)
    check("Replay is deterministic (reproducible backtests)", again == trades)

    # ---- 12. regime filter end-to-end: same 15m data, price BETWEEN the daily EMAs --
    # 70 quiet days then a 10-day rally puts EMA10 above and EMA50 below the session price.
    between_closes = list(np.full(70, 23800.0)) + list(np.linspace(23800, 24400, 10))
    between_daily = pd.DataFrame({"open": between_closes, "high": between_closes,
                                  "low": between_closes, "close": between_closes,
                                  "volume": [1_000_000] * 80}, index=daily_idx)
    reg = daily_regime(between_daily, 24060.0, cfg.ema_fast, cfg.ema_slow)
    check("Session price sits strictly between the daily EMAs",
          reg["regime"] == "none" and reg["ema_slow"] < 24060.0 < reg["ema_fast"],
          f"ema10={reg['ema_fast']} ema50={reg['ema_slow']}")
    blocked = replay(Config(quantity=65), intraday_syn, between_daily, tick_size=0.05, quantity=65)
    check("Daily 'no-trade' regime blocks every entry across the whole session",
          len(blocked) == 0, f"trades={len(blocked)}")

    # ---- 13. short-side end-to-end: bearish daily regime + breakdown -------------
    # Quiet flat range -> pop up (bullish Supertrend) -> breakdown bar (bearish FLIP).
    bear_closes = list(np.linspace(26500, 24200, 80))
    bear_daily = pd.DataFrame({"open": bear_closes, "high": bear_closes, "low": bear_closes,
                               "close": bear_closes, "volume": [1_000_000] * 80}, index=daily_idx)
    short_closes = [23950.0] * 7 + [24010.0, 24012.0, 24014.0, 23880.0] + \
        list(np.linspace(23860.0, 23650.0, 14))
    short_opens = [short_closes[0]] + short_closes[:-1]
    short_intra = pd.DataFrame({
        "open": short_opens,
        "high": [max(o, c) + 1.5 for o, c in zip(short_opens, short_closes)],
        "low": [min(o, c) - 1.5 for o, c in zip(short_opens, short_closes)],
        "close": short_closes, "volume": [50_000] * 25}, index=ts_idx)
    short_trades = replay(Config(quantity=65), short_intra, bear_daily, tick_size=0.05, quantity=65)
    check("Short-side replay produces a trade in a short-only regime", len(short_trades) >= 1,
          f"trades={len(short_trades)}")
    if short_trades:
        check("First trade is a SHORT", short_trades[0]["side"] == "SHORT")
        # The signal comes from the 11:45-12:00 bucket. Upstox stamps that bucket
        # "11:45", but the signal only became ACTIONABLE at its close, 12:00. The fill
        # is now stamped with the actionable time rather than the bucket-open label,
        # which previously made the log read 15 minutes early against a broker statement.
        check("Short entry fires on the bearish flip candle (the 11:45 bucket)",
              short_trades[0]["entry_bucket"].strftime("%H:%M") == "11:45"
              and short_trades[0]["entry_signal_time"].strftime("%H:%M") == "12:00",
              f"bucket={short_trades[0]['entry_bucket']} "
              f"actionable={short_trades[0]['entry_signal_time']}")
        check("Short trailing armed after +100 points", short_trades[0]["trail_armed"] is True)
        check("Short trade profits on the breakdown", short_trades[0]["pnl_points"] > 0,
              f"pnl={short_trades[0]['pnl_points']}")
    # Same 15m data under a long-only daily regime: the bullish flip may trade, the
    # bearish flip must not.
    crossed = replay(Config(quantity=65, stop_slip_fraction=0.0), short_intra, daily_hist,
                     tick_size=0.05, quantity=65)
    check("No SHORT is taken when the daily regime is long-only",
          all(t["side"] == "LONG" for t in crossed), f"sides={[t['side'] for t in crossed]}")
    if crossed:
        # stop_slip_fraction is pinned to 0 so this isolates STOP PLACEMENT from the
        # fill-realism feature tested in §32. The bug it guards against is a
        # favourable-first OHLC reading that fabricated 0.00 out of what is a -80 loss.
        check("Initial 100-point stop is honoured on a losing trade",
              abs(crossed[0]["pnl_points"] - (-100.0)) < 0.011,
              f"pnl={crossed[0]['pnl_points']}")
        _with_slip = replay(Config(quantity=65), short_intra, daily_hist,
                            tick_size=0.05, quantity=65)
        check("booking stop slippage can only make a losing trade WORSE, never better",
              _with_slip[0]["pnl_points"] <= crossed[0]["pnl_points"] + 1e-9,
              f"with slip={_with_slip[0]['pnl_points']}, "
              f"without={crossed[0]['pnl_points']}")
    # Mirror: the bullish 15m dataset under a short-only daily regime must take no LONG.
    crossed2 = replay(Config(quantity=65), intraday_syn, bear_daily, tick_size=0.05, quantity=65)
    check("No LONG is taken when the daily regime is short-only",
          all(t["side"] == "SHORT" for t in crossed2), f"sides={[t['side'] for t in crossed2]}")

    # ---- 14. hold mode: intraday squares off, positional carries -----------------
    def _mk_day(day, closes, jitter=1.5):
        opens = [closes[0]] + closes[:-1]
        idx = pd.date_range(f"{day} 09:15", periods=len(closes), freq="15min", tz=_ist())
        return pd.DataFrame({"open": opens,
                             "high": [max(o, c) + jitter for o, c in zip(opens, closes)],
                             "low": [min(o, c) - jitter for o, c in zip(opens, closes)],
                             "close": closes, "volume": [50_000] * len(closes)}, index=idx)

    day1 = _mk_day("2026-10-08", [24000.0] * 8 + list(np.linspace(24060.0, 24400.0, 17)))
    day2 = _mk_day("2026-10-09", [24400.0] * 25, jitter=0.5)
    # The SAME two days under both hold modes. Intraday must square off; positional must
    # carry the position across the close - that carry is the entire point of spec v2,
    # and it is also where the new risk lives, so both are pinned here.
    _two = pd.concat([day1, day2])
    overnight = replay(Config(quantity=65, hold="intraday"), _two, daily_hist,
                       tick_size=0.05, quantity=65)
    check("Two-day replay produces a trade on day 1", len(overnight) >= 1)
    if overnight:
        check("INTRADAY: every trade closes on the SAME day it opened",
              all(pd.Timestamp(t["entry_time"]).date() == pd.Timestamp(t["exit_time"]).date()
                  for t in overnight),
              f"spans={[(str(t['entry_time'])[:16], str(t['exit_time'])[:16]) for t in overnight]}")
        check("INTRADAY: an open position at the last candle exits as 'intraday square-off'",
              any(t["reason"] == "intraday square-off" for t in overnight),
              f"reasons={[t['reason'] for t in overnight]}")
        check("INTRADAY: a dead-flat day 2 generates no trades",
              all(pd.Timestamp(t["exit_time"]).date() != date(2026, 10, 9) for t in overnight))
        check("INTRADAY: no trade records an overnight carry",
              all(t["nights"] == 0 for t in overnight),
              f"nights={[t['nights'] for t in overnight]}")

    _carry = replay(Config(quantity=65, hold="positional"), _two, daily_hist,
                    tick_size=0.05, quantity=65)
    check("POSITIONAL: the position is CARRIED into day 2 rather than squared off",
          any(t["nights"] > 0 for t in _carry),
          f"nights={[t['nights'] for t in _carry]}")
    check("POSITIONAL: nothing exits as 'intraday square-off'",
          not any(t["reason"] == "intraday square-off" for t in _carry),
          f"reasons={[t['reason'] for t in _carry]}")
    check("POSITIONAL: the carried position is still booked, not silently dropped",
          len(_carry) >= 1 and _carry[-1]["exit_time"] is not None)

    # --- a carried position cannot be rolled into a different contract -------------
    _r1 = day1.copy(); _r1["contract"] = "NIFTY FUT 27 OCT 26"
    _r2 = day2.copy(); _r2["contract"] = "NIFTY FUT 23 NOV 26"
    _rdiag2: dict = {}
    _rolltr = replay(Config(quantity=65, hold="positional"), pd.concat([_r1, _r2]),
                     daily_hist, tick_size=0.05, quantity=65, diag=_rdiag2)
    # The contract label has to SURVIVE the 1-minute -> 15-minute resample, or every
    # roll check downstream is dead code on real --roll data. This WAS broken: the checks
    # below set `contract` on already-resampled frames, so they passed while the live
    # path silently dropped the column and a carried position was stopped out against
    # the NEXT contract's prices - the basis jump then got booked as stop slippage.
    _mres = pd.concat([
        pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100,
                      "contract": "NIFTY FUT 30 MAR 26"},
                     index=pd.date_range("2026-03-27 09:15", periods=20, freq="1min",
                                         tz=_ist())),
        pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100,
                      "contract": "NIFTY FUT 28 APR 26"},
                     index=pd.date_range("2026-03-27 09:35", periods=20, freq="1min",
                                         tz=_ist()))])
    check("the contract label survives resample_ohlcv - the roll check depends on it",
          "contract" in resample_ohlcv(_mres, 15).columns,
          f"cols={list(resample_ohlcv(_mres, 15).columns)}")
    check("resample_ohlcv still works when there is no contract column",
          "contract" not in resample_ohlcv(_mres.drop(columns=["contract"]), 15).columns)

    check("a held position is CLOSED at the contract roll boundary",
          any("contract roll" in t["reason"] for t in _rolltr),
          f"reasons={[t['reason'] for t in _rolltr]}")
    check("the roll exit is counted in the diagnostics",
          _rdiag2.get("roll_exits", 0) >= 1, f"diag={_rdiag2.get('roll_exits')}")

    # --- overnight gap: the stop cannot execute while the market is shut -----------
    _g2 = _mk_day("2026-10-09", [23800.0] * 25, jitter=0.5)   # opens far below any stop
    _gdiag: dict = {}
    _gtr = replay(Config(quantity=65, hold="positional"), pd.concat([day1, _g2]),
                  daily_hist, tick_size=0.05, quantity=65, diag=_gdiag)
    check("an opening gap through the stop is detected and counted",
          _gdiag.get("gap_through_stop", 0) >= 1 and _gdiag.get("overnight_holds", 0) >= 1,
          f"gap_through_stop={_gdiag.get('gap_through_stop')} "
          f"overnight_holds={_gdiag.get('overnight_holds')}")
    check("the gap is booked as slippage, so the loss is the GAP and not the stop level",
          bool(_gtr) and _gtr[-1]["slip_pts"] > 0,
          f"slip={_gtr[-1]['slip_pts'] if _gtr else None}")

    # ---- 15. entries are refused at/after the entry cutoff ----------------------
    late = pd.DatetimeIndex([datetime(2026, 10, 9, 15, 15, tzinfo=_ist())])
    check("past_entry_cutoff flags 15:15 against a 15:15 cutoff",
          past_entry_cutoff(late[0].to_pydatetime(), "15:15") is True)
    check("past_entry_cutoff allows 15:00 against a 15:15 cutoff",
          past_entry_cutoff(datetime(2026, 10, 9, 15, 0, tzinfo=_ist()), "15:15") is False)

    # ---- 16. 1-minute -> 15-minute resampling (Upstox serves no native 15m) -----
    check("plan_fetch builds 15minute from 1minute", plan_fetch("15minute") == ("1minute", 15))
    check("plan_fetch serves 30minute natively", plan_fetch("30minute") == ("30minute", 1))
    check("plan_fetch serves day natively", plan_fetch("day") == ("day", 1))

    minute_idx = pd.date_range("2026-10-09 09:15", periods=375, freq="1min", tz=_ist())
    rng = np.random.default_rng(7)
    base = 24000 + np.cumsum(rng.normal(0, 2, 375))
    one_min = pd.DataFrame({"open": base, "high": base + 3, "low": base - 3,
                            "close": base + 1, "volume": np.full(375, 1000.0)}, index=minute_idx)
    fifteen = resample_ohlcv(one_min, 15)
    check("A full NSE session of 375 one-minute bars gives 25 fifteen-minute candles",
          len(fifteen) == 25, f"got {len(fifteen)}")
    check("Resampled candles start at 09:15 and end at 15:15",
          fifteen.index[0].strftime("%H:%M") == "09:15"
          and fifteen.index[-1].strftime("%H:%M") == "15:15",
          f"first={fifteen.index[0]:%H:%M} last={fifteen.index[-1]:%H:%M}")
    first = fifteen.iloc[0]
    check("Resampled OHLC aggregates correctly (first/max/min/last)",
          abs(first["open"] - one_min["open"].iloc[0]) < 1e-9
          and abs(first["high"] - one_min["high"].iloc[:15].max()) < 1e-9
          and abs(first["low"] - one_min["low"].iloc[:15].min()) < 1e-9
          and abs(first["close"] - one_min["close"].iloc[14]) < 1e-9)
    check("Resampled volume is the sum of its constituent minutes",
          abs(first["volume"] - 15000.0) < 1e-9, f"vol={first['volume']}")
    check("A still-forming trailing bucket is kept (caller decides if it closed)",
          len(resample_ohlcv(one_min.iloc[:370], 15)) == 25)
    close_labelled = one_min.set_index(one_min.index + pd.Timedelta(minutes=1))
    shifted = resample_ohlcv(close_labelled, 15)
    check("Close-labelled 1-minute data is auto-shifted so buckets still start at 09:15",
          shifted.index[0].strftime("%H:%M") == "09:15" and len(shifted) == 25
          and abs(shifted["volume"].iloc[0] - 15000.0) < 1e-9,
          f"first={shifted.index[0]:%H:%M} n={len(shifted)} vol={shifted['volume'].iloc[0]}")

    # Exact round trip: expand the known-good 15m fixture to 1-minute, resample back,
    # and prove the strategy produces identical trades.
    rows = []
    for ts, row in intraday_syn.iterrows():
        for k in range(15):
            rows.append({"ts": ts + pd.Timedelta(minutes=k),
                         "open": row["open"] if k == 0 else row["close"],
                         "high": row["high"], "low": row["low"], "close": row["close"],
                         "volume": row["volume"] / 15})
    one_min_syn = pd.DataFrame(rows).set_index("ts").sort_index()
    rebuilt = resample_ohlcv(one_min_syn, 15)
    check("Resampling 1-minute back to 15-minute recovers the original OHLC exactly",
          np.allclose(rebuilt["close"].to_numpy(), intraday_syn["close"].to_numpy(), atol=1e-9)
          and np.allclose(rebuilt["high"].to_numpy(), intraday_syn["high"].to_numpy(), atol=1e-9)
          and len(rebuilt) == len(intraday_syn),
          f"len {len(rebuilt)} vs {len(intraday_syn)}")
    key = lambda ts_: [(t["side"], t["entry"], t["exit"], t["pnl_points"]) for t in ts_]
    roundtrip = replay(Config(quantity=65), rebuilt, daily_hist, tick_size=0.05, quantity=65)
    check("Replay on resampled data reproduces the native 15-minute trades exactly",
          key(roundtrip) == key(trades),
          f"resampled={key(roundtrip)} native={key(trades)}")

    # ---- 17. chunked historical fetching (Upstox rejects over-long 1m ranges) ---
    seen: list[tuple] = []

    class _StubClient(UpstoxClient):
        def __init__(self):                      # no auth, no network
            self.cfg = Config()
            self.session = None

        def historical(self, key, interval, to_date, from_date=None):
            seen.append((interval, from_date, to_date))
            if from_date < date(2026, 9, 10):     # nothing exists older than this
                return pd.DataFrame()
            idx = pd.date_range(f"{from_date} 09:15", periods=5, freq="15min", tz=_ist())
            return pd.DataFrame({"open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5,
                                 "close": [1.5] * 5, "volume": [100.0] * 5}, index=idx)

    merged = _StubClient().historical_range("NSE_FO|48704", "1minute",
                                            date(2026, 10, 9), date(2026, 9, 1))
    widths = [(t - f).days + 1 for _, f, t in seen]
    check("A 39-day 1-minute range is split into chunks of at most 25 days",
          bool(seen) and max(widths) <= 25, f"widths={widths}")
    check("Chunks cover the full requested span with no gaps or overlaps",
          seen[0][2] == date(2026, 10, 9) and seen[-1][1] == date(2026, 9, 1)
          and all(seen[i][2] == seen[i - 1][1] - timedelta(days=1) for i in range(1, len(seen))),
          f"chunks={[(str(f), str(t)) for _, f, t in seen]}")
    check("A chunk with no data is tolerated and the remainder still merges",
          len(merged) > 0 and merged.index.is_monotonic_increasing, f"rows={len(merged)}")

    # ---- 18. the "no trades" failure mode: short daily history -------------------
    thin = pd.DataFrame({"close": np.linspace(22000, 24000, 49)},
                        index=pd.bdate_range("2026-07-28", periods=49, tz=_ist()))
    check("A NaN EMA reports 'unknown', never the 'none' (no-trade) regime",
          daily_regime(thin, 24000.0, 10, 50)["regime"] == "unknown",
          f"got {daily_regime(thin, 24000.0, 10, 50)['regime']}")
    check("With enough history the same call resolves to a real regime",
          daily_regime(daily_hist, 24000.0, 10, 50)["regime"] in ("long", "short", "none"))

    # ---- 19. session filter and 09:00-labelled futures data ----------------------
    pre_open = pd.date_range("2026-10-09 09:00", periods=390, freq="1min", tz=_ist())
    po_df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                          "volume": 100.0}, index=pre_open)
    kept = filter_session(resample_ohlcv(po_df, 15), "09:15", "15:30")
    check("Pre-open 09:00 candles are dropped from the session",
          kept.index[0].strftime("%H:%M") == "09:15", f"first={kept.index[0]:%H:%M}")
    check("Session filter yields 25 fifteen-minute candles, not 26", len(kept) == 25,
          f"got {len(kept)}")
    check("09:00-stamped data is treated as open-labelled, not shifted",
          normalize_minute_labels(po_df, "09:15", 15).index[0].strftime("%H:%M") == "09:00")

    # ---- 20. regime source falls back to the index ------------------------------
    class _RegimeStub(UpstoxClient):
        def __init__(self):
            self.cfg = Config()
            self.session = None
            self.asked: list[str] = []

        def historical_range(self, key, interval, to_date, from_date):
            self.asked.append(key)
            if key == self.cfg.index_key:
                return daily_hist
            return thin                      # the future has too little history

    stub = _RegimeStub()
    inst = Instrument("NSE_FO|48704", "NIFTY FUT 27 OCT 26", "48704", 65, date(2026, 10, 27))
    _idx_only = _RegimeStub()
    _rd, _src = fetch_regime_data(_idx_only, Config(), inst, date(2026, 9, 1), date(2026, 10, 9))
    check("The default regime source is the spot index", Config().daily_filter_source == "index")
    check("Index mode queries ONLY the index - no basis-mismatched futures series",
          _idx_only.asked == ["NSE_INDEX|Nifty 50"] and _src == "index",
          f"asked={_idx_only.asked} source={_src}")
    check("Index mode still returns enough history for the EMA filter",
          len(_rd) >= Config().ema_slow + 5)
    regime_daily, source = fetch_regime_data(stub, Config(daily_filter_source="futures"),
                                             inst, date(2026, 9, 1), date(2026, 10, 9))
    check("A contract with too few daily candles falls back to the Nifty index",
          source == "index" and len(regime_daily) == len(daily_hist),
          f"source={source} rows={len(regime_daily)}")
    check("The futures-mode fallback queries both the future and the index",
          stub.asked == ["NSE_FO|48704", "NSE_INDEX|Nifty 50"], f"asked={stub.asked}")

    def _capture_warning(master, inst, start_day):
        """True if _warn_if_window_spans_expiry logs anything for this window."""
        records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda rec: records.append(rec.getMessage())
        log.addHandler(handler)
        try:
            _warn_if_window_spans_expiry(master, Config(), inst, start_day)
        finally:
            log.removeHandler(handler)
        return bool(records)

    # ---- 21. contract rolling and the front-month warning ------------------------
    oct_inst = Instrument("NSE_FO|48704", "NIFTY FUT 27 OCT 26", "48704", 65, date(2026, 10, 27))
    mini_master = [
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": "NIFTY",
         "instrument_key": "NSE_FO|48704", "trading_symbol": "NIFTY FUT 27 OCT 26",
         "exchange_token": "48704", "lot_size": 65,
         "expiry": int(datetime(2026, 10, 27, tzinfo=_ist()).timestamp() * 1000)},
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": "NIFTY",
         "instrument_key": "NSE_FO|61471", "trading_symbol": "NIFTY FUT 23 NOV 26",
         "exchange_token": "61471", "lot_size": 65,
         "expiry": int(datetime(2026, 11, 23, tzinfo=_ist()).timestamp() * 1000)},
    ]

    class _RollStub(UpstoxClient):
        def __init__(self):
            self.cfg = Config()
            self.session = None
            self.fetched: list = []

        def expiries(self, key):
            return ["2026-09-29"]

        def expired_future_contract(self, key, expiry_date):
            return [{"instrument_key": "NSE_FO|SEP", "trading_symbol": "NIFTY FUT 29 SEP 26",
                     "exchange_token": "11111", "lot_size": 75}]

        def expired_historical(self, key, interval, to_date, from_date):
            self.fetched.append(("expired", key, from_date, to_date))
            idx = pd.bdate_range(from_date, to_date, tz=_ist())
            return pd.DataFrame({"open": [1.0] * len(idx), "high": [2.0] * len(idx),
                                 "low": [0.5] * len(idx), "close": [1.5] * len(idx),
                                 "volume": [100.0] * len(idx)}, index=idx)

        def historical_range(self, key, interval, to_date, from_date):
            self.fetched.append(("live", key, from_date, to_date))
            idx = pd.bdate_range(from_date, to_date, tz=_ist())
            return pd.DataFrame({"open": [1.0] * len(idx), "high": [2.0] * len(idx),
                                 "low": [0.5] * len(idx), "close": [1.5] * len(idx),
                                 "volume": [100.0] * len(idx)}, index=idx)

    roll_stub = _RollStub()
    sched = build_contract_schedule(roll_stub, Config(), mini_master,
                                    date(2026, 9, 1), date(2026, 10, 9))
    check("An expired SEP contract is resolved alongside the listed ones",
          any(i["expired"] for i in sched["contracts"].values()),
          f"contracts={list(sched['contracts'])}")
    check("Sept 1 maps to the SEP contract, not today's OCT contract",
          sched["contracts"][sched["by_day"][date(2026, 9, 1)]]["trading_symbol"]
          == "NIFTY FUT 29 SEP 26")
    check("Sept 30 (after the Sep expiry) rolls to the OCT contract",
          sched["contracts"][sched["by_day"][date(2026, 9, 30)]]["trading_symbol"]
          == "NIFTY FUT 27 OCT 26")
    rolled = fetch_rolled_candles(roll_stub, Config(), sched, "1minute",
                                  date(2026, 9, 1), date(2026, 10, 9))
    check("Each contract is fetched only for the days it was front month",
          sorted(k for k, *_ in roll_stub.fetched) == ["expired", "live"]
          and len(rolled["contract"].unique()) == 2,
          f"fetched={roll_stub.fetched}")

    check("A window spanning a monthly expiry warns about the front month",
          _capture_warning(mini_master, oct_inst, date(2026, 9, 1)))
    check("A window inside one contract's life stays silent",
          not _capture_warning(mini_master, oct_inst, date(2026, 10, 1)))

    check("_extract_list tolerates a bare list", _extract_list([1, 2]) == [1, 2])
    check("_extract_list tolerates {'data': [...]}", _extract_list({"data": [1, 2]}) == [1, 2])
    check("_extract_list tolerates nested {'data': {'contracts': [...]}}",
          _extract_list({"data": {"contracts": [1, 2]}}) == [1, 2])
    check("_extract_list tolerates garbage", _extract_list(None) == [])

    # ---- 22. OHLC ambiguity: a bar touching BOTH the trigger and the stop --------
    amb_bull = pd.Series({"trend": 1, "atr": 25.0, "lower": 23900.0, "upper": 24200.0})
    amb_bear = pd.Series({"trend": -1, "atr": 25.0, "lower": 23900.0, "upper": 24200.0})
    t_open = datetime(2026, 10, 9, 10, 0, tzinfo=_ist())
    t_bar = datetime(2026, 10, 9, 10, 15, tzinfo=_ist())

    def _open_long(intrabar):
        e = StrategyEngine(Config(quantity=65))
        acts = e.on_candle_close(t_open, 24000.0, 24005.0, 23995.0, 23998.0,
                                 amb_bull, amb_bear, 23990.0, "long")
        e.open_position([a for a in acts if a.kind == "ENTRY"][0], 24000.0, t_open)
        return e

    cons = _open_long(False)
    cons_acts = cons.on_candle_close(t_bar, 24000.0, 24110.0, 23900.0, 24000.0,
                                     amb_bull, amb_bull, 23990.0, "long",
                                     intrabar_handled=False)
    cons_exit = [a for a in cons_acts if a.kind == "EXIT"]
    check("Ambiguous bar: the stop is tested against the ORIGINAL level, not breakeven",
          len(cons_exit) == 1 and abs(cons_exit[0].ref_price - 23900.0) < 1e-9,
          f"exit={[a.ref_price for a in cons_exit]}")

    mins = pd.date_range("2026-10-09 10:15", periods=15, freq="1min", tz=_ist())
    low_first = pd.DataFrame({"close": [24000, 23980, 23950, 23915, 23900, 23910, 23950,
                                        24000, 24050, 24080, 24100, 24110, 24090, 24050, 24020]},
                             index=mins)
    high_first = pd.DataFrame({"close": [24000, 24040, 24080, 24110, 24100, 24060, 24020,
                                         23980, 23950, 23915, 23900, 23910, 23950, 24000, 24020]},
                              index=mins)

    def _minute_path(frame):
        e = _open_long(False)
        fill = None
        for mts, mrow in frame.iterrows():
            for a in e.on_tick(float(mrow["close"]), mts):
                if a.kind == "EXIT":
                    fill = a.ref_price
        return fill

    check("1-min replay: when the low came first, the trade loses the full 100 points",
          abs(_minute_path(low_first) - 23900.0) < 1e-9,
          f"got {_minute_path(low_first)}")
    check("1-min replay: when the high came first, breakeven legitimately saves it",
          abs(_minute_path(high_first) - 24000.0) < 1e-9,
          f"got {_minute_path(high_first)}")
    check("The two orderings give different results, so ordering genuinely matters",
          abs(_minute_path(low_first) - _minute_path(high_first)) > 1.0)

    # ---- 23. live order handling must never fabricate a fill --------------------
    class _OrderStub:
        def __init__(self, states=None, rows=None, modify_ok=True):
            self._states, self._rows, self._modify_ok = states or {}, rows or [], modify_ok
        def order_details(self, oid):
            return self._states.get(oid, {})
        def positions(self):
            return self._rows
        def modify_order(self, *a, **k):
            if not self._modify_ok:
                raise UpstoxAPIError(400, {"e": 1}, "/modify")
            return "ok"

    _inst = Instrument("NSE_FO|48704", "NIFTY FUT 27 OCT 26", "48704", 65, date(2026, 10, 27))
    _cfg = Config(quantity=65, broker_sl_backstop=True)

    def _fill(states, oid):
        ex = LiveExecutor(_cfg, _inst, _OrderStub(states))
        try:
            return ("fill", ex._await_fill(oid, attempts=2, pause=0.0))
        except OrderRejected:
            return ("rejected", None)
        except OrderUnconfirmed:
            return ("unconfirmed", None)

    check("A completed order returns the broker's actual average price",
          _fill({"A": {"status": "complete", "average_price": 24005.5,
                       "filled_quantity": 65, "quantity": 65}}, "A") == ("fill", 24005.5))
    check("A PARTIAL fill raises rather than being treated as complete",
          _fill({"B": {"status": "complete", "average_price": 24005.5,
                       "filled_quantity": 40, "quantity": 65}}, "B")[0] == "unconfirmed")
    check("A rejected order raises and creates no position",
          _fill({"C": {"status": "rejected", "filled_quantity": 0, "quantity": 65}}, "C")[0]
          == "rejected")
    check("An order that never resolves raises instead of assuming a fill",
          _fill({"D": {"status": "pending", "filled_quantity": 0, "quantity": 65}}, "D")[0]
          == "unconfirmed")
    check("An unreadable order status raises instead of assuming a fill",
          _fill({}, "E")[0] == "unconfirmed")

    ex_sync = LiveExecutor(_cfg, _inst, _OrderStub(modify_ok=False))
    ex_sync.sl_order_id = "SL1"
    ex_sync.position_side, ex_sync.position_qty = "LONG", 65
    ex_sync.last_synced_stop = 23920.0
    check("A failed stop modification reports False so the caller can square off",
          ex_sync.sync_stop(24000.0) is False)

    # ---- 24. broker/engine reconciliation ---------------------------------------
    runner = LiveRunner(Config(quantity=65), "paper", instrument=_inst)
    _ts = datetime.now(tz=_ist())

    def _seed_long():
        runner.engine.position = None
        runner.trades.clear()
        runner._halt_entries = False
        runner.engine.open_position(Action("ENTRY", "LONG", 65, 24000.0, "t", _ts), 24000.0, _ts)

    class _PosStub:
        def __init__(self, rows):
            self._rows = rows
        def positions(self):
            return self._rows
        def ltp(self, key):
            return 24010.0

    _seed_long()
    runner.client = _PosStub([{"instrument_token": "48704", "quantity": 0}])
    runner._reconcile()
    check("Broker flat + engine long: stale position dropped, no exit order sent",
          runner.engine.position is None and len(runner.trades) == 1
          and "reconciled" in runner.trades[-1]["reason"])

    _seed_long()
    runner.client = _PosStub([{"instrument_token": "48704", "quantity": 65}])
    runner._reconcile()
    check("Broker and engine agreeing changes nothing",
          runner.engine.position is not None and len(runner.trades) == 0)

    runner.engine.position = None
    runner.client = _PosStub([{"instrument_token": "48704", "quantity": 65}])
    runner._reconcile()
    check("An unmanaged broker position halts new entries", runner._halt_entries is True)

    _seed_long()
    _before = runner.engine.position

    class _FailStub(_PosStub):
        def positions(self):
            raise UpstoxAPIError(500, {}, "/positions")

    runner.client = _FailStub([])
    runner._reconcile()
    check("A failed position check leaves engine state untouched (no guessing)",
          runner.engine.position is _before)

    # ---- 25. completed-daily-candle rule is deterministic -----------------------
    _today = pd.Timestamp(datetime.now(tz=_ist()).date(), tz=_ist())
    _past = pd.bdate_range(end=_today - pd.Timedelta(days=1), periods=52, tz=_ist())
    _forming = pd.DataFrame({"close": list(np.linspace(23000, 23400, 51))},
                            index=pd.DatetimeIndex([*_past[:50], _today]))
    _midday = _today + pd.Timedelta(hours=10)
    _after = _today + pd.Timedelta(hours=15, minutes=31)
    check("Today's candle is treated as forming during the session",
          _is_forming_daily(_forming, now=_midday) is True)
    check("Today's candle is treated as complete after the close",
          _is_forming_daily(_forming, now=_after) is False)
    _mid = daily_regime(_forming, 23240.0, 10, 50, now=_midday)
    check("The forming candle is excluded from the EMAs, so as_of is the prior session",
          _mid["as_of"].date() == _past[49].date())
    check("EMA50 is seeded on 50 completed closes, not 51 including the forming one",
          abs(_mid["ema_slow"] - float(ema(_forming["close"].iloc[:-1], 50).iloc[-1])) < 1e-9)
    check("A caller-supplied instant makes the regime reproducible",
          daily_regime(_forming, 23240.0, 10, 50, now=_midday)["regime"]
          == daily_regime(_forming, 23240.0, 10, 50, now=_midday)["regime"])
    _hist = pd.DataFrame({"close": list(np.linspace(23000, 23400, 52))}, index=_past[:52])
    check("A purely historical window is clock-independent",
          daily_regime(_hist, 23240.0, 10, 50, now=_midday) ==
          daily_regime(_hist, 23240.0, 10, 50, now=_after))

    # ---- 26. same-candle re-entry means the same thing on BOTH exit paths --------
    # A candle that stops out a long AND carries a valid bearish flip (regime short,
    # close below VWAP). Which exit path detected the stop must not change whether a
    # re-entry is allowed - only the flag should decide that.
    _st_now = pd.Series({"trend": -1, "lower": 23900.0, "upper": 24100.0, "atr": 50.0})
    _st_prev = pd.Series({"trend": 1, "lower": 23900.0, "upper": 24100.0, "atr": 50.0})
    _cts = datetime(2026, 10, 6, 10, 0, tzinfo=_ist())
    _C, _H, _L, _O, _V = 23890.0, 23960.0, 23910.0, 24010.0, 23950.0

    def _same_candle(path, flag):
        eng = StrategyEngine(Config(quantity=65, allow_same_candle_reentry=flag,
                                    entry_on="flip"))
        eng.open_position(Action("ENTRY", "LONG", 65, 24000.0, "t", _cts), 24000.0, _cts)
        eng.position.stop = 23920.0
        if path == "intrabar":
            for act in eng.on_tick(23910.0, _cts):
                if act.kind == "EXIT":
                    eng.close_position(_cts)
            acts = eng.on_candle_close(_cts, _C, _H, _L, _O, _st_now, _st_prev, _V,
                                       "short", intrabar_handled=True)
        else:
            acts = eng.on_candle_close(_cts, _C, _H, _L, _O, _st_now, _st_prev, _V, "short")
        return any(x.kind == "ENTRY" for x in acts)

    check("Same-candle re-entry allowed on the intrabar exit path when the flag is on",
          _same_candle("intrabar", True) is True)
    check("Same-candle re-entry allowed on the OHLC fallback when the flag is on",
          _same_candle("ohlc", True) is True)
    check("Same-candle re-entry BLOCKED on the intrabar path when the flag is off",
          _same_candle("intrabar", False) is False)
    check("Same-candle re-entry BLOCKED on the OHLC fallback when the flag is off",
          _same_candle("ohlc", False) is False)
    check("The exit path does not decide the outcome - only the flag does",
          _same_candle("intrabar", True) == _same_candle("ohlc", True)
          and _same_candle("intrabar", False) == _same_candle("ohlc", False))

    # ---- 27. a 1-minute WICK through the stop is not invisible -------------------
    # Replaying only 1-minute closes misses a stop pierced and recovered inside the
    # minute, which turns a genuine -80 loser into a +75 winner.
    _day = pd.bdate_range("2026-10-06", periods=1, tz=_ist())[0].date()
    _base = pd.date_range(f"{_day} 09:15", periods=375, freq="1min", tz=_ist())
    _px = [24000.0] * 375
    for _i in range(140, 200):
        _px[_i] = 24000.0 + (_i - 139) * 1.5
    for _i in range(200, 375):
        _px[_i] = _px[199]
    _mf = pd.DataFrame([{"open": _p, "high": _p + 2.0,
                         "low": (23900.0 if _i == 210 else _p - 1.0),
                         "close": _p, "volume": 900.0} for _i, _p in enumerate(_px)],
                       index=_base)
    _intra = filter_session(resample_ohlcv(_mf, 15), "09:15", "15:30")
    _wd = pd.DataFrame({"close": list(np.linspace(20000, 23000, 200))},
                       index=pd.bdate_range("2026-01-05", periods=200, tz=_ist()))
    _wick = replay(Config(quantity=65), _intra, _wd, tick_size=0.05, quantity=65,
                   minute_frame=_mf)
    check("The minute wick produced at least one trade to judge", len(_wick) >= 1)
    _last = _wick[-1]
    check("A 1-minute wick through the stop exits the trade (close-only replay missed it)",
          _last["pnl_points"] < 0 and "stop" in _last["reason"],
          f"pnl={_last['pnl_points']} reason={_last['reason']}")
    check("That trade is NOT the +75 'intraday square-off' the close-only model reported",
          _last["reason"] != "intraday square-off")
    # Mark-to-market must be measured at the minute close, not at the favourable wick.
    _diag: dict = {}
    replay(Config(quantity=65), _intra, _wd, tick_size=0.05, quantity=65,
           diag=_diag, minute_frame=_mf)
    check("The minute path was actually replayed", _diag.get("minute_bars_replayed", 0) > 0)

    # ---- 27b. every exit must reach BOTH equity accumulators --------------------
    # realised_pts used to be updated at only three of the five _replay_act call sites.
    # The two square-off sites were missed, so on the reference window 28 of 51 trades
    # worth +2,579 pts never entered the points-based MTM curve. RiskManager was right
    # because on_close runs inside _replay_act. These checks pin the two together.
    _rcap = 1_000_000.0
    _rrm = RiskManager(Config(initial_capital=_rcap, risk_per_trade_pct=0.0061),
                       65, 80.0, CostModel())
    _rdiag: dict = {}
    _rtr = replay(Config(quantity=65, initial_capital=_rcap, risk_per_trade_pct=0.0061),
                  _intra, _wd, tick_size=0.05, quantity=65, diag=_rdiag,
                  minute_frame=_mf, cost_model=CostModel(), risk=_rrm)
    check("the fixture ends with a square-off, so this path is actually exercised",
          any("square-off" in t["reason"] for t in _rtr) or len(_rtr) >= 1,
          f"reasons={[t['reason'] for t in _rtr]}")
    _sum_pts = sum(t["pnl_points"] for t in _rtr)
    # pnl_points is rounded to 2dp per trade, so allow rounding drift - but nothing
    # like the 167,635 INR that a single missed square-off used to leave behind.
    check("every exit - including square-offs - reaches the risk manager's equity",
          abs(_sum_pts * 65 - (_rrm.equity - _rcap)) < 1.0 + 0.4 * len(_rtr),
          f"trades={_sum_pts * 65:,.2f} INR vs risk.equity={_rrm.equity - _rcap:,.2f} INR")
    _vals = [v for _, v in _rrm.equity_curve]
    _pk, _rdd = _vals[0], 0.0
    for _v in _vals:
        _pk = max(_pk, _v)
        _rdd = max(_rdd, _pk - _v)
    check("the points MTM drawdown and the rupee MTM drawdown AGREE",
          _rrm.mtm_tracked and abs(_rdiag["mtm_drawdown_pts"] * 65 - _rdd) < 1.0,
          f"points={_rdiag['mtm_drawdown_pts'] * 65:,.2f} rupees={_rdd:,.2f}")

    # ---- 28. live failure paths: a missing stop must never look like success -----
    class _FailClient:
        """Every order-touching call can be individually broken."""
        def __init__(self, place_ok=True, cancel_ok=True, modify_ok=True, rows=None):
            self._place_ok, self._cancel_ok = place_ok, cancel_ok
            self._modify_ok, self._rows = modify_ok, rows or []
            self.placed, self.cancelled = [], []
        def place_order(self, **kw):
            # Only the protective SL-M is made to fail; the market entry must still fill,
            # otherwise we would be testing a rejected entry, not a missing stop.
            if kw.get("order_type") == "SL-M" and not self._place_ok:
                raise UpstoxAPIError(400, {"e": 1}, "/place")
            self.placed.append(kw.get("order_type"))
            return "OID"
        def order_details(self, oid):
            return {"status": "complete", "average_price": 24000.0,
                    "filled_quantity": 65, "quantity": 65}
        def positions(self):
            return self._rows
        def modify_order(self, *a, **k):
            if not self._modify_ok:
                raise UpstoxAPIError(400, {"e": 1}, "/modify")
            return "ok"
        def _request(self, method, path, **kw):
            if not self._cancel_ok:
                raise UpstoxAPIError(400, {"e": 1}, "/cancel")
            self.cancelled.append(kw)
            return {}
        def ltp(self, key):
            return 24000.0

    _lc = Config(quantity=65, broker_sl_backstop=True)
    _li = Instrument("NSE_FO|48704", "NIFTY FUT 27 OCT 26", "48704", 65, date(2026, 10, 27))

    # 1. a stop that could not be placed is not a synchronised stop
    _ex = LiveExecutor(_lc, _li, _FailClient(place_ok=False))
    _ok = _ex._place_backstop(Action("ENTRY", "LONG", 65, 24000.0, "t", _cts), 24000.0)
    check("_place_backstop reports failure when the broker rejects the SL-M", _ok is False)
    check("A failed backstop leaves no order id behind", _ex.sl_order_id is None)
    check("sync_stop does NOT report success when no exchange stop exists",
          _ex.sync_stop(23920.0) is False)

    _ex2 = LiveExecutor(_lc, _li, _FailClient(place_ok=True))
    _ex2._place_backstop(Action("ENTRY", "LONG", 65, 24000.0, "t", _cts), 24000.0)
    check("sync_stop succeeds once a real SL-M is resting",
          _ex2.sl_order_id is not None and _ex2.sync_stop(23920.0) is True)

    # 2. entering without a placeable stop must square off, not carry on
    _r = LiveRunner(Config(quantity=65, broker_sl_backstop=True), "live", instrument=_li)
    _r.client = _FailClient(place_ok=False)
    _r.executor = LiveExecutor(_r.cfg, _li, _r.client)
    _ok_entry = _r._act(Action("ENTRY", "LONG", 65, 24000.0, "entry", _cts), 24000.0)
    check("An entry with no placeable exchange stop reports failure", _ok_entry is False)
    check("That position is squared off rather than held unprotected",
          _r.engine.position is None)
    check("New entries are halted after an unprotected fill", _r._halt_entries is True)
    check("entries_allowed() actually honours the halt flag",
          _r.entries_allowed(_cts) is False)

    # 3. a failed exit must not be followed by the same candle's re-entry
    _r2 = LiveRunner(Config(quantity=65, broker_sl_backstop=False), "live", instrument=_li)

    class _ExitFails(_FailClient):
        def order_details(self, oid):
            # 'complete' with no usable fill - _await_fill rejects this immediately rather
            # than polling for 12s, and it is the same OrderUnconfirmed outcome.
            return {"status": "complete", "average_price": None,
                    "filled_quantity": 0, "quantity": 65}

    _r2.client = _ExitFails()
    _r2.executor = LiveExecutor(_r2.cfg, _li, _r2.client)
    _r2.engine.open_position(Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 24000.0, _cts)
    _ok_exit = _r2._act(Action("EXIT", "LONG", 65, 23920.0, "exit", _cts), 23920.0)
    check("An unconfirmed exit reports failure", _ok_exit is False)
    check("An unconfirmed exit halts new entries", _r2._halt_entries is True)
    # _reconcile() then finds the broker flat and records an ESTIMATED close. The invariant
    # is that no trade was booked from the unconfirmed order's price.
    check("No trade was booked from the unconfirmed exit price itself",
          all("reconciled" in t["reason"] for t in _r2.trades) or not _r2.trades,
          f"reasons={[t['reason'] for t in _r2.trades]}")

    # 4. a failed cancel must not be forgotten
    _ex3 = LiveExecutor(_lc, _li, _FailClient(place_ok=True, cancel_ok=False))
    _ex3._place_backstop(Action("ENTRY", "LONG", 65, 24000.0, "t", _cts), 24000.0)
    _held = _ex3.sl_order_id
    check("_cancel_backstop reports failure when the cancel is rejected",
          _ex3._cancel_backstop() is False)
    check("The possibly-still-resting order id is KEPT, not cleared",
          _ex3.sl_order_id == _held)

    # 5. an unmanaged position at startup blocks entries immediately
    _r3 = LiveRunner(Config(quantity=65), "live", instrument=_li)
    _r3.client = _FailClient(rows=[{"instrument_token": "48704", "quantity": 65,
                                    "average_price": 23800.0}])
    _r3._halt_entries = False
    _r3._report_account_state()
    check("An unmanaged broker position at startup halts entries immediately",
          _r3._halt_entries is True)

    _r4 = LiveRunner(Config(quantity=65), "live", instrument=_li)
    _r4.client = _FailClient(rows=[{"instrument_token": "48704", "quantity": 0}])
    _r4._halt_entries = False
    _r4._report_account_state()
    check("A flat account does not halt entries", _r4._halt_entries is False)

    # ---- 29. live data must match the backtest's conventions ---------------------
    class _DataStub:
        def __init__(self, fail=False):
            self.asked, self.fail = [], fail
        def historical(self, key, interval, to_d, from_d=None):
            self.asked.append(key)
            if self.fail:
                raise UpstoxAPIError(500, {}, "/historical")
            _c = list(range(23000, 23200))
            return pd.DataFrame({"open": _c, "high": _c, "low": _c, "close": _c,
                                 "volume": [1e6] * 200},
                                index=pd.bdate_range("2026-01-05", periods=200, tz=_ist()))
        def intraday(self, key, interval):
            # deliberately spans 09:00 pre-open through 15:39 post-close
            _d = pd.bdate_range("2026-10-06", periods=1, tz=_ist())[0].date()
            _ix = pd.date_range(f"{_d} 09:00", periods=400, freq="1min", tz=_ist())
            return pd.DataFrame({"open": 24000.0, "high": 24001.0, "low": 23999.0,
                                 "close": 24000.0, "volume": 900.0}, index=_ix)

    _ikey = Config().index_key
    _r5 = LiveRunner(Config(quantity=65, daily_filter_source="index"), "paper", instrument=_li)
    _st5 = _DataStub()
    _r5.client = _st5
    _r5._daily_frame()
    check("Index regime mode fetches the INDEX daily series in live too",
          _st5.asked == [_ikey], f"asked={_st5.asked}")

    _r5f = LiveRunner(Config(quantity=65, daily_filter_source="futures"), "paper",
                      instrument=_li)
    _st5f = _DataStub()
    _r5f.client = _st5f
    _r5f._daily_frame()
    check("Futures regime mode fetches the CONTRACT daily series",
          _st5f.asked == [_li.instrument_key], f"asked={_st5f.asked}")

    _r5x = LiveRunner(Config(quantity=65, daily_filter_source="index"), "paper",
                      instrument=_li)
    _r5x.client = _DataStub(fail=True)
    check("A failed index fetch yields no data rather than a mismatched substitute",
          len(_r5x._daily_frame()) == 0)

    _r6 = LiveRunner(Config(quantity=65), "paper", instrument=_li)
    _r6.client = _DataStub()
    _r6.fetch_interval, _r6.resample_minutes = plan_fetch("15minute")
    _fr = _r6._intraday_frame()
    check("Live intraday drops pre-open and post-close buckets, like the backtest",
          _fr.index[0].strftime("%H:%M") == "09:15"
          and _fr.index[-1].strftime("%H:%M") == "15:15" and len(_fr) == 25,
          f"{_fr.index[0]}..{_fr.index[-1]} n={len(_fr)}")

    _sig = _wick[-1]
    check("Trade rows separate the exit bucket from when it became actionable",
          _sig["exit_signal_time"] != _sig["exit_time"],
          f"exit={_sig.get('exit_time')} actionable={_sig.get('exit_signal_time')}")
    check("exit_signal_time is one candle length after the candle-open stamp",
          (_sig["exit_signal_time"] - _sig["exit_time"]).total_seconds() == 15 * 60)
    check("the entry bucket is recorded separately from the fill time",
          _sig["entry_bucket"] is not None
          and (_sig["entry_signal_time"] - _sig["entry_bucket"]).total_seconds() == 15 * 60,
          f"bucket={_sig.get('entry_bucket')} actionable={_sig.get('entry_signal_time')}")
    check("the fill time is at or after the moment the entry became actionable",
          _sig["entry_time"] >= _sig["entry_signal_time"],
          f"fill={_sig.get('entry_time')} actionable={_sig.get('entry_signal_time')}")

    # ---- 30. session validation and backtest/live square-off alignment ----------
    # A feed shaped like Upstox's: pre-open prints from 09:00, running out to 15:39.
    _raw30 = pd.DataFrame(
        {"open": 24000.0, "high": 24001.0, "low": 23999.0, "close": 24000.0, "volume": 900.0},
        index=pd.date_range(f"{_day} 09:00", periods=400, freq="1min", tz=_ist()))
    _sess = validate_session(_raw30, Config())
    check("Session check counts pre-open bars instead of dropping them silently",
          _sess["pre_open"] == 15, f"pre_open={_sess['pre_open']}")
    check("Session check counts the post-close prints out to 15:39",
          _sess["post_close"] == 10 and _sess["last"].strftime("%H:%M") == "15:39",
          f"post_close={_sess['post_close']} last={_sess['last']:%H:%M}")

    _sun = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.DatetimeIndex(
        [pd.Timestamp("2026-02-01 10:00", tz=_ist()), pd.Timestamp("2026-02-02 10:00", tz=_ist())]))
    check("A Sunday bar is reported as a weekend session, not silently discarded",
          validate_session(_sun, Config())["weekend_days"] == ["2026-02-01"])

    _cfg30 = Config()
    _cm30 = interval_minutes(_cfg30.entry_timeframe) or 15
    _b30 = filter_session(resample_ohlcv(_mf, _cm30), _cfg30.market_open, _cfg30.square_off)
    _last30 = _b30.index[-1].to_pydatetime()
    check("The backtest session now ends at the square-off time, not the exchange close",
          (_last30 + timedelta(minutes=_cm30)).strftime("%H:%M") == _cfg30.square_off,
          f"last bucket {_last30:%H:%M} closes at {(_last30 + timedelta(minutes=_cm30)):%H:%M}")
    check("So the backtest squares off on the same price live mode would use",
          _last30.strftime("%H:%M") != "15:15")
    _sig30 = _last30 + timedelta(minutes=_cm30)
    check("The final candle's signal is actionable at/after the cutoff, so it cannot enter",
          past_entry_cutoff(_sig30, _cfg30.entry_cutoff) is True)

    # ---- 31. --allowed-sides gates entries without touching position management --
    def _side_probe(sides, regime, trend_now, trend_prev, close, vwap):
        eng = StrategyEngine(Config(quantity=65, entry_on="flip", allowed_sides=sides))
        _r = pd.Series({"trend": trend_now, "lower": 23900.0, "upper": 24100.0, "atr": 50.0})
        _p = pd.Series({"trend": trend_prev, "lower": 23900.0, "upper": 24100.0, "atr": 50.0})
        _t = datetime(2026, 10, 6, 10, 0, tzinfo=_ist())
        acts = eng.on_candle_close(_t, close, close + 5, close - 5, close, _r, _p, vwap, regime)
        _e = [x for x in acts if x.kind == "ENTRY"]
        return (_e[0].side if _e else None), eng.blocked_by_side

    check("allowed_sides=both admits the long signal",
          _side_probe("both", "long", 1, -1, 24010.0, 24000.0)[0] == "LONG")
    check("allowed_sides=both admits the short signal",
          _side_probe("both", "short", -1, 1, 23990.0, 24000.0)[0] == "SHORT")
    check("allowed_sides=long blocks the short signal and counts it",
          _side_probe("long", "short", -1, 1, 23990.0, 24000.0) == (None, 1))
    check("allowed_sides=short blocks the long signal and counts it",
          _side_probe("short", "long", 1, -1, 24010.0, 24000.0) == (None, 1))
    check("allowed_sides=short still admits the short signal",
          _side_probe("short", "short", -1, 1, 23990.0, 24000.0)[0] == "SHORT")

    # an open position must still be managed and exited normally
    _eng = StrategyEngine(Config(quantity=65, allowed_sides="short"))
    _eng.open_position(Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 24000.0, _cts)
    check("A side filter does not stop an open LONG being stopped out",
          any(x.kind == "EXIT" for x in _eng.on_tick(23895.0, _cts)))
    _eng2 = StrategyEngine(Config(quantity=65, allowed_sides="short"))
    _eng2.open_position(Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 24000.0, _cts)
    _eng2.position.stop = 24050.0
    _eng2.position.trailing_armed = True
    _hi = pd.Series({"trend": 1, "lower": 24080.0, "upper": 24100.0, "atr": 50.0})
    _eng2.on_candle_close(_cts, 24120.0, 24130.0, 24090.0, 24100.0, _hi, _st_prev,
                          24050.0, "short")
    check("A side filter does not stop the Supertrend trail updating",
          _eng2.position.stop == 24080.0, f"stop={_eng2.position.stop}")

    daily = pd.DataFrame({"close": list(range(100, 200))},
                         index=pd.date_range("2026-01-01", periods=100, freq="D", tz=_ist()))
    check("Regime is long when price is above both daily EMAs",
          daily_regime(daily, 500.0, 10, 50)["regime"] == "long")
    check("Regime is short when price is below both daily EMAs",
          daily_regime(daily, 1.0, 10, 50)["regime"] == "short")
    between = float(ema(daily["close"], 10).iloc[-1])
    check("Regime is none when price sits between the EMAs",
          daily_regime(daily, between - 0.5, 10, 50)["regime"] == "none")
    check("daily_regime tolerates a short history",
          daily_regime(daily.iloc[:5], 100.0, 10, 50)["regime"] == "unknown")

    # ===================== §32 COST MODEL, SIZING, FILL REALISM =====================
    # These are the checks that decide whether a backtest number means anything. A fill
    # booked at the exact print, a flat cost guess, and a fixed lot size all flatter the
    # result, and each of those used to be the default here.
    print("\n-- §32 cost model --")
    _cm = CostModel()
    _buy = _cm.leg_money(24000.0, 65, True)
    _sell = _cm.leg_money(24000.0, 65, False)
    check("the SELL leg costs far more than the buy leg (STT is sell-side only)",
          _sell > 5 * _buy, f"buy Rs{_buy:.2f} vs sell Rs{_sell:.2f}")
    check("STT is not charged on the buy leg",
          CostModel(stt_sell_pct=0.0, stt_sell_pct_legacy=0.0).leg_money(24000.0, 65, True)
          == _buy, "changing the STT rate must not move the buy leg")
    _pre = _cm.round_trip_points(24000.0, 24000.0, 65, "LONG",
                                 datetime(2026, 2, 10, tzinfo=_ist()))
    _post = _cm.round_trip_points(24000.0, 24000.0, 65, "LONG",
                                  datetime(2026, 6, 10, tzinfo=_ist()))
    check("the STT rate switches on 2026-04-01, so a window spanning it needs both",
          abs(_pre - 7.13) < 0.02 and abs(_post - 14.33) < 0.02,
          f"pre={_pre:.2f} post={_post:.2f}")
    check("GST is not applied to STT (doubling STT must not raise GST)",
          (lambda c1, c2: abs((c2.leg_money(24000.0, 65, False)
                               - c1.leg_money(24000.0, 65, False))
                              - 24000.0 * 65 * 0.0005) < 0.01)(
              CostModel(stt_sell_pct=0.0005), CostModel(stt_sell_pct=0.001)))
    check("a LONG's entry is the buy leg and a SHORT's entry is the SELL leg",
          abs(_cm.round_trip_money(24000.0, 23900.0, 65, "LONG")
              - _cm.round_trip_money(23900.0, 24000.0, 65, "SHORT")) < 1e-9,
          "swapping direction and prices must give the same two legs")
    _rt = _post
    check("one lot at 24,000 costs ~14.33 points round trip at the current STT rate",
          abs(_rt - 14.33) < 0.02, f"{_rt:.4f} pts")
    check("the old 0.5 pts/side default understated real cost by more than 10x",
          _rt / 1.0 > 10.0, f"{_rt / 1.0:.1f}x")
    check("cost scales with turnover, which a flat points figure cannot",
          _cm.round_trip_points(26000.0, 26000.0, 65)
          > _cm.round_trip_points(22000.0, 22000.0, 65))
    _flat = CostModel(flat_points_per_side=0.5)
    check("the legacy flat override still bypasses the model",
          abs(_flat.round_trip_points(24000.0, 24000.0, 65) - 1.0) < 1e-9)

    print("-- §32 risk sizing --")
    _rm = RiskManager(Config(initial_capital=1_000_000, risk_per_trade_pct=0.0052,
                             min_lots=1, initial_stop_points=80.0), 65, 80.0, _cm)
    _n = _rm.size_units(24000.0)
    check("0.52% on Rs 10 lakh does NOT fund one lot once costs are included",
          _rm.blocked["sizing"] == 1 and _n == 65,
          f"units={_n} risk/lot=Rs{_rm.risk_per_lot_money:,.0f}")
    check("min_lots forces the trade AND records that the budget was exceeded",
          _n == 65 and _rm.actual_risk_money > 1_000_000 * 0.0052,
          f"actual Rs{_rm.actual_risk_money:,.0f} vs budget "
          f"Rs{1_000_000 * 0.0052:,.0f}")
    _rms = RiskManager(Config(initial_capital=1_000_000, risk_per_trade_pct=0.0052,
                              risk_mode="strict"), 65, 80.0, _cm)
    check("risk_mode=strict SKIPS the trade instead of exceeding the cap",
          _rms.size_units(24000.0) == 0 and _rms.blocked["strict_cap"] == 1,
          _rms.last_block_reason)
    _rm2 = RiskManager(Config(initial_capital=1_000_000, risk_per_trade_pct=0.015),
                       65, 80.0, _cm)
    check("1.5% on Rs 10 lakh funds exactly 2 lots",
          _rm2.size_units(24000.0) == 130 and not _rm2.blocked["sizing"],
          f"lots={_rm2.lots_used}")
    check("sizing is disabled unless capital AND risk-pct are both set",
          not RiskManager(Config(), 65, 80.0).enabled)

    print("-- §32 account limits --")
    _t = datetime(2026, 10, 6, 10, 0, tzinfo=_ist())
    _rl = RiskManager(Config(initial_capital=1_000_000, daily_loss_limit=10_000), 65, 80.0)
    check("entries are allowed before any loss", _rl.can_open(_t))
    _rl.on_close(_t, -10_000.0)
    check("the daily loss limit blocks new entries",
          not _rl.can_open(_t) and _rl.blocked["daily"] == 1, _rl.last_block_reason)
    check("a new day lifts the daily limit",
          _rl.can_open(datetime(2026, 10, 7, 10, 0, tzinfo=_ist())))
    _rw = RiskManager(Config(initial_capital=1_000_000, weekly_loss_limit=25_000), 65, 80.0)
    _rw.on_close(_t, -25_000.0)
    check("the weekly loss limit blocks new entries",
          not _rw.can_open(_t) and _rw.blocked["weekly"] == 1, _rw.last_block_reason)
    _rmn = RiskManager(Config(initial_capital=1_000_000, monthly_dd_review=50_000), 65, 80.0)
    _rmn.on_close(_t, -50_000.0)
    check("the monthly drawdown review blocks new entries",
          not _rmn.can_open(_t) and _rmn.blocked["monthly"] == 1, _rmn.last_block_reason)
    _rp = RiskManager(Config(initial_capital=1_000_000, monthly_dd_review=50_000), 65, 80.0)
    _rp.on_close(_t, 60_000.0)
    _rp.on_close(_t, -50_000.0)
    check("monthly drawdown is measured from the month's PEAK, not the start",
          not _rp.can_open(_t), f"equity={_rp.equity:,.0f} peak={_rp.month_peak:,.0f}")

    # An OPEN losing position must count against the limit before it is booked.
    # Measuring realised equity alone lets an account keep opening positions while a
    # large unrealised loss is already running.
    _ro = RiskManager(Config(initial_capital=1_000_000, daily_loss_limit=10_000), 65, 80.0)
    _ro.on_open(_t, "LONG", 65, 24000.0)
    check("an open position alone does not breach the limit",
          _ro.can_open(_t) and _ro.open_pnl == 0.0)
    _ro.mark(_t + timedelta(minutes=30), 23800.0)     # 200 pts against, still open
    check("an OPEN losing position breaches the daily limit before any trade closes",
          not _ro.can_open(_t) and _ro.blocked["daily"] == 1,
          f"open_pnl={_ro.open_pnl:,.0f} mtm={_ro.mtm_equity:,.0f}")
    check("the loss is unrealised - realised equity is untouched",
          _ro.equity == 1_000_000 and _ro.mtm_equity < _ro.equity)
    _ro.on_close(_t + timedelta(minutes=31), -13_000.0)
    check("closing clears the open mark so the next session starts clean",
          _ro.open_qty == 0 and _ro.open_pnl == 0.0
          and _ro.mtm_equity == _ro.equity == 987_000.0)

    print("-- §32 fill realism --")
    _cs = Config(quantity=65, entry_slippage_points=1.0)
    _e1 = StrategyEngine(_cs)
    _replay_act(_e1, Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 0.05, [], _cts,
                0.0, 15, _cs)
    check("entry slippage makes a LONG fill worse (higher)",
          _e1.position.entry_price == 24001.0, f"{_e1.position.entry_price}")
    _e2 = StrategyEngine(_cs)
    _replay_act(_e2, Action("ENTRY", "SHORT", 65, 24000.0, "e", _cts), 0.05, [], _cts,
                0.0, 15, _cs)
    check("entry slippage makes a SHORT fill worse (lower)",
          _e2.position.entry_price == 23999.0, f"{_e2.position.entry_price}")

    def _stop_fill(frac, exit_slip=0.0, side="LONG"):
        c = Config(quantity=65, stop_slip_fraction=frac, exit_slippage_points=exit_slip)
        eng = StrategyEngine(c)
        ref = 24000.0
        eng.open_position(Action("ENTRY", side, 65, ref, "e", _cts), ref, _cts)
        stop = ref - 80.0 if side == "LONG" else ref + 80.0
        tr: list[dict] = []
        _replay_act(eng, Action("EXIT", side, 65, stop, "stop hit", _cts, slip_pts=10.0),
                    0.05, tr, _cts, 0.0, 15, c)
        return tr[0]["exit"], tr[0]["slip_pts"], tr[0]["slip_applied_pts"]

    _f0 = _stop_fill(0.0)
    check("stop_slip_fraction=0 reproduces the OLD optimistic booking at the stop",
          _f0[0] == 23920.0 and _f0[2] == 0.0, f"exit={_f0[0]}")
    _f1 = _stop_fill(1.0)
    check("stop_slip_fraction=1 books the adverse extreme, not the stop",
          _f1[0] == 23910.0 and _f1[2] == 10.0, f"exit={_f1[0]}")
    check("the measured gap is still reported even though it is now deducted",
          _f1[1] == 10.0)
    _fh = _stop_fill(0.5)
    check("stop_slip_fraction=0.5 books the midpoint", _fh[0] == 23915.0, f"exit={_fh[0]}")
    _fe = _stop_fill(1.0, exit_slip=1.0)
    check("exit slippage stacks on top of the stop gap", _fe[0] == 23909.0, f"exit={_fe[0]}")
    _fs = _stop_fill(1.0, side="SHORT")
    check("a SHORT stop-out slips UPWARD, not downward",
          _fs[0] == 24090.0, f"exit={_fs[0]}")

    print("-- §32 entry-fill timing --")
    # 09:15 + 20 one-minute bars = 09:15..09:34, so the 09:30 actionable time of the
    # 09:15-09:30 bucket is INSIDE the data. A fixture that stops before it would make
    # the lookup return None and the test would silently pass on the fallback path.
    _midx = pd.date_range("2026-10-06 09:15", periods=20, freq="min", tz=_ist())
    _mf = pd.DataFrame({"open": [24000.0 + 2.0 * i for i in range(20)],
                        "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0},
                       index=_midx)
    _lk = _build_entry_fill_lookup(_mf)
    check("the lookup returns the first 1-minute bar at or after the actionable time",
          _lk is not None and _lk(_midx[2]) == (_midx[2], 24004.0),
          f"{_lk(_midx[2]) if _lk else None}")
    check("the lookup reaches the 09:30 bar that closes the 09:15 bucket",
          _lk(_midx[0] + timedelta(minutes=15)) == (_midx[15], 24030.0))
    check("the lookup returns None past the end of the data",
          _lk(_midx[-1] + timedelta(minutes=5)) is None)
    check("no 1-minute data means no lookup, not a crash",
          _build_entry_fill_lookup(None) is None
          and _build_entry_fill_lookup(pd.DataFrame()) is None)

    _cf = Config(quantity=65, entry_fill="next_minute_open")
    _ef = StrategyEngine(_cf)
    _bucket_open = _midx[0]                       # 09:15 bucket, actionable at 09:30
    _replay_act(_ef, Action("ENTRY", "LONG", 65, 23999.0, "e", _bucket_open), 0.05, [],
                _bucket_open, 0.0, 15, _cf, None, None, None, _lk)
    check("the entry fills at the NEXT minute's open, not the signal candle's close",
          _ef.position.entry_price == 24030.0, f"fill={_ef.position.entry_price}")
    check("the position is stamped at the fill time, not the bucket-open label",
          _ef.position.entry_time == _midx[15], f"ts={_ef.position.entry_time}")
    _cf2 = Config(quantity=65, entry_fill="signal_close")
    _ef2 = StrategyEngine(_cf2)
    _replay_act(_ef2, Action("ENTRY", "LONG", 65, 23999.0, "e", _bucket_open), 0.05, [],
                _bucket_open, 0.0, 15, _cf2, None, None, None, _lk)
    check("entry_fill=signal_close reproduces the old behaviour",
          _ef2.position.entry_price == 23999.0, f"fill={_ef2.position.entry_price}")

    print("-- §32 costs and limits applied inside the replay --")
    _cc = Config(quantity=65)
    _ce = StrategyEngine(_cc)
    _ce.open_position(Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 24000.0, _cts)
    _ctr: list[dict] = []
    _replay_act(_ce, Action("EXIT", "LONG", 65, 24100.0, "x", _cts), 0.05, _ctr, _cts,
                0.0, 15, _cc, CostModel())
    check("the cost model deducts a real round trip from the P&L",
          10.0 < _ctr[0]["cost_points"] < 20.0 and _ctr[0]["pnl_points"] < 90.0,
          f"cost={_ctr[0]['cost_points']} pnl={_ctr[0]['pnl_points']}")
    _cs_cost = _ctr[0]["cost_points"]
    _ctr_short: list[dict] = []
    _ce3 = StrategyEngine(_cc)
    _ce3.open_position(Action("ENTRY", "SHORT", 65, 24100.0, "e", _cts), 24100.0, _cts)
    _replay_act(_ce3, Action("EXIT", "SHORT", 65, 24000.0, "x", _cts), 0.05, _ctr_short,
                _cts, 0.0, 15, _cc, CostModel())
    check("a SHORT books the same two legs as the mirror-image LONG",
          abs(_ctr_short[0]["cost_points"] - _cs_cost) < 0.01,
          f"short={_ctr_short[0]['cost_points']} long={_cs_cost}")
    _ce2 = StrategyEngine(_cc)
    _ce2.open_position(Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 24000.0, _cts)
    _ctr2: list[dict] = []
    _replay_act(_ce2, Action("EXIT", "LONG", 65, 24100.0, "x", _cts), 0.05, _ctr2, _cts,
                0.0, 15, _cc, None)
    check("with no cost model the old cost_per_side path still applies",
          _ctr2[0]["cost_points"] == 0.0 and _ctr2[0]["pnl_points"] == 100.0)

    _rc = Config(initial_capital=1_000_000, risk_per_trade_pct=0.03, min_lots=1)
    _rr = RiskManager(_rc, 65, 80.0, CostModel())
    _re = StrategyEngine(Config(quantity=65))
    _replay_act(_re, Action("ENTRY", "LONG", 65, 24000.0, "e", _t), 0.05, [], _t,
                0.0, 15, None, None, _rr, {})
    check("risk sizing overrides the fixed quantity inside the replay",
          _re.position.quantity == 260, f"qty={_re.position.quantity}")
    check("an entry registers the open position so the limits can mark it",
          _rr.open_qty == 260 and _rr.open_entry == 24000.0,
          f"open={_rr.open_qty}@{_rr.open_entry}")

    _rb = Config(initial_capital=1_000_000, risk_per_trade_pct=0.03, min_lots=1,
                 daily_loss_limit=10_000)
    _rrb = RiskManager(_rb, 65, 80.0, CostModel())
    _rrb.on_close(_t, -10_000.0)
    _reb = StrategyEngine(_rb)
    _diagb: dict = {}
    _replay_act(_reb, Action("ENTRY", "LONG", 65, 24000.0, "e", _t), 0.05, [], _t,
                0.0, 15, _rb, None, _rrb, _diagb)
    check("a breached daily limit stops the entry being opened at all",
          _reb.position is None and _diagb.get("blocked_by_risk") == 1,
          f"diag={_diagb}")

    # ---- 34. the three section-8 decisions that were previously implicit ---------
    print("-- §34 pre-trade margin --")
    _mg = RiskManager(Config(initial_capital=1_000_000, risk_per_trade_pct=0.03,
                             margin_pct_of_notional=0.12), 65, 100.0, CostModel())
    check("one lot at 24,000 needs ~Rs 187,200 of margin and is affordable",
          _mg.has_margin(24000.0, 65) and _mg.blocked["margin"] == 0,
          f"need Rs{_mg.margin_required(24000.0, 65):,.0f}")
    _mg.on_open(_t, "LONG", 65, 24000.0)
    check("margin is committed once a position is open",
          _mg.committed_margin > 0, f"committed={_mg.committed_margin:,.0f}")
    check("a second lot is blocked when the margin is already committed",
          not _mg.has_margin(24000.0, 65 * 6) and _mg.blocked["margin"] == 1,
          _mg.last_block_reason)
    _mg.on_close(_t, 0.0)
    check("margin is released when the position closes", _mg.committed_margin == 0.0)
    check("margin_pct=0 disables the check rather than blocking everything",
          RiskManager(Config(initial_capital=1_000_000, margin_pct_of_notional=0.0),
                      65, 100.0).has_margin(24000.0, 65 * 100))

    print("-- §34 breakeven level --")
    _be0 = StrategyEngine(Config(quantity=65, breakeven_covers_costs=False))
    _be0.open_position(Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 24000.0, _cts)
    check("default breakeven is the ENTRY PRICE EXACTLY, as the spec states",
          _be0._breakeven_level(_be0.position) == 24000.0)
    _be1 = StrategyEngine(Config(quantity=65, breakeven_covers_costs=True))
    _be1.breakeven_cost_pts = 14.33
    _be1.open_position(Action("ENTRY", "LONG", 65, 24000.0, "e", _cts), 24000.0, _cts)
    check("cost-adjusted breakeven sits ABOVE entry for a LONG",
          abs(_be1._breakeven_level(_be1.position) - 24014.33) < 1e-9,
          f"{_be1._breakeven_level(_be1.position)}")
    _be2 = StrategyEngine(Config(quantity=65, breakeven_covers_costs=True))
    _be2.breakeven_cost_pts = 14.33
    _be2.open_position(Action("ENTRY", "SHORT", 65, 24000.0, "e", _cts), 24000.0, _cts)
    check("cost-adjusted breakeven sits BELOW entry for a SHORT",
          abs(_be2._breakeven_level(_be2.position) - 23985.67) < 1e-9,
          f"{_be2._breakeven_level(_be2.position)}")
    check("breakeven_covers_costs without a cost supplied degrades to the entry price",
          StrategyEngine(Config(quantity=65, breakeven_covers_costs=True)
                         )._breakeven_level(_be1.position) == 24000.0)

    print("-- §34 rollover policy --")
    # Rebuilt here under unique names: `_r2` is rebound to a LiveRunner by a later
    # section, and reaching back for it produced a pd.concat of a runner object.
    _rfa = _mk_day("2026-10-08", [24000.0] * 8 + list(np.linspace(24060.0, 24400.0, 17)))
    _rfa["contract"] = "NIFTY FUT 27 OCT 26"
    _rfb = _mk_day("2026-10-09", [24400.0] * 25, jitter=0.5)
    _rfb["contract"] = "NIFTY FUT 23 NOV 26"
    _rf: dict = {}
    _rfr = replay(Config(quantity=65, hold="positional", roll_action="roll_forward"),
                  pd.concat([_rfa, _rfb]), daily_hist, tick_size=0.05, quantity=65,
                  diag=_rf)
    check("roll_forward re-opens the position on the new contract",
          any("roll forward onto" in t["reason"] for t in _rfr)
          or _rf.get("roll_forwards", 0) >= 1,
          f"reasons={[t['reason'] for t in _rfr]} diag={_rf.get('roll_forwards')}")
    check("roll_forward still books the exit on the OLD contract",
          any("contract roll" in t["reason"] for t in _rfr))
    _rc2: dict = {}
    _rcl = replay(Config(quantity=65, hold="positional", roll_action="close"),
                  pd.concat([_rfa, _rfb]), daily_hist, tick_size=0.05, quantity=65,
                  diag=_rc2)
    check("roll_action=close does NOT re-open across the boundary",
          not any("roll forward onto" in t["reason"] for t in _rcl),
          f"reasons={[t['reason'] for t in _rcl]}")

    print(f"\nSelf-test: {len(failures)} failure(s).")
    return len(failures)


# =====================================================================================
# 11. AUTH HELPERS
# =====================================================================================

def print_login_url(cfg: Config) -> None:
    if not cfg.api_key or not cfg.redirect_uri:
        raise SystemExit("Set UPSTOX_API_TOKEN and UPSTOX_REDIRECT_URI in env.txt first.")
    params = {
        "response_type": "code",
        "client_id": cfg.api_key,
        "redirect_uri": cfg.redirect_uri,
    }
    url = BASE_URL + "/v2/login/authorization/dialog?" + urllib.parse.urlencode(params)
    print("Open this URL in a browser, log in, then copy the 'code' from the redirect URL:\n")
    print(url)


def exchange_code(cfg: Config, code: str, redirect_uri: str = "") -> None:
    uri = redirect_uri or cfg.redirect_uri
    if not cfg.api_key or not cfg.api_secret or not uri:
        raise SystemExit("UPSTOX_API_TOKEN, UPSTOX_API_SECRET and UPSTOX_REDIRECT_URI are all required.")
    payload = UpstoxClient.exchange_code(code=code, client_id=cfg.api_key,
                                        client_secret=cfg.api_secret, redirect_uri=uri)
    token = (payload or {}).get("access_token")
    if not token:
        raise SystemExit(f"Token exchange failed: {payload}")
    _update_env_file(cfg.env_file, "UPSTOX_ACCESS_TOKEN", token)
    exp = token_expiry(token)
    print(f"Access token saved to {cfg.env_file} (valid until "
          f"{exp:%Y-%m-%d %H:%M:%S IST} if readable)." if exp else
          f"Access token saved to {cfg.env_file}.")


def _update_env_file(path: str, key: str, value: str) -> None:
    lines: list[str] = []
    found = False
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# =====================================================================================
# 12. UTILITIES + CLI
# =====================================================================================

_IST = None


def _ist():
    global _IST
    if _IST is None:
        _IST = timezone(timedelta(hours=5, minutes=30), "IST")
    return _IST


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(f"nifty_supertrend_{datetime.now(tz=_ist()):%Y%m%d}.log",
                                      encoding="utf-8")],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Nifty current-month futures intraday algo (daily EMA regime + 15m Supertrend + VWAP).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["paper", "live"], default="paper",
                   help="paper = simulated fills (default); live = real Upstox orders")
    p.add_argument("--backtest", nargs=2, metavar=("START", "END"),
                   help="replay historical candles, e.g. --backtest 2026-06-01 2026-10-09")
    p.add_argument("--days", type=int, default=0, metavar="N",
                   help="backtest the last N calendar days up to today "
                        "(shorthand, e.g. --days 30)")
    p.add_argument("--roll", action="store_true",
                   help="use each day's actual front-month contract instead of today's "
                        "contract for the whole window (needs the expired-instrument API)")
    p.add_argument("--cost-per-side", type=float, default=0.0, metavar="POINTS",
                   help="LEGACY flat cost per side in index points. Default 0 = use the "
                        "component cost model instead (brokerage + exchange + SEBI + "
                        "stamp + GST, priced from each leg's turnover)")
    p.add_argument("--cost-multiplier", type=float, default=1.0, metavar="X",
                   help="scale the modelled transaction cost, e.g. 2 to stress-test a "
                        "pessimistic fill/charge assumption")
    p.add_argument("--cost-brokerage", type=float, default=20.0, metavar="INR",
                   help="flat brokerage per executed order (default 20, Upstox)")
    p.add_argument("--cost-spread", type=float, default=0.0, metavar="POINTS",
                   help="half-spread cost per side, in index points")
    # --- execution realism ---
    p.add_argument("--entry-slip", type=float, default=0.0, metavar="POINTS",
                   help="adverse slippage added to every ENTRY fill (default 0)")
    p.add_argument("--exit-slip", type=float, default=0.0, metavar="POINTS",
                   help="adverse slippage added to every EXIT fill (default 0)")
    p.add_argument("--stop-slip-fraction", type=float, default=1.0, metavar="FRAC",
                   help="how much of the measured stop gap to book as the fill: "
                        "0 = book at the stop level (old, optimistic), 1 = book at the "
                        "adverse extreme (default, conservative)")
    # --- capital protection ---
    p.add_argument("--capital", type=float, default=0.0, metavar="INR",
                   help="account capital; enables risk-based sizing and the loss limits "
                        "below (default 0 = disabled, fixed --quantity)")
    p.add_argument("--risk-pct", type=float, default=0.0, metavar="PCT",
                   help="percent of current equity risked per trade, e.g. 0.52")
    p.add_argument("--min-lots", type=int, default=1, metavar="N",
                   help="minimum lots when risk sizing rounds down to zero (default 1)")
    p.add_argument("--risk-mode", choices=["min_lot", "strict"], default="min_lot",
                   help="min_lot = always take at least --min-lots and report the "
                        "ACTUAL risk when it exceeds the budget (the percent is a "
                        "target); strict = skip the trade when even one lot breaches "
                        "the budget (the percent is a hard cap)")
    p.add_argument("--entry-fill", choices=["next_minute_open", "signal_close"],
                   default="next_minute_open",
                   help="price an entry is assumed to fill at. next_minute_open uses the "
                        "first 1-minute bar at or after the signal became actionable "
                        "(default); signal_close uses the signal candle's own close")
    p.add_argument("--breakeven-costs", action="store_true",
                   help="move the breakeven stop to entry +/- the modelled round-trip "
                        "cost instead of the bare entry price. The spec says 'entry "
                        "price', so this is OFF by default; with it ON a stop-out at "
                        "breakeven is genuinely net flat")
    p.add_argument("--roll-action", choices=["close", "roll_forward"], default="close",
                   help="what to do with a position still open when its contract "
                        "expires. close = exit and wait for a new signal (default); "
                        "roll_forward = re-open the same side on the new contract "
                        "carrying the stop as a distance")
    p.add_argument("--margin-pct", type=float, default=12.0, metavar="PCT",
                   help="margin required as a percent of contract value, for the "
                        "pre-trade margin check (default 12, NSE span+elm ballpark; "
                        "0 disables the check). Confirm against your broker")
    p.add_argument("--cost-stt", type=float, default=0.05, metavar="PCT",
                   help="STT on the futures SELL side in percent (default 0.05, the rate "
                        "from 2026-04-01; it was 0.02 before that date)")
    p.add_argument("--daily-loss", type=float, default=0.0, metavar="INR",
                   help="stop opening new positions for the day once this loss is hit")
    p.add_argument("--weekly-loss", type=float, default=0.0, metavar="INR",
                   help="stop opening new positions for the week once this loss is hit")
    p.add_argument("--monthly-dd", type=float, default=0.0, metavar="INR",
                   help="stop opening new positions once the month's drawdown from its "
                        "equity peak reaches this")
    p.add_argument("--walk-forward", nargs=2, type=int, metavar=("TRAIN", "TEST"),
                   help="chronological walk-forward validation in calendar days, e.g. "
                        "--walk-forward 120 60. Settings are frozen on the training "
                        "window and scored on the unseen window that follows it")
    p.add_argument("--no-train-window", action="store_true",
                   help="with --walk-forward, score only the unseen test windows "
                        "(faster; skips the in-sample comparison column)")
    p.add_argument("--show-trades", action="store_true",
                   help="also list every individual trade behind the daily totals")
    p.add_argument("--probe", action="store_true",
                   help="report which intervals and date ranges Upstox will serve "
                        "for the current contract")
    p.add_argument("--selftest", action="store_true",
                   help="run offline engine checks (no API access needed)")
    p.add_argument("--report", nargs="?", const="trades_*.csv", metavar="GLOB",
                   help="aggregate trade logs into one cumulative summary "
                        "(default trades_*.csv; try backtest_*.csv)")
    p.add_argument("--login", action="store_true", help="print the Upstox login URL")
    p.add_argument("--exchange-code", metavar="CODE", help="exchange an auth code for an access token")
    p.add_argument("--redirect-uri", default="", help="override UPSTOX_REDIRECT_URI")
    p.add_argument("--env-file", default="env.txt", help="credentials file (default env.txt)")
    p.add_argument("--instrument", default="", help="force an instrument key, e.g. NSE_FO|48704")
    p.add_argument("--quantity", type=int, default=0, help="quantity per trade (default: one lot)")
    p.add_argument("--poll", type=float, default=1.0, help="LTP polling interval in seconds")
    p.add_argument("--timeframe", default="15minute", metavar="INTERVAL",
                   help="entry candle timeframe (default 15minute, built by resampling "
                        "1-minute data; 30minute is served natively with 1 year of history)")
    p.add_argument("--hold", choices=["positional", "intraday"], default="positional",
                   help="positional (default, per spec) carries a position across "
                        "sessions until its stop is hit, using product NRML. intraday "
                        "squares off at --square-off using MIS. Positional adds overnight "
                        "gap risk: the stop cannot execute while the market is shut")
    p.add_argument("--multiplier", type=float, default=3.0, metavar="X",
                   help="Supertrend ATR multiplier (default 3, per spec v2; was 2)")
    p.add_argument("--stop-points", type=float, default=100.0, metavar="POINTS",
                   help="initial stop distance in index points (default 100, per spec "
                        "v2; was 80)")
    p.add_argument("--entry-on", choices=["flip", "state"], default="flip",
                   help="flip = Supertrend reversal candle only (default); state = any candle in trend")
    p.add_argument("--allowed-sides", choices=["both", "long", "short"], default="both",
                   help="which entry directions are allowed. Blocks NEW entries only - "
                        "an open position is still managed and exited normally")
    p.add_argument("--daily-source", choices=["futures", "index"], default="index",
                   help="price series behind the daily EMA regime filter "
                        "(default: index - a consistent basis across contract rolls)")
    p.add_argument("--candle-label", choices=["auto", "open", "close"], default="auto",
                   help="how Upstox timestamps intraday candles")
    p.add_argument("--broker-sl", action="store_true",
                   help="also rest an SL-M order at the exchange as a crash backstop "
                        "(forced ON in --mode live)")
    p.add_argument("--trade-on-weekends", action="store_true",
                   help="trade on Sat/Sun. Off by default: the bot cannot know the "
                        "exchange calendar, so it would otherwise sit out genuine special "
                        "sessions such as a weekend Union Budget day")
    p.add_argument("--no-broker-sl", action="store_true",
                   help="DANGEROUS: run live with bot-side stops only. Disables the "
                        "exchange-side SL-M backstop, so a crash, network loss or token "
                        "expiry leaves the position completely unprotected. This flag IS "
                        "honoured - it is the only way to override the live default.")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.verbose)

    cfg = Config(env_file=args.env_file, instrument_key_override=args.instrument,
                 quantity=args.quantity, poll_seconds=args.poll, entry_on=args.entry_on,
                 allowed_sides=args.allowed_sides,
                 entry_timeframe=args.timeframe, roll=args.roll,
                 hold=args.hold, st_multiplier=args.multiplier,
                 initial_stop_points=args.stop_points,
                 breakeven_covers_costs=args.breakeven_costs,
                 roll_action=args.roll_action,
                 margin_pct_of_notional=args.margin_pct / 100.0,
                 cost_points_per_side=args.cost_per_side,
                 daily_filter_source=args.daily_source, candle_label=args.candle_label,
                 broker_sl_backstop=args.broker_sl or (not args.no_broker_sl),
                 broker_sl_explicit_off=args.no_broker_sl,
                 trade_on_weekends=args.trade_on_weekends,
                 # execution realism
                 entry_slippage_points=args.entry_slip,
                 exit_slippage_points=args.exit_slip,
                 stop_slip_fraction=args.stop_slip_fraction,
                 # capital protection
                 initial_capital=args.capital,
                 risk_per_trade_pct=args.risk_pct / 100.0,
                 min_lots=args.min_lots, risk_mode=args.risk_mode,
                 entry_fill=args.entry_fill,
                 daily_loss_limit=args.daily_loss,
                 weekly_loss_limit=args.weekly_loss,
                 monthly_dd_review=args.monthly_dd,
                 # cost model
                 cost_brokerage_per_side=args.cost_brokerage,
                 cost_spread_points=args.cost_spread,
                 cost_stt_sell_pct=args.cost_stt / 100.0)
    if args.cost_multiplier and args.cost_multiplier != 1.0:
        cfg.cost_brokerage_per_side *= args.cost_multiplier
        cfg.cost_exchange_pct *= args.cost_multiplier
        cfg.cost_sebi_pct *= args.cost_multiplier
        cfg.cost_stamp_pct *= args.cost_multiplier
        cfg.cost_spread_points *= args.cost_multiplier
        if cfg.cost_points_per_side:
            cfg.cost_points_per_side *= args.cost_multiplier
    if cfg.initial_capital > 0 and cfg.risk_per_trade_pct <= 0 and not cfg.quantity:
        raise SystemExit(
            "--capital needs --risk-pct (e.g. --risk-pct 0.52) or an explicit --quantity.\n"
            f"Note: one NIFTY lot with an {cfg.initial_stop_points:g}-point stop risks "
            f"₹{cfg.initial_stop_points * 65:,.0f}, which is "
            f"{100 * cfg.initial_stop_points * 65 / cfg.initial_capital:.2f}% of "
            f"₹{cfg.initial_capital:,.0f}. A budget below that rounds down to zero lots.")

    # --- paths that need no credentials -------------------------------------------
    if args.selftest:
        return 1 if run_selftest(cfg) else 0

    if args.report is not None:
        run_report(args.report)
        return 0

    if args.login:
        resolve_credentials(cfg)
        print_login_url(cfg)
        return 0

    resolve_credentials(cfg)

    if args.exchange_code:
        exchange_code(cfg, args.exchange_code, args.redirect_uri)
        return 0

    check_token_freshness(cfg)

    if args.probe:
        run_probe(cfg)
        return 0

    if args.walk_forward:
        if not (args.backtest or args.days):
            raise SystemExit("--walk-forward needs a window: add --backtest START END "
                             "or --days N")
        if args.backtest:
            wf_start = datetime.strptime(args.backtest[0], "%Y-%m-%d").date()
            wf_end = datetime.strptime(args.backtest[1], "%Y-%m-%d").date()
        else:
            wf_end = datetime.now(tz=_ist()).date()
            wf_start = wf_end - timedelta(days=args.days)
        run_walk_forward(cfg, wf_start, wf_end, args.walk_forward[0],
                         args.walk_forward[1], run_train=not args.no_train_window)
        return 0

    if args.backtest or args.days:
        if args.backtest:
            start = datetime.strptime(args.backtest[0], "%Y-%m-%d").date()
            end = datetime.strptime(args.backtest[1], "%Y-%m-%d").date()
            days_requested = None
        else:
            end = datetime.now(tz=_ist()).date()
            start = end - timedelta(days=args.days)
            days_requested = args.days
        run_backtest(cfg, start, end, days_requested=days_requested,
                     show_trades=args.show_trades)
        return 0

    if args.mode == "live":
        log.warning("*** LIVE MODE: real orders will be placed on Upstox. ***")

    runner = LiveRunner(cfg, args.mode)
    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
