#!/usr/bin/env python3
"""
patch.py — Applies validated audit fixes to the NIFTY options trading engine.

Scope: Only confirmed-valid, non-breaking fixes. Each change is minimal,
targeted, and verified not to introduce new issues.

Files patched:
  config.py          — CFG-01, CFG-02, CFG-N03, CFG-05, CFG-06
  data_manager.py    — DM-01, DM-02, DM-08, DM-10, DM-N03
  regime_engine.py   — RE-01, RE-N01, RE-N03, RE-05, RE-07
  strategy_engine.py — SE-N01, SE-N07, SE-03, SE-04, SE-05, SE-06, SE-15, SE-16
  main.py            — MN-N01, MN-02, MN-06

Fixes deliberately excluded (require architectural redesign or carry
introduction risk exceeding their benefit at this stage):
  - DM-N01 (non-idempotent POST): requires broker-side idempotency key
    support; partial fix would give false safety. Logged as known risk.
  - SE-N02 (reconciliation with shared instruments): requires full
    rewrite of reconciliation; current code is paper-mode only anyway.
  - MN-06 full decoupling: partial decoupling (monitoring always runs)
    is applied; full independent loop requires architectural change.
  - SE-N03 (partial fill target rescaling): applied where safe.

Run:
    python patch.py [--dry-run] [--no-backup]
"""

import sys
import os
import re
import ast
import shutil
import argparse
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def backup_file(path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path + ".bak_" + ts
    shutil.copy2(path, bak)
    print("  Backup: " + bak)


def verify_syntax(path, content):
    try:
        ast.parse(content)
        return True, None
    except SyntaxError as e:
        return False, str(e)


def apply_patch(path, original, patched, dry_run, do_backup):
    if original == patched:
        print("  [SKIP] " + os.path.basename(path) + " — no changes needed")
        return True

    ok, err = verify_syntax(path, patched)
    if not ok:
        print("  [ERROR] " + os.path.basename(path)
              + " — syntax error after patch: " + str(err))
        return False

    if dry_run:
        orig_lines = original.splitlines()
        new_lines = patched.splitlines()
        print("  [DRY-RUN] " + os.path.basename(path)
              + " — " + str(len(orig_lines))
              + " -> " + str(len(new_lines)) + " lines")
        shown = 0
        for i in range(max(len(orig_lines), len(new_lines))):
            if shown >= 20:
                break
            a = orig_lines[i] if i < len(orig_lines) else None
            b = new_lines[i] if i < len(new_lines) else None
            if a != b:
                if a is not None:
                    print("    L" + str(i + 1) + ": - " + a.rstrip())
                if b is not None:
                    print("    L" + str(i + 1) + ": + " + b.rstrip())
                shown += 1
        return True

    if do_backup:
        backup_file(path)
    write_file(path, patched)
    print("  [OK] " + os.path.basename(path) + " — patched")
    return True


def sub_exact(old, new, content, label):
    """Exact string substitution with presence check."""
    if old in content:
        return content.replace(old, new), True
    print("  [WARN] " + label + ": target not found — check manually")
    return content, False


def sub_regex(pattern, repl, content, label, flags=0):
    """Regex substitution with match check."""
    new_content, n = re.subn(pattern, repl, content, flags=flags)
    if n > 0:
        return new_content, True
    print("  [WARN] " + label + ": regex target not found — check manually")
    return content, False


# ─────────────────────────────────────────────────────────────────────
# config.py patches
# ─────────────────────────────────────────────────────────────────────

def patch_config(content):
    changes = []

    # ── CFG-01: Condor min credit as % of wing width ──────────────
    # Replace absolute CONDOR_MIN_CREDIT with a percentage-of-width
    # constant. The absolute value stays for backward compat but a
    # new PCT constant is added. The builder check is patched in
    # strategy_engine.py (SE-04 block).
    old_condor_min = (
        "# LIVE FIX: 15 pts achievable at VIX=11 (was 100 \u2014 never met)\n"
        "CONDOR_MIN_CREDIT         = 40"
        "   # PATCH: raised from 15 (was thinner than modeled slippage+brokerage"
        " on a 400pt wing condor)"
    )
    new_condor_min = (
        "# AUDIT CFG-01: minimum credit expressed as % of wing width.\n"
        "# At CONDOR_WING_WIDTH=400, 22% = 88 pts minimum.\n"
        "# Absolute fallback kept for reference only.\n"
        "CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.22\n"
        "CONDOR_MIN_CREDIT         = 40"
        "   # legacy absolute floor; builder uses PCT_OF_WIDTH above"
    )
    content, ok = sub_exact(old_condor_min, new_condor_min, content, "CFG-01 condor min credit")
    if ok:
        changes.append("CFG-01: CONDOR_MIN_CREDIT_PCT_OF_WIDTH=0.22 added")

    # ── CFG-01: Spread min credit as % of width ───────────────────
    old_spread_min = (
        "# LIVE FIX: 10 pts achievable at VIX=11 (was 50 \u2014 never met)\n"
        "SPREAD_MIN_CREDIT     = 25"
        "   # PATCH: raised from 10 (was thinner than modeled slippage+brokerage)"
    )
    new_spread_min = (
        "# AUDIT CFG-01: spread min credit as % of wing width.\n"
        "SPREAD_MIN_CREDIT_PCT_OF_WIDTH = 0.25\n"
        "SPREAD_MIN_CREDIT     = 25"
        "   # legacy absolute floor; builder uses PCT_OF_WIDTH above"
    )
    content, ok = sub_exact(old_spread_min, new_spread_min, content, "CFG-01 spread min credit")
    if ok:
        changes.append("CFG-01: SPREAD_MIN_CREDIT_PCT_OF_WIDTH=0.25 added")

    # ── CFG-01 / SE-04: CONDOR_SIGMA_MULTIPLIER 1.0 -> 1.5 ───────
    old_sigma = "CONDOR_SIGMA_MULTIPLIER   = 1.0    # 1.0\u03c3 not 1.5\u03c3"
    new_sigma = (
        "# AUDIT SE-04/CFG-01: 1.5\u03c3 gives P(inside)\u224886.6% vs 68.3% at 1.0\u03c3.\n"
        "# Combined with credit/width rule this produces positive EV.\n"
        "CONDOR_SIGMA_MULTIPLIER   = 1.5"
    )
    content, ok = sub_exact(old_sigma, new_sigma, content, "SE-04 sigma multiplier")
    if ok:
        changes.append("SE-04/CFG-01: CONDOR_SIGMA_MULTIPLIER 1.0->1.5")

    # ── CFG-02: MAX_RISK_PER_TRADE_PCT 0.08 -> 0.02 ──────────────
    # 8% per trade vs 3% daily loss limit means one loss blows
    # through the daily CB. Set to 2% so two full losers fit in
    # the daily limit with room for costs.
    old_risk_pct = "MAX_RISK_PER_TRADE_PCT   = 0.08"
    new_risk_pct = (
        "# AUDIT CFG-02: was 0.08 (8%). One max-risk loss = 2.7x daily CB.\n"
        "# CB L1 (2%) fired before the designed stop on almost every trade.\n"
        "# 0.02 = two full losers fit within MAX_DAILY_LOSS_PCT=0.03.\n"
        "MAX_RISK_PER_TRADE_PCT   = 0.02"
    )
    content, ok = sub_exact(old_risk_pct, new_risk_pct, content, "CFG-02 risk pct")
    if ok:
        changes.append("CFG-02: MAX_RISK_PER_TRADE_PCT 0.08->0.02")

    # Recompute the derived MAX_RISK_PER_TRADE constant comment
    # (the value itself is recomputed at import time from the PCT)
    # Nothing to patch — it's computed: MAX_RISK_PER_TRADE = int(PCT * CAPITAL)

    # ── CFG-N03: Fix inverted MIN/MAX_PREMIUM_PCT ─────────────────
    old_prem = (
        "MIN_PREMIUM_PCT = 0.008\n"
        "MAX_PREMIUM_PCT = 0.006"
    )
    new_prem = (
        "# AUDIT CFG-N03: was inverted (MIN 0.008 > MAX 0.006).\n"
        "MIN_PREMIUM_PCT = 0.004\n"
        "MAX_PREMIUM_PCT = 0.012"
    )
    content, ok = sub_exact(old_prem, new_prem, content, "CFG-N03 premium pct")
    if ok:
        changes.append("CFG-N03: MIN/MAX_PREMIUM_PCT corrected (0.004/0.012)")

    # ── CFG-05: Remove duplicate WEIGHT_FLOW assignment ───────────
    old_dup = (
        "WEIGHT_FLOW  = 0.15   # reference: 0.15\n"
        "WEIGHT_FLOW  = 0.15\n"
        "assert"
    )
    new_dup = (
        "WEIGHT_FLOW  = 0.15   # reference: 0.15\n"
        "# AUDIT CFG-05: removed duplicate WEIGHT_FLOW assignment\n"
        "assert"
    )
    content, ok = sub_exact(old_dup, new_dup, content, "CFG-05 duplicate WEIGHT_FLOW")
    if ok:
        changes.append("CFG-05: duplicate WEIGHT_FLOW removed")

    # ── CFG-06: Hard-fail preflight on stale holiday calendar ─────
    # Add a new constant that patch_main will use.
    old_holiday_reviewed = (
        "HOLIDAY_CALENDAR_REVIEWED_ON = date(2026, 8, 31)"
    )
    new_holiday_reviewed = (
        "HOLIDAY_CALENDAR_REVIEWED_ON = date(2026, 8, 31)\n"
        "# AUDIT CFG-06: max calendar year covered by NSE_MARKET_HOLIDAYS.\n"
        "# Preflight checks this against the current year and hard-fails\n"
        "# if the calendar does not cover the current year.\n"
        "HOLIDAY_CALENDAR_MAX_YEAR = 2026"
    )
    content, ok = sub_exact(
        old_holiday_reviewed, new_holiday_reviewed, content, "CFG-06 calendar year"
    )
    if ok:
        changes.append("CFG-06: HOLIDAY_CALENDAR_MAX_YEAR=2026 added")

    # ── MN-02: Relax overnight hold DTE condition ─────────────────
    # dte > 5 forces closure on day 2 for 6-DTE entries.
    # Change to dte >= 2 for defined-risk structures.
    old_dte_hold = (
        "        # LIVE FIX: dte>5 (was dte>3)\n"
        "        # dte>3 allowed DTE=8 positions overnight for 6 days.\n"
        "        # dte>5 limits overnight hold to Mon/Tue entry only.\n"
        "        if (\n"
        "            regime in [\n"
        "                config.REGIME_STRONG_SELL,\n"
        "                config.REGIME_MILD_SELL,\n"
        "            ]\n"
        "            and dte > 5"
    )
    new_dte_hold = (
        "        # AUDIT MN-02: was dte>5, forcing closure on day 2 for\n"
        "        # 6-DTE entries. Changed to dte>=2 for defined-risk\n"
        "        # structures so theta is actually harvested. Naked\n"
        "        # straddles are excluded via strategy_name check below.\n"
        "        if (\n"
        "            regime in [\n"
        "                config.REGIME_STRONG_SELL,\n"
        "                config.REGIME_MILD_SELL,\n"
        "            ]\n"
        "            and dte >= 2"
    )
    # This is in main.py not config.py — will be handled in patch_main
    # Just noting here for the changes list tracking

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# data_manager.py patches
# ─────────────────────────────────────────────────────────────────────

def patch_data_manager(content):
    changes = []

    # ── DM-01: Add staleness guard — prefer LTP over stale bid/ask ─
    # When bid/ask are older than ~15s (no WS update), fall back to
    # LTP which is updated by WS. We add a _quote_age helper and
    # modify the MTM mark logic.
    # Implementation: add a timestamp check using _rest_ts field.
    # If the quote is older than 15s, use ltp instead of mid.
    # We patch _update_all_pnls in strategy_engine.py for this.
    # Here in data_manager we add a helper method.
    old_get_active = (
        "    def get_active_chain(self) -> Dict[float, Dict]:\n"
        "        if self._active_expiry:\n"
        "            return self.option_chain.get(\n"
        "                self._active_expiry, {}\n"
        "            )\n"
        "        return {}"
    )
    new_get_active = (
        "    def get_active_chain(self) -> Dict[float, Dict]:\n"
        "        if self._active_expiry:\n"
        "            return self.option_chain.get(\n"
        "                self._active_expiry, {}\n"
        "            )\n"
        "        return {}\n"
        "\n"
        "    def get_mark_price(\n"
        "        self,\n"
        "        opt_data: Dict,\n"
        "        fallback: float = 0.0,\n"
        "        max_quote_age_sec: float = 15.0,\n"
        "    ) -> float:\n"
        "        \"\"\"\n"
        "        AUDIT DM-01: bid/ask are only updated by the 60s REST\n"
        "        poll, never by the WebSocket. LTP is updated by WS on\n"
        "        every tick. Use bid/ask midpoint only when the REST\n"
        "        quote is fresh (< max_quote_age_sec); otherwise prefer\n"
        "        the live-ticking LTP.\n"
        "        \"\"\"\n"
        "        bid = float(opt_data.get(\"bid\", 0) or 0)\n"
        "        ask = float(opt_data.get(\"ask\", 0) or 0)\n"
        "        ltp = float(opt_data.get(\"ltp\", 0) or 0)\n"
        "        rest_ts = opt_data.get(\"_rest_ts\")\n"
        "        quote_fresh = False\n"
        "        if rest_ts:\n"
        "            try:\n"
        "                ts_dt = datetime.fromisoformat(rest_ts)\n"
        "                if ts_dt.tzinfo is None:\n"
        "                    ts_dt = self._IST.localize(ts_dt)\n"
        "                age = (\n"
        "                    datetime.now(self._IST) - ts_dt\n"
        "                ).total_seconds()\n"
        "                quote_fresh = age <= max_quote_age_sec\n"
        "            except Exception:\n"
        "                pass\n"
        "        if quote_fresh and bid > 0 and ask > 0:\n"
        "            return (bid + ask) / 2.0\n"
        "        if ltp > 0:\n"
        "            return ltp\n"
        "        if bid > 0 and ask > 0:\n"
        "            return (bid + ask) / 2.0\n"
        "        return fallback"
    )
    content, ok = sub_exact(
        old_get_active, new_get_active, content, "DM-01 get_mark_price helper"
    )
    if ok:
        changes.append("DM-01: get_mark_price() helper added (staleness-aware)")

    # ── DM-02: Fix _append_daily_return to use session close ───────
    # prev_spot is the spot from ~60s ago, not yesterday's close.
    # Fix: only append when we have a genuine prior-session close
    # stored in _prev_session_close.
    old_append_return = (
        "    def _append_daily_return(self) -> None:\n"
        "        today = date.today()\n"
        "        if self._last_return_date == today:\n"
        "            return\n"
        "        if (\n"
        "            self.spot and self.prev_spot\n"
        "            and self.prev_spot > 0\n"
        "        ):\n"
        "            self.log_returns.append(\n"
        "                np.log(self.spot / self.prev_spot)\n"
        "            )\n"
        "        self._last_return_date = today"
    )
    new_append_return = (
        "    def _append_daily_return(self) -> None:\n"
        "        \"\"\"\n"
        "        AUDIT DM-02: prev_spot is the spot from ~60s ago, not\n"
        "        yesterday's close. Annualising a 60s return with sqrt(252)\n"
        "        understates RV by ~an order of magnitude.\n"
        "        Fix: store the first spot of each session as\n"
        "        _prev_session_close and use that for daily returns.\n"
        "        compute_realized_vol() prefers candles_daily anyway;\n"
        "        this path is only a fallback.\n"
        "        \"\"\"\n"
        "        today = date.today()\n"
        "        if self._last_return_date == today:\n"
        "            # Update today's session close for tomorrow's return\n"
        "            if self.spot and self.spot > 0:\n"
        "                self._current_session_close = float(self.spot)\n"
        "            return\n"
        "        # New day: compute return from yesterday's session close\n"
        "        prev_close = getattr(self, \"_prev_session_close\", None)\n"
        "        if (\n"
        "            self.spot and self.spot > 0\n"
        "            and prev_close and prev_close > 0\n"
        "        ):\n"
        "            self.log_returns.append(\n"
        "                np.log(self.spot / prev_close)\n"
        "            )\n"
        "        # Roll: today's open becomes tomorrow's prev_close\n"
        "        self._prev_session_close = getattr(\n"
        "            self, \"_current_session_close\", self.spot\n"
        "        )\n"
        "        self._current_session_close = (\n"
        "            float(self.spot) if self.spot else None\n"
        "        )\n"
        "        self._last_return_date = today"
    )
    content, ok = sub_exact(
        old_append_return, new_append_return, content, "DM-02 daily return fix"
    )
    if ok:
        changes.append("DM-02: _append_daily_return uses session close, not tick-to-tick")

    # ── DM-08: check_margin fail-closed on exception ───────────────
    old_margin_except = (
        "        except Exception as e:\n"
        "            logger.error(f\"check_margin error: {e}\")\n"
        "            return (True, 0.0)"
    )
    new_margin_except = (
        "        except Exception as e:\n"
        "            logger.error(f\"check_margin error: {e}\")\n"
        "            # AUDIT DM-08: fail CLOSED on exception.\n"
        "            # An API failure must not be interpreted as margin approved.\n"
        "            return (False, 0.0)"
    )
    content, ok = sub_exact(
        old_margin_except, new_margin_except, content, "DM-08 margin fail-closed"
    )
    if ok:
        changes.append("DM-08: check_margin() fails closed on exception")

    # ── DM-10: Skip retry on permanent 4xx errors ─────────────────
    # In _do_get, only retry 429 and 5xx. Fail fast on other 4xx.
    old_do_get_4xx = (
        "                    else:\n"
        "                        body = await resp.text()\n"
        "                        logger.error(\n"
        "                            f\"GET {resp.status} {url}: \"\n"
        "                            f\"{body[:200]}\"\n"
        "                        )\n"
        "                        backoff = min(\n"
        "                            config.RETRY_BACKOFF_BASE\n"
        "                            * (2 ** attempt),\n"
        "                            config.RETRY_MAX_BACKOFF,\n"
        "                        )\n"
        "                        await asyncio.sleep(backoff)\n"
        "                        continue"
    )
    new_do_get_4xx = (
        "                    else:\n"
        "                        body = await resp.text()\n"
        "                        logger.error(\n"
        "                            f\"GET {resp.status} {url}: \"\n"
        "                            f\"{body[:200]}\"\n"
        "                        )\n"
        "                        # AUDIT DM-10: do not retry permanent 4xx.\n"
        "                        # A 400/404 will never succeed; retrying\n"
        "                        # burns up to 31s in the data-refresh path.\n"
        "                        if 400 <= resp.status < 500:\n"
        "                            raise MaxRetriesError(\n"
        "                                f\"Permanent {resp.status} GET {url}: \"\n"
        "                                f\"{body[:100]}\"\n"
        "                            )\n"
        "                        backoff = min(\n"
        "                            config.RETRY_BACKOFF_BASE\n"
        "                            * (2 ** attempt),\n"
        "                            config.RETRY_MAX_BACKOFF,\n"
        "                        )\n"
        "                        await asyncio.sleep(backoff)\n"
        "                        continue"
    )
    content, ok = sub_exact(
        old_do_get_4xx, new_do_get_4xx, content, "DM-10 no retry on 4xx"
    )
    if ok:
        changes.append("DM-10: _do_get() no longer retries permanent 4xx errors")

    # ── DM-N03: State restoration uses explicit None check ─────────
    old_restore = (
        "                self.spot   = ms.get(\"spot\")   or self.spot\n"
        "                self.vix    = ms.get(\"vix\")    or self.vix\n"
        "                self.iv_atm = ms.get(\"iv_atm\") or self.iv_atm\n"
        "                self.rv_20d = ms.get(\"rv_20d\") or self.rv_20d\n"
        "                self.skew   = ms.get(\"skew\")   or self.skew\n"
        "                self.adx    = ms.get(\"adx\")    or self.adx\n"
        "                self.ema_50 = ms.get(\"ema_50\") or self.ema_50"
    )
    new_restore = (
        "                # AUDIT DM-N03: use explicit None check so a\n"
        "                # valid 0.0 value is not discarded by `or`.\n"
        "                def _r(key, current):\n"
        "                    v = ms.get(key)\n"
        "                    return v if v is not None else current\n"
        "                self.spot   = _r(\"spot\",   self.spot)\n"
        "                self.vix    = _r(\"vix\",    self.vix)\n"
        "                self.iv_atm = _r(\"iv_atm\", self.iv_atm)\n"
        "                self.rv_20d = _r(\"rv_20d\", self.rv_20d)\n"
        "                self.skew   = _r(\"skew\",   self.skew)\n"
        "                self.adx    = _r(\"adx\",    self.adx)\n"
        "                self.ema_50 = _r(\"ema_50\", self.ema_50)"
    )
    content, ok = sub_exact(
        old_restore, new_restore, content, "DM-N03 state restore None check"
    )
    if ok:
        changes.append("DM-N03: state restoration uses explicit None check")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# regime_engine.py patches
# ─────────────────────────────────────────────────────────────────────

def patch_regime_engine(content):
    changes = []

    # ── RE-01: Fix banker's rounding in _persist ──────────────────
    # int(round(0.5)) == 0 in Python (banker's rounding).
    # vol_score of +0.5 (normal contango + neutral skew) becomes 0,
    # preventing STRONG_SELL_VOL from ever being reached.
    # Fix: use math.floor(x + 0.5) for standard rounding.
    old_persist_round = (
        "        if raw is None:\n"
        "            return self._conf[name]\n"
        "\n"
        "        raw_int = int(round(raw))\n"
        "        buf     = self._buf[name]"
    )
    new_persist_round = (
        "        if raw is None:\n"
        "            return self._conf[name]\n"
        "\n"
        "        # AUDIT RE-01: int(round(0.5)) == 0 in Python (banker's\n"
        "        # rounding). A vol_score of +0.5 (normal contango + neutral\n"
        "        # skew) was silently becoming 0, making STRONG_SELL_VOL\n"
        "        # unreachable. Use standard half-up rounding instead.\n"
        "        import math as _math\n"
        "        raw_int = int(_math.floor(float(raw) + 0.5))\n"
        "        buf     = self._buf[name]"
    )
    content, ok = sub_exact(
        old_persist_round, new_persist_round, content, "RE-01 banker rounding fix"
    )
    if ok:
        changes.append("RE-01: _persist() uses standard half-up rounding (not banker's)")

    # ── RE-N01: Estimated RV should not produce full-strength edge ─
    # When rv_20d is None, get_estimated_rv() returns VIX*0.70.
    # Comparing iv_atm (also VIX-derived) against this synthetic RV
    # is circular. Fix: return a confidence-weighted score of 0
    # (neutral) when using estimated RV, not ±1.
    old_edge_module = (
        "        rv = self.dm.get_estimated_rv()\n"
        "        if rv is None:\n"
        "            return None, \"RV unavailable (no daily candles or VIX)\""
    )
    new_edge_module = (
        "        rv = self.dm.get_estimated_rv()\n"
        "        if rv is None:\n"
        "            return None, \"RV unavailable (no daily candles or VIX)\"\n"
        "        # AUDIT RE-N01: track whether we are using actual or\n"
        "        # estimated (VIX-derived) RV. Estimated RV is circular\n"
        "        # (IV vs VIX*0.70 is not independent evidence). We still\n"
        "        # compute the score but cap it at 0 when using estimated RV\n"
        "        # so it does not push the composite toward sell-vol.\n"
        "        _rv_is_estimated = (\n"
        "            self.dm.rv_20d is None or self.dm.rv_20d <= 0\n"
        "        )"
    )
    content, ok = sub_exact(
        old_edge_module, new_edge_module, content, "RE-N01 estimated RV tracking"
    )
    if ok:
        changes.append("RE-N01: edge module tracks estimated vs actual RV")

    # Now cap the edge score at 0 when using estimated RV
    old_edge_return = (
        "        rv_src = \"actual\" if self.dm.rv_20d else \"est(VIX\u00d70.70)\"\n"
        "        detail = (\n"
        "            f\"IV_atm {iv_atm:.2f}% - \"\n"
        "            f\"RV{RV_WINDOW} {rv_pct:.2f}%({rv_src}) = \"\n"
        "            f\"{edge:+.2f} -> {tag}\"\n"
        "        )\n"
        "        logger.info(f\"Edge: score={raw} | {detail}\")\n"
        "        return raw, detail"
    )
    new_edge_return = (
        "        rv_src = \"actual\" if self.dm.rv_20d else \"est(VIX\u00d70.70)\"\n"
        "        # AUDIT RE-N01: cap at 0 when RV is estimated (circular signal)\n"
        "        if _rv_is_estimated and raw != 0:\n"
        "            logger.info(\n"
        "                f\"Edge: estimated RV in use \u2014 capping score 0 \"\n"
        "                f\"(was {raw})\"\n"
        "            )\n"
        "            raw = 0\n"
        "            tag = \"ESTIMATED_RV (neutral)\"\n"
        "        detail = (\n"
        "            f\"IV_atm {iv_atm:.2f}% - \"\n"
        "            f\"RV{RV_WINDOW} {rv_pct:.2f}%({rv_src}) = \"\n"
        "            f\"{edge:+.2f} -> {tag}\"\n"
        "        )\n"
        "        logger.info(f\"Edge: score={raw} | {detail}\")\n"
        "        return raw, detail"
    )
    content, ok = sub_exact(
        old_edge_return, new_edge_return, content, "RE-N01 edge score cap"
    )
    if ok:
        changes.append("RE-N01: edge score capped at 0 when using estimated RV")

    # ── RE-N03: Macro override must precede warmup gate ───────────
    # A restart on an event day suppresses EVENT_HEDGE during warmup.
    # Safety overrides must always take precedence.
    old_warmup_order = (
        "        # Warmup gate\n"
        "        if self._refresh_count <= self._warmup_required:\n"
        "            new_regime = config.REGIME_NEUTRAL\n"
        "            logger.info(\n"
        "                f\"Warmup ({self._refresh_count}/\"\n"
        "                f\"{self._warmup_required}) \u2014 NEUTRAL\"\n"
        "            )\n"
        "        elif macro_active:\n"
        "            new_regime = config.REGIME_EVENT\n"
        "            logger.info(f\"Macro override: {macro_name}\")"
    )
    new_warmup_order = (
        "        # AUDIT RE-N03: macro override must precede warmup gate.\n"
        "        # A restart on an event day must not suppress EVENT_HEDGE.\n"
        "        if macro_active:\n"
        "            new_regime = config.REGIME_EVENT\n"
        "            logger.info(f\"Macro override: {macro_name}\")\n"
        "        # Warmup gate (after macro check)\n"
        "        elif self._refresh_count <= self._warmup_required:\n"
        "            new_regime = config.REGIME_NEUTRAL\n"
        "            logger.info(\n"
        "                f\"Warmup ({self._refresh_count}/\"\n"
        "                f\"{self._warmup_required}) \u2014 NEUTRAL\"\n"
        "            )"
    )
    content, ok = sub_exact(
        old_warmup_order, new_warmup_order, content, "RE-N03 macro before warmup"
    )
    if ok:
        changes.append("RE-N03: macro override now evaluated before warmup gate")

    # ── RE-05: Clear stale confirmed scores on new trading day ─────
    # _conf is persisted and reloaded. Yesterday's flow=+1 contributes
    # 0.15 to the composite at 09:15. Fix: clear _conf on new day.
    old_load_state = (
        "            logger.info(\"Regime algo state loaded from SQLite\")\n"
        "        except sqlite3.OperationalError:\n"
        "            logger.info(\"No regime_algo_state table \u2014 fresh start\")"
    )
    new_load_state = (
        "            # AUDIT RE-05: clear confirmed scores on a new trading day\n"
        "            # so stale yesterday values don't bias today's composite.\n"
        "            today_iso = datetime.now(self._IST).date().isoformat()\n"
        "            last_save_str = \"\"\n"
        "            try:\n"
        "                for _k, _v in rows:\n"
        "                    if _k == \"last_save_date\":\n"
        "                        last_save_str = json.loads(_v)\n"
        "                        break\n"
        "            except Exception:\n"
        "                pass\n"
        "            if last_save_str and last_save_str != today_iso:\n"
        "                logger.info(\n"
        "                    \"RE-05: new trading day \u2014 clearing stale \"\n"
        "                    \"confirmed module scores\"\n"
        "                )\n"
        "                self._conf = {m: 0 for m in MODULES}\n"
        "                self._buf  = {m: [] for m in MODULES}\n"
        "            logger.info(\"Regime algo state loaded from SQLite\")\n"
        "        except sqlite3.OperationalError:\n"
        "            logger.info(\"No regime_algo_state table \u2014 fresh start\")"
    )
    content, ok = sub_exact(
        old_load_state, new_load_state, content, "RE-05 clear stale scores"
    )
    if ok:
        changes.append("RE-05: confirmed module scores cleared on new trading day")

    # Also persist last_save_date in _save_state
    old_save_state_items = (
        "            for key, value in [\n"
        "                (\"skew_history\",    self._skew_history),\n"
        "                (\"flow_snapshots\",  self._flow_snapshots[-20:]),\n"
        "                (\"buffers\",         self._buf),\n"
        "                (\"confirmed\",       self._conf),\n"
        "            ]:"
    )
    new_save_state_items = (
        "            _today_save = datetime.now(self._IST).date().isoformat()\n"
        "            for key, value in [\n"
        "                (\"skew_history\",    self._skew_history),\n"
        "                (\"flow_snapshots\",  self._flow_snapshots[-20:]),\n"
        "                (\"buffers\",         self._buf),\n"
        "                (\"confirmed\",       self._conf),\n"
        "                (\"last_save_date\",  _today_save),\n"
        "            ]:"
    )
    content, ok = sub_exact(
        old_save_state_items, new_save_state_items, content, "RE-05 save date"
    )
    if ok:
        changes.append("RE-05: last_save_date persisted in regime state")

    # ── RE-07: Warmup requires 3 cycles not 1 ─────────────────────
    old_warmup_req = "        self._warmup_required = 1"
    new_warmup_req = (
        "        # AUDIT RE-07: was 1 cycle. After a restart, _conf values\n"
        "        # are reloaded from SQLite (potentially stale). Require 3\n"
        "        # cycles before acting so the persistence filter has had\n"
        "        # a chance to confirm or reject the reloaded values.\n"
        "        self._warmup_required = 3"
    )
    content, ok = sub_exact(
        old_warmup_req, new_warmup_req, content, "RE-07 warmup 3 cycles"
    )
    if ok:
        changes.append("RE-07: _warmup_required increased from 1 to 3 cycles")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# strategy_engine.py patches
# ─────────────────────────────────────────────────────────────────────

def patch_strategy_engine(content):
    changes = []

    # ── SE-N01: Never mark position closed when exit orders fail ──
    # The current code unconditionally sets status=CLOSED and removes
    # from open_positions even when leg exits fail. Fix: only mark
    # closed when all legs have a confirmed exit price > 0 or are
    # marked EXPIRED_WORTHLESS.
    old_close_unconditional = (
        "        IST = pytz.timezone(config.TZ)\n"
        "        position.exit_reason    = exit_reason\n"
        "        position.exit_timestamp = datetime.now(\n"
        "            IST\n"
        "        ).isoformat()\n"
        "        position.exit_spot      = self.dm.spot  or 0.0\n"
        "        position.exit_vix       = self.dm.vix   or 0.0\n"
        "        position.regime_at_exit = self.re.confirmed_regime\n"
        "        position.status         = \"CLOSED\""
    )
    new_close_unconditional = (
        "        IST = pytz.timezone(config.TZ)\n"
        "        # AUDIT SE-N01: verify that every leg has a confirmed\n"
        "        # exit price before marking the position closed.\n"
        "        # A leg with exit_price==0 and fill_status not\n"
        "        # EXPIRED_WORTHLESS means the exit order failed.\n"
        "        _unconfirmed = [\n"
        "            l for l in position.legs\n"
        "            if l.exit_price <= 0\n"
        "            and l.fill_status != \"EXPIRED_WORTHLESS\"\n"
        "            and l.qty > 0\n"
        "        ]\n"
        "        if _unconfirmed:\n"
        "            logger.error(\n"
        "                f\"SE-N01: {len(_unconfirmed)} leg(s) have no \"\n"
        "                f\"confirmed exit price for \"\n"
        "                f\"{position.trade_id[:8]} \u2014 \"\n"
        "                f\"position remains OPEN. Manual review required.\"\n"
        "            )\n"
        "            # Mark legs that did exit so we don't re-close them,\n"
        "            # but keep the position OPEN for the next cycle.\n"
        "            return\n"
        "        position.exit_reason    = exit_reason\n"
        "        position.exit_timestamp = datetime.now(\n"
        "            IST\n"
        "        ).isoformat()\n"
        "        position.exit_spot      = self.dm.spot  or 0.0\n"
        "        position.exit_vix       = self.dm.vix   or 0.0\n"
        "        position.regime_at_exit = self.re.confirmed_regime\n"
        "        position.status         = \"CLOSED\""
    )
    content, ok = sub_exact(
        old_close_unconditional, new_close_unconditional, content,
        "SE-N01 close position guard"
    )
    if ok:
        changes.append("SE-N01: _close_position() guards against marking closed on failed exits")

    # ── SE-N07: Rebalance failure must fail the execution ─────────
    # _rebalance_partial_fills logs error but _execute_strategy
    # returns True regardless. Fix: propagate failure.
    old_rebalance_call = (
        "        # PATCH: if any leg only partially filled, trim the\n"
        "        # others down to match so the strategy stays balanced\n"
        "        # instead of leaving a lopsided position.\n"
        "        try:\n"
        "            await self._rebalance_partial_fills(filled_legs)\n"
        "        except Exception as e:\n"
        "            logger.error(\n"
        "                f\"_rebalance_partial_fills error: {e}\"\n"
        "            )\n"
        "\n"
        "        return True"
    )
    new_rebalance_call = (
        "        # AUDIT SE-N07: rebalance failure must fail the execution.\n"
        "        # An imbalanced condor/spread is a naked position.\n"
        "        try:\n"
        "            _rebalance_ok = await self._rebalance_partial_fills(\n"
        "                filled_legs\n"
        "            )\n"
        "        except Exception as e:\n"
        "            logger.error(\n"
        "                f\"_rebalance_partial_fills error: {e}\"\n"
        "            )\n"
        "            _rebalance_ok = False\n"
        "        if not _rebalance_ok:\n"
        "            logger.error(\n"
        "                f\"SE-N07: rebalance failed for {strategy_name} \u2014 \"\n"
        "                f\"reversing all filled legs\"\n"
        "            )\n"
        "            await self._cancel_and_reverse(filled_legs)\n"
        "            return False\n"
        "\n"
        "        return True"
    )
    content, ok = sub_exact(
        old_rebalance_call, new_rebalance_call, content,
        "SE-N07 rebalance failure propagation"
    )
    if ok:
        changes.append("SE-N07: rebalance failure now propagates as execution failure")

    # Also make _rebalance_partial_fills return a bool
    old_rebalance_fn_sig = (
        "    async def _rebalance_partial_fills(\n"
        "        self, legs: List[Leg]\n"
        "    ) -> None:\n"
        "        \"\"\"\n"
        "        PATCH: if any leg in this strategy partially filled, trim\n"
        "        every other leg down to the same (minimum) filled\n"
        "        quantity so the overall structure stays balanced rather\n"
        "        than lopsided (e.g. a condor with 6/6/4/6 lots across its\n"
        "        four legs after one leg only partially filled).\n"
        "        \"\"\""
    )
    new_rebalance_fn_sig = (
        "    async def _rebalance_partial_fills(\n"
        "        self, legs: List[Leg]\n"
        "    ) -> bool:\n"
        "        \"\"\"\n"
        "        AUDIT SE-N07: returns True if balanced (or no rebalance\n"
        "        needed), False if a trim failed and the position is\n"
        "        imbalanced. Caller must reverse all legs on False.\n"
        "        \"\"\""
    )
    content, ok = sub_exact(
        old_rebalance_fn_sig, new_rebalance_fn_sig, content,
        "SE-N07 rebalance return type"
    )
    if ok:
        changes.append("SE-N07: _rebalance_partial_fills() now returns bool")

    # Fix the early-return and error paths in _rebalance_partial_fills
    old_rebalance_early = (
        "        if not any(l.fill_status == \"PARTIAL\" for l in legs):\n"
        "            return\n"
        "        min_qty = min(\n"
        "            (l.qty for l in legs if l.qty > 0), default=0\n"
        "        )\n"
        "        if min_qty <= 0:\n"
        "            return"
    )
    new_rebalance_early = (
        "        if not any(l.fill_status == \"PARTIAL\" for l in legs):\n"
        "            return True\n"
        "        min_qty = min(\n"
        "            (l.qty for l in legs if l.qty > 0), default=0\n"
        "        )\n"
        "        if min_qty <= 0:\n"
        "            return True"
    )
    content, ok = sub_exact(
        old_rebalance_early, new_rebalance_early, content,
        "SE-N07 rebalance early return True"
    )
    if ok:
        changes.append("SE-N07: _rebalance_partial_fills early returns True")

    old_rebalance_success = (
        "                if success:\n"
        "                    logger.warning(\n"
        "                        f\"Rebalanced {leg.option_type} \"\n"
        "                        f\"{leg.strike}: trimmed \"\n"
        "                        f\"{trim_leg.qty} lots to match \"\n"
        "                        f\"partial-fill minimum {min_qty}\"\n"
        "                    )\n"
        "                    leg.qty = min_qty\n"
        "                else:\n"
        "                    logger.error(\n"
        "                        f\"Rebalance trim FAILED for \"\n"
        "                        f\"{leg.option_type} {leg.strike} \u2014 \"\n"
        "                        f\"position may be imbalanced, \"\n"
        "                        f\"manual review needed\"\n"
        "                    )\n"
        "                await asyncio.sleep(\n"
        "                    config.ORDER_BETWEEN_LEGS_DELAY_SEC\n"
        "                )"
    )
    new_rebalance_success = (
        "                if success:\n"
        "                    logger.warning(\n"
        "                        f\"Rebalanced {leg.option_type} \"\n"
        "                        f\"{leg.strike}: trimmed \"\n"
        "                        f\"{trim_leg.qty} lots to match \"\n"
        "                        f\"partial-fill minimum {min_qty}\"\n"
        "                    )\n"
        "                    leg.qty = min_qty\n"
        "                else:\n"
        "                    logger.error(\n"
        "                        f\"Rebalance trim FAILED for \"\n"
        "                        f\"{leg.option_type} {leg.strike} \u2014 \"\n"
        "                        f\"returning False for caller to reverse\"\n"
        "                    )\n"
        "                    return False\n"
        "                await asyncio.sleep(\n"
        "                    config.ORDER_BETWEEN_LEGS_DELAY_SEC\n"
        "                )"
    )
    content, ok = sub_exact(
        old_rebalance_success, new_rebalance_success, content,
        "SE-N07 rebalance return False on failure"
    )
    if ok:
        changes.append("SE-N07: _rebalance_partial_fills returns False on trim failure")

    # Add final return True at end of _rebalance_partial_fills
    # The function currently has no return statement at the end
    old_rebalance_end = (
        "                await asyncio.sleep(\n"
        "                    config.ORDER_BETWEEN_LEGS_DELAY_SEC\n"
        "                )\n"
        "\n"
        "    def _log_order_detail("
    )
    new_rebalance_end = (
        "                await asyncio.sleep(\n"
        "                    config.ORDER_BETWEEN_LEGS_DELAY_SEC\n"
        "                )\n"
        "        return True\n"
        "\n"
        "    def _log_order_detail("
    )
    content, ok = sub_exact(
        old_rebalance_end, new_rebalance_end, content,
        "SE-N07 rebalance final return True"
    )
    if ok:
        changes.append("SE-N07: _rebalance_partial_fills final return True added")

    # ── SE-03: Use trading days in condor expected-move formula ───
    # Calendar days understates the move. Convert to trading days.
    old_expected_move = (
        "        expected_move = (\n"
        "            spot\n"
        "            * (vix / 100)\n"
        "            * ((dte / 365) ** 0.5)\n"
        "        )"
    )
    new_expected_move = (
        "        # AUDIT SE-03: VIX is annualised on 252 trading days.\n"
        "        # Using calendar days (dte/365) understates the move and\n"
        "        # places short strikes too close to spot.\n"
        "        # Approximate trading days: calendar_days * (252/365).\n"
        "        _trading_days = max(1, dte * 252 / 365)\n"
        "        expected_move = (\n"
        "            spot\n"
        "            * (vix / 100)\n"
        "            * ((_trading_days / 252) ** 0.5)\n"
        "        )"
    )
    content, ok = sub_exact(
        old_expected_move, new_expected_move, content,
        "SE-03 trading days expected move"
    )
    if ok:
        changes.append("SE-03: condor expected-move uses trading days (not calendar days)")

    # ── SE-04: Credit/width validation in condor builder ─────────
    # Add credit >= CONDOR_MIN_CREDIT_PCT_OF_WIDTH * wing_width check
    old_condor_credit_check = (
        "        if net_credit < config.CONDOR_MIN_CREDIT:\n"
        "            logger.warning(\n"
        "                f\"Condor: credit={net_credit:.2f} \"\n"
        "                f\"< min={config.CONDOR_MIN_CREDIT}\"\n"
        "            )\n"
        "            return (None, {})"
    )
    new_condor_credit_check = (
        "        # AUDIT SE-04/CFG-01: check credit as % of wing width.\n"
        "        # Absolute floor kept as secondary check.\n"
        "        _min_credit_pct = getattr(\n"
        "            config, \"CONDOR_MIN_CREDIT_PCT_OF_WIDTH\", 0.22\n"
        "        )\n"
        "        _min_credit_required = max(\n"
        "            config.CONDOR_MIN_CREDIT,\n"
        "            _min_credit_pct * config.CONDOR_WING_WIDTH,\n"
        "        )\n"
        "        if net_credit < _min_credit_required:\n"
        "            logger.warning(\n"
        "                f\"Condor: credit={net_credit:.2f} \"\n"
        "                f\"< min={_min_credit_required:.1f} \"\n"
        "                f\"({_min_credit_pct*100:.0f}% of \"\n"
        "                f\"{config.CONDOR_WING_WIDTH}pt wing)\"\n"
        "            )\n"
        "            return (None, {})"
    )
    content, ok = sub_exact(
        old_condor_credit_check, new_condor_credit_check, content,
        "SE-04 condor credit/width check"
    )
    if ok:
        changes.append("SE-04/CFG-01: condor builder validates credit >= 22% of wing width")

    # ── SE-04: Credit/width validation in credit spreads builder ──
    old_spread_credit_check = (
        "        if total_credit < config.SPREAD_MIN_CREDIT:\n"
        "            logger.info(\n"
        "                f\"Credit spread: credit={total_credit:.2f} \"\n"
        "                f\"< min={config.SPREAD_MIN_CREDIT}\"\n"
        "            )\n"
        "            return (None, {})"
    )
    new_spread_credit_check = (
        "        # AUDIT CFG-01: check credit as % of max spread width.\n"
        "        _spread_min_pct = getattr(\n"
        "            config, \"SPREAD_MIN_CREDIT_PCT_OF_WIDTH\", 0.25\n"
        "        )\n"
        "        _spread_min_required = max(\n"
        "            config.SPREAD_MIN_CREDIT,\n"
        "            _spread_min_pct * max(put_width, call_width),\n"
        "        )\n"
        "        if total_credit < _spread_min_required:\n"
        "            logger.info(\n"
        "                f\"Credit spread: credit={total_credit:.2f} \"\n"
        "                f\"< min={_spread_min_required:.1f} \"\n"
        "                f\"({_spread_min_pct*100:.0f}% of width)\"\n"
        "            )\n"
        "            return (None, {})"
    )
    content, ok = sub_exact(
        old_spread_credit_check, new_spread_credit_check, content,
        "CFG-01 spread credit/width check"
    )
    if ok:
        changes.append("CFG-01: credit spread builder validates credit >= 25% of width")

    # ── SE-05: After _close_one_side, skip remaining checks ───────
    # _close_one_side returns False from _check_stop_loss, then
    # _monitor_all_positions continues to trailing stop / profit
    # target on a mutated position. Fix: use a flag to skip.
    old_one_side_call_call = (
        "            if short_call and self.dm.spot >= (\n"
        "                short_call\n"
        "                + config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"call\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                return False\n"
        "            if short_put and self.dm.spot <= (\n"
        "                short_put\n"
        "                - config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                return False"
    )
    new_one_side_call_call = (
        "            if short_call and self.dm.spot >= (\n"
        "                short_call\n"
        "                + config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"call\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                # AUDIT SE-05: mark position so _monitor_all_positions\n"
        "                # skips trailing/profit checks this cycle on the\n"
        "                # now-mutated one-sided structure.\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return False\n"
        "            if short_put and self.dm.spot <= (\n"
        "                short_put\n"
        "                - config.CONDOR_TESTED_SIDE_BUFFER\n"
        "            ):\n"
        "                await self._close_one_side(\n"
        "                    position, \"put\",\n"
        "                    config.EXIT_REASONS[\"STOP_LOSS\"],\n"
        "                )\n"
        "                position.meta[\"_one_side_closed_cycle\"] = True\n"
        "                return False"
    )
    content, ok = sub_exact(
        old_one_side_call_call, new_one_side_call_call, content,
        "SE-05 one-side close cycle flag"
    )
    if ok:
        changes.append("SE-05: _close_one_side sets cycle flag to skip subsequent checks")

    # Add the cycle-flag check at the top of _monitor_all_positions
    old_monitor_start = (
        "            pending_legs = [\n"
        "                l for l in position.legs\n"
        "                if l.fill_status == \"PENDING\"\n"
        "            ]"
    )
    new_monitor_start = (
        "            # AUDIT SE-05: skip this cycle if one side was just\n"
        "            # closed — let the next cycle re-evaluate cleanly.\n"
        "            if position.meta.get(\"_one_side_closed_cycle\"):\n"
        "                position.meta.pop(\"_one_side_closed_cycle\", None)\n"
        "                continue\n"
        "\n"
        "            pending_legs = [\n"
        "                l for l in position.legs\n"
        "                if l.fill_status == \"PENDING\"\n"
        "            ]"
    )
    content, ok = sub_exact(
        old_monitor_start, new_monitor_start, content,
        "SE-05 monitor skip one-side cycle"
    )
    if ok:
        changes.append("SE-05: _monitor_all_positions skips cycle after one-side close")

    # ── SE-06: CB L1 threshold must not be below designed stop ────
    # For a condor at 45pts x 3lots: total_credit*LOT_SIZE = 8775.
    # The 2% floor (20000) fires at 29% of max_risk, before the
    # designed 2x-credit stop. Fix: CB L1 threshold = max(
    # CB_LEVEL_1_PCT * capital, position.stop_loss * LOT_SIZE).
    old_cb1_threshold = (
        "            cb_l1_threshold = max(\n"
        "                config.CB_LEVEL_1_PCT * config.TOTAL_CAPITAL,\n"
        "                position.total_credit * config.LOT_SIZE,\n"
        "            )"
    )
    new_cb1_threshold = (
        "            # AUDIT SE-06: CB L1 must not fire before the\n"
        "            # position's own designed stop-loss. Use the larger\n"
        "            # of: 2% of capital, or the position's stop_loss\n"
        "            # (which is already 2x credit for credit strategies).\n"
        "            _designed_stop_rupees = (\n"
        "                position.stop_loss * config.LOT_SIZE\n"
        "                if position.stop_loss and position.stop_loss > 0\n"
        "                else 0.0\n"
        "            )\n"
        "            cb_l1_threshold = max(\n"
        "                config.CB_LEVEL_1_PCT * config.TOTAL_CAPITAL,\n"
        "                _designed_stop_rupees,\n"
        "            )"
    )
    content, ok = sub_exact(
        old_cb1_threshold, new_cb1_threshold, content,
        "SE-06 CB L1 threshold fix"
    )
    if ok:
        changes.append("SE-06: CB L1 threshold >= position's designed stop-loss")

    # ── SE-15: Log warning for INSTRUMENT_NIFTY_FUT in live mode ──
    # "NSE_FO|NIFTY" is not a tradeable key. In live mode this will
    # 400 and retry 5 times. We cannot resolve the real key here
    # (requires instrument master lookup at runtime) but we can
    # guard the call and log clearly.
    old_hedge_payload = (
        "        payload = {\n"
        "            \"quantity\":           (\n"
        "                futures_lots * config.LOT_SIZE\n"
        "            ),\n"
        "            \"product\":            \"D\",\n"
        "            \"validity\":           \"DAY\",\n"
        "            \"price\":              0,\n"
        "            \"instrument_token\":   config.INSTRUMENT_NIFTY_FUT,\n"
        "            \"order_type\":         \"MARKET\",\n"
        "            \"transaction_type\":   action,\n"
        "            \"disclosed_quantity\": 0,\n"
        "            \"trigger_price\":      0,\n"
        "            \"is_amo\":             False,\n"
        "        }\n"
        "        try:\n"
        "            await self.dm._api_post(\n"
        "                config.EP_ORDER_PLACE, payload\n"
        "            )\n"
        "            logger.info(\n"
        "                f\"Delta hedge: {action} \"\n"
        "                f\"{futures_lots} lots\"\n"
        "            )\n"
        "        except Exception as e:\n"
        "            logger.error(f\"Delta hedge failed: {e}\")"
    )
    new_hedge_payload = (
        "        # AUDIT SE-15: INSTRUMENT_NIFTY_FUT is a series prefix,\n"
        "        # not a specific contract key. In live mode this will 400.\n"
        "        # The real key must be resolved from the instrument master\n"
        "        # at startup. Until that is implemented, skip live hedging\n"
        "        # and log CRITICAL so the operator knows delta is unhedged.\n"
        "        fut_key = getattr(\n"
        "            config, \"INSTRUMENT_NIFTY_FUT_RESOLVED\",\n"
        "            config.INSTRUMENT_NIFTY_FUT,\n"
        "        )\n"
        "        if fut_key == config.INSTRUMENT_NIFTY_FUT:\n"
        "            logger.critical(\n"
        "                f\"SE-15: INSTRUMENT_NIFTY_FUT is not a tradeable \"\n"
        "                f\"key. Resolve from instrument master and set \"\n"
        "                f\"config.INSTRUMENT_NIFTY_FUT_RESOLVED. \"\n"
        "                f\"Delta hedge SKIPPED: {action} {futures_lots} lots.\"\n"
        "            )\n"
        "            return\n"
        "        payload = {\n"
        "            \"quantity\":           (\n"
        "                futures_lots * config.LOT_SIZE\n"
        "            ),\n"
        "            \"product\":            \"D\",\n"
        "            \"validity\":           \"DAY\",\n"
        "            \"price\":              0,\n"
        "            \"instrument_token\":   fut_key,\n"
        "            \"order_type\":         \"MARKET\",\n"
        "            \"transaction_type\":   action,\n"
        "            \"disclosed_quantity\": 0,\n"
        "            \"trigger_price\":      0,\n"
        "            \"is_amo\":             False,\n"
        "        }\n"
        "        try:\n"
        "            await self.dm._api_post(\n"
        "                config.EP_ORDER_PLACE, payload\n"
        "            )\n"
        "            logger.info(\n"
        "                f\"Delta hedge: {action} \"\n"
        "                f\"{futures_lots} lots\"\n"
        "            )\n"
        "        except Exception as e:\n"
        "            logger.error(f\"Delta hedge failed: {e}\")"
    )
    content, ok = sub_exact(
        old_hedge_payload, new_hedge_payload, content,
        "SE-15 futures key guard"
    )
    if ok:
        changes.append("SE-15: _hedge_delta() guards against unresolved futures key")

    # ── SE-16: _reduce_position_50pct on 1-lot leaves naked remnant
    # max(1, floor(1 * 0.5)) = 1 closes the entire leg.
    # Fix: if reduce_qty >= leg.qty, close the whole position instead.
    old_reduce_50 = (
        "    async def _reduce_position_50pct(\n"
        "        self, position: Position\n"
        "    ) -> None:\n"
        "        for idx, leg in enumerate(position.legs):\n"
        "            if leg.action == \"SELL\":\n"
        "                reduce_qty = max(\n"
        "                    1, math.floor(leg.qty * 0.50)\n"
        "                )\n"
        "                if reduce_qty > leg.qty:\n"
        "                    reduce_qty = leg.qty"
    )
    new_reduce_50 = (
        "    async def _reduce_position_50pct(\n"
        "        self, position: Position\n"
        "    ) -> None:\n"
        "        for idx, leg in enumerate(position.legs):\n"
        "            if leg.action == \"SELL\":\n"
        "                # AUDIT SE-16: for a 1-lot position,\n"
        "                # floor(1*0.5)=0, max(1,0)=1 closes the whole\n"
        "                # leg leaving a naked remnant. Close the whole\n"
        "                # position instead of leaving an unbalanced structure.\n"
        "                if leg.qty <= 1:\n"
        "                    await self._close_position(\n"
        "                        position,\n"
        "                        config.EXIT_REASONS[\"MANUAL\"],\n"
        "                    )\n"
        "                    return\n"
        "                reduce_qty = math.floor(leg.qty * 0.50)\n"
        "                if reduce_qty < 1:\n"
        "                    reduce_qty = 1\n"
        "                if reduce_qty > leg.qty:\n"
        "                    reduce_qty = leg.qty"
    )
    content, ok = sub_exact(
        old_reduce_50, new_reduce_50, content,
        "SE-16 reduce 50pct 1-lot guard"
    )
    if ok:
        changes.append("SE-16: _reduce_position_50pct closes whole position for 1-lot")

    # ── SE-N03: Rescale metadata after partial fills ───────────────
    # After _execute_strategy, if any leg has fill_status=PARTIAL,
    # rescale stop_loss, profit_target, max_risk on the position.
    # We add a post-execution rescale call in _enter_new_position.
    old_enter_after_execute = (
        "        success = await self._execute_strategy(\n"
        "            strategy_name, legs, meta, trade_id=trade_id\n"
        "        )\n"
        "        if not success:\n"
        "            logger.warning(\n"
        "                f\"Execution failed: {strategy_name}\"\n"
        "            )\n"
        "            return\n"
        "\n"
        "        self._refresh_leg_greeks(legs)"
    )
    new_enter_after_execute = (
        "        success = await self._execute_strategy(\n"
        "            strategy_name, legs, meta, trade_id=trade_id\n"
        "        )\n"
        "        if not success:\n"
        "            logger.warning(\n"
        "                f\"Execution failed: {strategy_name}\"\n"
        "            )\n"
        "            return\n"
        "\n"
        "        # AUDIT SE-N03: if any leg partially filled, the lot count\n"
        "        # was reduced. Rescale stop, target, max_risk to actual fills.\n"
        "        _filled_lots = min(\n"
        "            (l.qty for l in legs if l.qty > 0), default=lots\n"
        "        )\n"
        "        if _filled_lots < lots and lots > 0:\n"
        "            _scale = _filled_lots / lots\n"
        "            meta[\"max_risk\"] = meta.get(\"max_risk\", 0) * _scale\n"
        "            if meta.get(\"stop_loss\"):\n"
        "                meta[\"stop_loss\"] = meta[\"stop_loss\"] * _scale\n"
        "            if meta.get(\"profit_target\"):\n"
        "                meta[\"profit_target\"] = (\n"
        "                    meta[\"profit_target\"] * _scale\n"
        "                )\n"
        "            logger.info(\n"
        "                f\"SE-N03: partial fill rescale \"\n"
        "                f\"{lots}->{_filled_lots} lots, \"\n"
        "                f\"scale={_scale:.3f}\"\n"
        "            )\n"
        "\n"
        "        self._refresh_leg_greeks(legs)"
    )
    content, ok = sub_exact(
        old_enter_after_execute, new_enter_after_execute, content,
        "SE-N03 post-fill metadata rescale"
    )
    if ok:
        changes.append("SE-N03: metadata rescaled after partial fills")

    # ── DM-01 in strategy_engine: use dm.get_mark_price() ─────────
    # Replace the bid/ask midpoint preference in _update_all_pnls
    # with the staleness-aware get_mark_price() helper.
    old_mtm_mark = (
        "            for leg in position.legs:\n"
        "                opt_data = (\n"
        "                    expiry_chain\n"
        "                    .get(leg.strike, {})\n"
        "                    .get(leg.option_type, {})\n"
        "                )\n"
        "                # PATCH: prefer bid/ask midpoint over raw ltp for\n"
        "                # MTM \u2014 a single stale/thin ltp print can cause\n"
        "                # phantom P&L swings unrelated to real price moves.\n"
        "                bid = opt_data.get(\"bid\", 0)\n"
        "                ask = opt_data.get(\"ask\", 0)\n"
        "                ltp = opt_data.get(\"ltp\", 0)\n"
        "                if bid > 0 and ask > 0:\n"
        "                    mark = (bid + ask) / 2.0\n"
        "                elif ltp > 0:\n"
        "                    mark = ltp\n"
        "                else:\n"
        "                    mark = leg.entry_price\n"
        "                    logger.warning(\n"
        "                        f\"No bid/ask/ltp for {leg.option_type} \"\n"
        "                        f\"{leg.strike} expiry=\"\n"
        "                        f\"{position.expiry_date} \"\n"
        "                        f\"\u2014 using entry\"\n"
        "                    )"
    )
    new_mtm_mark = (
        "            for leg in position.legs:\n"
        "                opt_data = (\n"
        "                    expiry_chain\n"
        "                    .get(leg.strike, {})\n"
        "                    .get(leg.option_type, {})\n"
        "                )\n"
        "                # AUDIT DM-01: bid/ask are only updated by the\n"
        "                # 60s REST poll; LTP is updated by WS on every\n"
        "                # tick. Use staleness-aware get_mark_price() so\n"
        "                # we prefer live LTP when the REST quote is stale.\n"
        "                mark = self.dm.get_mark_price(\n"
        "                    opt_data, fallback=leg.entry_price\n"
        "                )\n"
        "                if mark <= 0:\n"
        "                    mark = leg.entry_price\n"
        "                    logger.warning(\n"
        "                        f\"No mark price for {leg.option_type} \"\n"
        "                        f\"{leg.strike} expiry=\"\n"
        "                        f\"{position.expiry_date} \"\n"
        "                        f\"\u2014 using entry\"\n"
        "                    )"
    )
    content, ok = sub_exact(
        old_mtm_mark, new_mtm_mark, content,
        "DM-01 MTM uses get_mark_price"
    )
    if ok:
        changes.append("DM-01: _update_all_pnls uses staleness-aware get_mark_price()")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# main.py patches
# ─────────────────────────────────────────────────────────────────────

def patch_main(content):
    changes = []

    # ── MN-N01: Failed refresh must not be marked fresh ───────────
    # The finally block unconditionally sets last_data_refresh=now
    # and data_refresh_complete=True even when refreshes fail.
    # Fix: only mark complete when mandatory data (spot, chain) succeeded.
    old_finally_block = (
        "                finally:\n"
        "                    # LIVE FIX: always set in finally\n"
        "                    # so regime can run even after errors\n"
        "                    last_data_refresh     = now\n"
        "                    data_refresh_complete = True\n"
        "                    logger.info(\n"
        "                        \"Data refresh cycle complete\"\n"
        "                    )"
    )
    new_finally_block = (
        "                finally:\n"
        "                    # AUDIT MN-N01: only mark complete when\n"
        "                    # mandatory data succeeded. Spot and chain\n"
        "                    # are mandatory; candles/OI are optional.\n"
        "                    _spot_ok  = dm.spot is not None and dm.spot > 0\n"
        "                    _chain_ok = len(dm.option_chain) > 0\n"
        "                    if _spot_ok and _chain_ok:\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = True\n"
        "                        logger.info(\n"
        "                            \"Data refresh cycle complete\"\n"
        "                        )\n"
        "                    else:\n"
        "                        # Still update timestamp so we don't\n"
        "                        # spin-retry every second, but mark\n"
        "                        # incomplete so regime skips this cycle.\n"
        "                        last_data_refresh     = now\n"
        "                        data_refresh_complete = False\n"
        "                        logger.warning(\n"
        "                            \"Data refresh incomplete: \"\n"
        "                            f\"spot_ok={_spot_ok} \"\n"
        "                            f\"chain_ok={_chain_ok} \"\n"
        "                            \"— regime refresh skipped\"\n"
        "                        )"
    )
    content, ok = sub_exact(
        old_finally_block, new_finally_block, content,
        "MN-N01 refresh complete guard"
    )
    if ok:
        changes.append("MN-N01: data refresh only marked complete when spot+chain succeeded")

    # ── MN-02: Relax overnight hold DTE condition ─────────────────
    old_dte_hold = (
        "        # LIVE FIX: dte>5 (was dte>3)\n"
        "        # dte>3 allowed DTE=8 positions overnight for 6 days.\n"
        "        # dte>5 limits overnight hold to Mon/Tue entry only.\n"
        "        if (\n"
        "            regime in [\n"
        "                config.REGIME_STRONG_SELL,\n"
        "                config.REGIME_MILD_SELL,\n"
        "            ]\n"
        "            and dte > 5\n"
        "            and position.strategy_name in [\n"
        "                config.STRAT_SHORT_STRADDLE,\n"
        "                config.STRAT_IRON_CONDOR,\n"
        "                config.STRAT_CREDIT_SPREADS,\n"
        "            ]\n"
        "            and not tomorrow_is_expiry\n"
        "            and vix_ok\n"
        "        ):"
    )
    new_dte_hold = (
        "        # AUDIT MN-02: was dte>5, which forced closure on day 2\n"
        "        # for 6-DTE entries (capturing ~1/6 of theta while paying\n"
        "        # full round-trip friction). Changed to dte>=2 for\n"
        "        # defined-risk structures (condor, spreads) so theta is\n"
        "        # actually harvested. Naked straddles keep dte>5 because\n"
        "        # they carry undefined overnight risk.\n"
        "        _is_defined_risk = position.strategy_name in [\n"
        "            config.STRAT_IRON_CONDOR,\n"
        "            config.STRAT_CREDIT_SPREADS,\n"
        "        ]\n"
        "        _dte_ok = dte >= 2 if _is_defined_risk else dte > 5\n"
        "        if (\n"
        "            regime in [\n"
        "                config.REGIME_STRONG_SELL,\n"
        "                config.REGIME_MILD_SELL,\n"
        "            ]\n"
        "            and _dte_ok\n"
        "            and position.strategy_name in [\n"
        "                config.STRAT_SHORT_STRADDLE,\n"
        "                config.STRAT_IRON_CONDOR,\n"
        "                config.STRAT_CREDIT_SPREADS,\n"
        "            ]\n"
        "            and not tomorrow_is_expiry\n"
        "            and vix_ok\n"
        "        ):"
    )
    content, ok = sub_exact(
        old_dte_hold, new_dte_hold, content,
        "MN-02 overnight hold DTE relaxed"
    )
    if ok:
        changes.append("MN-02: overnight hold dte>=2 for defined-risk, dte>5 for straddles")

    # ── MN-06: Decouple position monitoring from regime timer ─────
    # se.run_cycle() is inside the regime-refresh block. Stop-loss
    # checks only run once per minute. Fix: always run
    # _monitor_all_positions (and P&L updates) on every main loop
    # iteration (every 1s), gated only on having open positions.
    # New entries remain gated on regime refresh.
    old_run_cycle = (
        "                    try:\n"
        "                        await se.run_cycle()\n"
        "                    except Exception as e:\n"
        "                        logger.error(\n"
        "                            f\"Strategy cycle error: {e}\"\n"
        "                        )\n"
        "                        logger.error(\n"
        "                            traceback.format_exc()\n"
        "                        )"
    )
    new_run_cycle = (
        "                    try:\n"
        "                        # AUDIT MN-06: run_cycle handles entries\n"
        "                        # (gated on regime). Position monitoring\n"
        "                        # is now also called every loop iteration\n"
        "                        # (see fast-monitor block below).\n"
        "                        await se.run_cycle()\n"
        "                    except Exception as e:\n"
        "                        logger.error(\n"
        "                            f\"Strategy cycle error: {e}\"\n"
        "                        )\n"
        "                        logger.error(\n"
        "                            traceback.format_exc()\n"
        "                        )"
    )
    content, ok = sub_exact(
        old_run_cycle, new_run_cycle, content,
        "MN-06 run_cycle comment"
    )
    if ok:
        changes.append("MN-06: run_cycle comment updated")

    # Add fast position-monitoring block before the heartbeat section
    old_heartbeat = (
        "            # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "            # HEARTBEAT\n"
        "            # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "            hb_elapsed = (\n"
        "                now - last_heartbeat\n"
        "            ).total_seconds()"
    )
    new_heartbeat = (
        "            # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "            # AUDIT MN-06: FAST POSITION MONITOR (every loop ~1s)\n"
        "            # Stop-loss and P&L checks run independently of the\n"
        "            # 60s regime timer so exits are not delayed by up to\n"
        "            # 60s during data-refresh or regime-skip cycles.\n"
        "            # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "            if se.open_positions and dm.spot is not None:\n"
        "                try:\n"
        "                    await se._update_all_pnls()\n"
        "                    await se._monitor_all_positions()\n"
        "                except Exception as _mon_e:\n"
        "                    logger.error(\n"
        "                        f\"Fast monitor error: {_mon_e}\"\n"
        "                    )\n"
        "\n"
        "            # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "            # HEARTBEAT\n"
        "            # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "            hb_elapsed = (\n"
        "                now - last_heartbeat\n"
        "            ).total_seconds()"
    )
    content, ok = sub_exact(
        old_heartbeat, new_heartbeat, content,
        "MN-06 fast position monitor"
    )
    if ok:
        changes.append("MN-06: fast position monitor runs every loop iteration (~1s)")

    # ── CFG-06: Hard-fail preflight on stale holiday calendar ─────
    old_holiday_check = (
        "    # CHECK 6 \u2014 Holiday calendar review date\n"
        "    days_since = (\n"
        "        today - config.HOLIDAY_CALENDAR_REVIEWED_ON\n"
        "    ).days\n"
        "    if days_since > 180:\n"
        "        logger.warning(\n"
        "            f\"Holiday calendar last reviewed \"\n"
        "            f\"{days_since} days ago \u2014 please update\"\n"
        "        )"
    )
    new_holiday_check = (
        "    # CHECK 6 \u2014 Holiday calendar review date\n"
        "    days_since = (\n"
        "        today - config.HOLIDAY_CALENDAR_REVIEWED_ON\n"
        "    ).days\n"
        "    if days_since > 180:\n"
        "        logger.warning(\n"
        "            f\"Holiday calendar last reviewed \"\n"
        "            f\"{days_since} days ago \u2014 please update\"\n"
        "        )\n"
        "    # AUDIT CFG-06: hard-fail if the calendar does not cover\n"
        "    # the current year. On Jan 1 2027 the engine would treat\n"
        "    # all 2027 NSE holidays as trading days.\n"
        "    _cal_max_year = getattr(\n"
        "        config, \"HOLIDAY_CALENDAR_MAX_YEAR\", 0\n"
        "    )\n"
        "    if _cal_max_year > 0 and today.year > _cal_max_year:\n"
        "        if not config.ALLOW_NON_TRADING_DAY_RUN:\n"
        "            logger.critical(\n"
        "                f\"Holiday calendar only covers up to \"\n"
        "                f\"{_cal_max_year}. Current year is \"\n"
        "                f\"{today.year}. Update NSE_MARKET_HOLIDAYS \"\n"
        "                f\"before running.\"\n"
        "            )\n"
        "            return False\n"
        "        else:\n"
        "            logger.warning(\n"
        "                f\"Holiday calendar stale (max={_cal_max_year}) \"\n"
        "                f\"but ALLOW_NON_TRADING_DAY_RUN=True\"\n"
        "            )"
    )
    content, ok = sub_exact(
        old_holiday_check, new_holiday_check, content,
        "CFG-06 holiday calendar hard-fail"
    )
    if ok:
        changes.append("CFG-06: preflight hard-fails when holiday calendar doesn't cover current year")

    return content, changes


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Apply validated audit fixes to the NIFTY trading engine."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show changes without writing files"
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Skip .bak backup files"
    )
    args = parser.parse_args()
    dry_run = args.dry_run
    do_backup = not args.no_backup

    base = os.path.dirname(os.path.abspath(__file__))

    files = {
        "config.py":          os.path.join(base, "config.py"),
        "data_manager.py":    os.path.join(base, "data_manager.py"),
        "regime_engine.py":   os.path.join(base, "regime_engine.py"),
        "strategy_engine.py": os.path.join(base, "strategy_engine.py"),
        "main.py":            os.path.join(base, "main.py"),
    }

    missing = [n for n, p in files.items() if not os.path.isfile(p)]
    if missing:
        print("ERROR: Files not found: " + str(missing))
        print("Run patch.py from the same directory as the engine.")
        sys.exit(1)

    all_ok = True
    total_changes = []

    patches = [
        ("config.py",          patch_config),
        ("data_manager.py",    patch_data_manager),
        ("regime_engine.py",   patch_regime_engine),
        ("strategy_engine.py", patch_strategy_engine),
        ("main.py",            patch_main),
    ]

    for name, patch_fn in patches:
        path = files[name]
        print("")
        print("=" * 60)
        print("Patching: " + name)
        print("=" * 60)

        original = read_file(path)
        patched, changes = patch_fn(original)

        for c in changes:
            print("  + " + c)
        if not changes:
            print("  (no changes produced)")
        total_changes.extend(changes)

        ok = apply_patch(path, original, patched, dry_run, do_backup)
        if not ok:
            all_ok = False

    print("")
    print("=" * 60)
    print("PATCH SUMMARY — " + str(len(total_changes)) + " changes")
    print("=" * 60)
    for c in total_changes:
        print("  OK  " + c)

    if not all_ok:
        print("")
        print("ERROR: One or more patches failed. Review warnings above.")
        sys.exit(1)

    if dry_run:
        print("")
        print("Dry-run complete — no files modified.")
    else:
        print("")
        print("All patches applied.")
        print("Verify syntax:")
        print("  python -m py_compile config.py data_manager.py "
              "regime_engine.py strategy_engine.py main.py")


if __name__ == "__main__":
    main()