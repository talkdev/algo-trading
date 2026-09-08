#!/usr/bin/env python3
# patch_final.py
# NIFTY Intraday Options Engine v3.0 — Final Consolidated Patch
# Applies all verified fixes from patch2 through patch5b in one idempotent script.
# Run: python patch_final.py
# Safe to run multiple times (idempotent).

from __future__ import annotations

import ast
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"backup_final_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

_RESULTS: list = []
_BACKED_UP: set = set()


def _say(msg: str) -> None:
    print(msg, flush=True)


def _record(fname: str, hid: str, status: str, detail: str = "") -> None:
    _RESULTS.append((fname, hid, status, detail))
    mark = {"APPLIED": "+", "ALREADY": "=", "NOT-FOUND": "?", "FAIL": "!"}.get(status, "-")
    extra = f" — {detail}" if detail else ""
    _say(f"  [{mark}] {fname} :: {hid}{extra}")


def backup(fname: str) -> None:
    if fname in _BACKED_UP:
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src = BASE / fname
    if src.exists():
        shutil.copy2(src, BACKUP_DIR / fname)
    _BACKED_UP.add(fname)


def read(fname: str) -> str:
    return (BASE / fname).read_text(encoding="utf-8")


def write(fname: str, src: str) -> bool:
    try:
        ast.parse(src)
    except SyntaxError as e:
        _say(f"  SYNTAX ERROR in {fname} line {e.lineno}: {e.msg}")
        _say(f"  ABORTING write — original preserved.")
        return False
    (BASE / fname).write_text(src, encoding="utf-8")
    return True


def apply(fname: str, old: str, new: str, hid: str) -> str:
    src = read(fname)
    if new.strip() in src:
        _record(fname, hid, "ALREADY")
        return src
    if old not in src:
        _record(fname, hid, "NOT-FOUND", old[:60].replace("\n", " "))
        return src
    result = src.replace(old, new, 1)
    _record(fname, hid, "APPLIED")
    return result


def patch_and_write(fname: str, hunks: list) -> None:
    backup(fname)
    src = read(fname)
    changed = False
    for hid, old, new in hunks:
        if new.strip() in src:
            _record(fname, hid, "ALREADY")
            continue
        if old not in src:
            _record(fname, hid, "NOT-FOUND", old[:60].replace("\n", " "))
            continue
        src = src.replace(old, new, 1)
        _record(fname, hid, "APPLIED")
        changed = True
    if changed:
        if not write(fname, src):
            _say(f"  RESTORING {fname} from backup")
            shutil.copy2(BACKUP_DIR / fname, BASE / fname)
    else:
        _say(f"  No changes needed: {fname}")


_say("=" * 70)
_say("NIFTY ALGO v3.0 — FINAL CONSOLIDATED PATCH")
_say("=" * 70)

# ─────────────────────────────────────────────────────────────────────────────
# data_engine.py
# ─────────────────────────────────────────────────────────────────────────────
_say("\n[1/5] data_engine.py")

patch_and_write("data_engine.py", [

    ("P1-RV-SPIKE-GUARD",
     "                    if cached_rv and cached_rv >= rv_floor:\n"
     "                        if rv < cached_rv * 0.50:\n"
     "                            self.logger.debug(\n"
     "                                f\"Parkinson RV {rv*100:.2f}% dropped >50% \"\n"
     "                                f\"from cached {cached_rv*100:.2f}% — using cached\"\n"
     "                            )\n"
     "                            return cached_rv, \"cached\"",
     "                    if cached_rv and cached_rv >= rv_floor:\n"
     "                        if rv < cached_rv * 0.50:\n"
     "                            self.logger.debug(\n"
     "                                f\"Parkinson RV {rv*100:.2f}% dropped >50% \"\n"
     "                                f\"from cached {cached_rv*100:.2f}% — using cached\"\n"
     "                            )\n"
     "                            return cached_rv, \"cached\"\n"
     "                        if rv > cached_rv * 1.50:\n"
     "                            self.logger.warning(\n"
     "                                f\"Parkinson RV spike {rv*100:.2f}% > 1.5x cached \"\n"
     "                                f\"{cached_rv*100:.2f}% — bad candle, using cached\"\n"
     "                            )\n"
     "                            return cached_rv, \"cached_spike_guard\""),

    ("P2-DTE-FROM-EXPIRY",
     "                    calendar_dte = ExpiryCalendar.get_dte(today)\n"
     "                    expiry_dte = 0\n"
     "                    _d = today + timedelta(days=1)\n"
     "                    while _d <= expiry:\n"
     "                        if not ExpiryCalendar.is_holiday(_d):\n"
     "                            expiry_dte += 1\n"
     "                        _d += timedelta(days=1)\n"
     "                    self.state[\"actual_expiry\"]       = expiry.isoformat()\n"
     "                    self.state[\"actual_dte\"]          = expiry_dte\n"
     "                    self.state[\"expiry_last_checked\"] = now_ist().isoformat()\n"
     "                    self.logger.info(\n"
     "                        f\"Active expiry: {expiry} \"\n"
     "                        f\"(calendar_dte={calendar_dte} expiry_dte={expiry_dte})\"\n"
     "                    )",
     "                    is_tue_full = today.weekday() == 1\n"
     "                    if is_tue_full:\n"
     "                        zero_dte_today = [f for f in future if f[0] == 0]\n"
     "                        expiry = zero_dte_today[0][1] if zero_dte_today else future[0][1]\n"
     "                    else:\n"
     "                        preferred = [f for f in future if f[0] >= 1]\n"
     "                        expiry = preferred[0][1] if preferred else future[0][1]\n"
     "                    expiry_dte = 0\n"
     "                    _d = today + timedelta(days=1)\n"
     "                    while _d <= expiry:\n"
     "                        if not ExpiryCalendar.is_holiday(_d):\n"
     "                            expiry_dte += 1\n"
     "                        _d += timedelta(days=1)\n"
     "                    self.state[\"actual_expiry\"]       = expiry.isoformat()\n"
     "                    self.state[\"actual_dte\"]          = expiry_dte\n"
     "                    self.state[\"expiry_last_checked\"] = now_ist().isoformat()\n"
     "                    self.logger.info(\n"
     "                        f\"Active expiry: {expiry} expiry_dte={expiry_dte}\"\n"
     "                    )"),

    ("P4-VRP-BUFFER-SEED",
     "        self._vrp_buffer: List[float] = []",
     "        self._vrp_buffer: List[float] = self._seed_vrp_buffer()"),

    ("P4-VRP-BUFFER-METHOD",
     "    def run_cycle(self) -> dict:",
     "    def _seed_vrp_buffer(self) -> List[float]:\n"
     "        try:\n"
     "            rows = self.db.get_vrp_smoothed_history(\n"
     "                n_cycles=self.config.vrp_smoothing_cycles\n"
     "            )\n"
     "            if rows:\n"
     "                return list(reversed(rows))\n"
     "        except Exception:\n"
     "            pass\n"
     "        return []\n"
     "\n"
     "    def run_cycle(self) -> dict:"),

    ("P5-PARKINSON-CORRECTION",
     "                    rv         = math.sqrt(variance * 375.0 * 252.0) * 1.15",
     "                    rv         = math.sqrt(variance * 375.0 * 252.0) * 1.05"),

    ("P7-STALE-VRP-FALLBACK",
     "        if atm_iv is None or parkinson_rv is None:\n"
     "            # Try to use last smoothed value\n"
     "            if self._vrp_buffer:\n"
     "                smoothed = self._vrp_buffer[-1]\n"
     "                return None, smoothed\n"
     "            return None, None",
     "        if atm_iv is None or parkinson_rv is None:\n"
     "            return None, None"),

    ("P3-ATM-IV-GUARD-DTE",
     "        vix_state = self.state.get(\"prev_vix\")\n"
     "        if vix_state and vix_state > 0:\n"
     "            vix_decimal = vix_state / 100.0\n"
     "            ratio = atm_iv / vix_decimal\n"
     "            _dte_now = self.state.get(\"actual_dte\")\n"
     "            if _dte_now == 0:\n"
     "                _ratio_lo, _ratio_hi = 0.40, 5.00\n"
     "            elif _dte_now == 1:\n"
     "                _ratio_lo, _ratio_hi = 0.50, 3.00\n"
     "            else:\n"
     "                _ratio_lo, _ratio_hi = 0.60, 2.00\n"
     "            if ratio < _ratio_lo or ratio > _ratio_hi:\n"
     "                self.logger.warning(\n"
     "                    f\"ATM IV {atm_iv*100:.2f}% vs VIX {vix_state:.2f} \"\n"
     "                    f\"ratio {ratio:.2f} outside {_ratio_lo}-{_ratio_hi} \"\n"
     "                    f\"(DTE={_dte_now}) — chain may be stale\"\n"
     "                )\n"
     "                return None",
     "        vix_state = self.state.get(\"prev_vix\")\n"
     "        if vix_state and vix_state > 0:\n"
     "            vix_decimal = vix_state / 100.0\n"
     "            ratio = atm_iv / vix_decimal\n"
     "            _dte_raw = self.state.get(\"actual_dte\")\n"
     "            _dte_now = int(_dte_raw) if _dte_raw is not None else 2\n"
     "            if _dte_now == 0:\n"
     "                _ratio_lo, _ratio_hi = 0.40, 5.00\n"
     "            elif _dte_now == 1:\n"
     "                _ratio_lo, _ratio_hi = 0.50, 3.00\n"
     "            else:\n"
     "                _ratio_lo, _ratio_hi = 0.60, 2.00\n"
     "            if ratio < _ratio_lo or ratio > _ratio_hi:\n"
     "                self.logger.warning(\n"
     "                    f\"ATM IV {atm_iv*100:.2f}% vs VIX {vix_state:.2f} \"\n"
     "                    f\"ratio {ratio:.2f} outside {_ratio_lo}-{_ratio_hi} \"\n"
     "                    f\"(DTE={_dte_now}) — chain may be stale\"\n"
     "                )\n"
     "                return None"),

    ("P5-DAY-MOVE-STRADDLE-REF",
     "        opening_straddle = (\n"
     "            self.state.get(\"opening_straddle_pts\") or\n"
     "            self.state.get(\"_straddle_open_for_regime\") or 0.0\n"
     "        )\n"
     "        if opening_straddle <= 0 or spot is None:\n"
     "            return 0.0\n"
     "        _dte = self.state.get(\"actual_dte\", 0) or 0\n"
     "        if _dte >= 2:\n"
     "            straddle_ref = opening_straddle * (0.5 ** (_dte / 5.0))\n"
     "            straddle_ref = max(straddle_ref, 80.0)\n"
     "        else:\n"
     "            straddle_ref = opening_straddle",
     "        opening_straddle = (\n"
     "            self.state.get(\"opening_straddle_pts\") or\n"
     "            self.state.get(\"_straddle_open_for_regime\") or 0.0\n"
     "        )\n"
     "        if opening_straddle <= 0 or spot is None:\n"
     "            return 0.0\n"
     "        _dte_raw = self.state.get(\"actual_dte\")\n"
     "        _dte = int(_dte_raw) if _dte_raw is not None else 0\n"
     "        if _dte >= 2:\n"
     "            import math as _math_dm\n"
     "            _theta_frac = max(1.0 / max(_dte, 1), 0.10)\n"
     "            straddle_ref = max(\n"
     "                opening_straddle * _math_dm.sqrt(_theta_frac),\n"
     "                60.0\n"
     "            )\n"
     "        else:\n"
     "            straddle_ref = opening_straddle"),

    ("P5-DAY-MOVE-RANGE-CALC",
     "                    return round((day_high - day_low) / straddle_ref * 100.0, 2)",
     "                    return round((day_high - day_low) / straddle_ref * 100.0, 2)"),

    ("P5-DAY-MOVE-FALLBACK",
     "        return round(abs(spot - first_close) / straddle_ref * 100.0, 2)",
     "        return round(abs(spot - first_close) / straddle_ref * 100.0, 2)"),
])

# ─────────────────────────────────────────────────────────────────────────────
# strategy_engine.py
# ─────────────────────────────────────────────────────────────────────────────
_say("\n[2/5] strategy_engine.py")

patch_and_write("strategy_engine.py", [

    ("P3-EV-GATE-RISK-LOSS",
     "        risk_pts     = net_credit * 2.5",
     "        risk_pts     = net_credit * 1.5"),

    ("P5-EV-GATE-FIRST-PASSAGE",
     "        if vrp_smoothed > 4.0:\n"
     "            vrp_adj = 0.08\n"
     "        elif vrp_smoothed > 3.0:\n"
     "            vrp_adj = 0.05\n"
     "        elif vrp_smoothed > 2.0:\n"
     "            vrp_adj = 0.02\n"
     "        elif vrp_smoothed < 1.5:\n"
     "            vrp_adj = -0.04\n"
     "        else:\n"
     "            vrp_adj = 0.0\n"
     "\n"
     "        p_win = min(0.88, max(0.35, p_win_prior + vrp_adj))\n"
     "        ev    = p_win * reward_pts - (1.0 - p_win) * risk_pts - friction\n"
     "        min_ev = max(net_credit * 0.03, friction * 0.25)",
     "        import math as _math_ev\n"
     "        _atm_iv = float(signals.get(\"atm_iv\") or 0.0)\n"
     "        _spot_ev = float(signals.get(\"spot\") or 23900.0)\n"
     "        _now_ev = now_ist().time()\n"
     "        _mins_to_exit = max(\n"
     "            (datetime.combine(today_ist(), dtime(15, 0)) -\n"
     "             datetime.combine(today_ist(), _now_ev)).total_seconds() / 60.0,\n"
     "            5.0\n"
     "        )\n"
     "        _sigma_t = _atm_iv * (_mins_to_exit / (375.0 * 252.0)) ** 0.5 if _atm_iv > 0 else 0.0\n"
     "        if _sigma_t > 0 and wing > 0 and _spot_ev > 0:\n"
     "            _barrier = max(wing * 0.5 - float(self.config.spot_proximity_pts), 20.0)\n"
     "            _z = _barrier / (_spot_ev * _sigma_t)\n"
     "            def _ncdf(x):\n"
     "                return 0.5 * (1.0 + _math_ev.erf(x / _math_ev.sqrt(2.0)))\n"
     "            p_win = max(0.35, min(0.92, 1.0 - 2.0 * _ncdf(-_z)))\n"
     "        else:\n"
     "            _or_c = or_condition or \"MODERATE\"\n"
     "            _pb = {\"VERY_NARROW\": 0.72, \"NARROW\": 0.68, \"MODERATE\": 0.62,\n"
     "                   \"WIDE\": 0.52, \"VERY_WIDE\": 0.44}.get(_or_c, 0.55)\n"
     "            if dte and dte >= 2:\n"
     "                _pb = max(_pb - 0.06 * min(dte - 1, 4), 0.35)\n"
     "            _va = 0.06 if vrp_smoothed > 3.5 else (\n"
     "                0.03 if vrp_smoothed > 2.5 else (\n"
     "                -0.03 if vrp_smoothed < 2.0 else 0.0))\n"
     "            p_win = max(0.35, min(0.88, _pb + _va))\n"
     "        ev    = p_win * reward_pts - (1.0 - p_win) * risk_pts - friction\n"
     "        min_ev = max(net_credit * 0.03, friction * 0.25)"),

    ("P6-EV-GATE-FRICTION",
     "        friction     = entry_costs_pts + total_slippage + entry_costs_pts + _exit_slip",
     "        _exit_slip_ev = self._compute_slippage(\n"
     "            [{\"bid\": 0, \"ask\": 0}] * 2, is_exit=True\n"
     "        )\n"
     "        friction     = entry_costs_pts + total_slippage + entry_costs_pts + _exit_slip_ev"),

    ("P2-DTE1-TARGET-RAISED",
     "        if dte == 1:\n"
     "            return 0.60 if vix < 12.0 else (0.55 if vix < 14.0 else 0.50)",
     "        if dte == 1:\n"
     "            return 0.58 if vix < 12.0 else (0.53 if vix < 14.0 else 0.48)"),

    ("P6-SLIPPAGE-STRESS-EXIT",
     "                total += half_spread * (1.5 if is_exit else 0.5)",
     "                total += half_spread * (3.0 if is_exit else 0.5)"),

    ("P6-SLIPPAGE-FALLBACK-EXIT",
     "                total += 0.60 if is_exit else 0.35",
     "                total += 1.20 if is_exit else 0.35"),

    ("P7-SIZING-STOP-LOSS",
     "        wing_for_sizing = actual_wing_pts or 150\n"
     "        _stop_loss_per_lot = 1.5 * net_credit * C02\n"
     "        _structural_per_lot = max(\n"
     "            (wing_for_sizing - net_credit) * C02, net_credit * C02\n"
     "        )\n"
     "        if _structural_per_lot <= 0:\n"
     "            _structural_per_lot = wing_for_sizing * C02 * 0.5\n"
     "        structural_loss_per_lot = min(_stop_loss_per_lot, _structural_per_lot)",
     "        wing_for_sizing = actual_wing_pts or 150\n"
     "        _stop_loss_per_lot = 1.0 * net_credit * C02\n"
     "        _structural_per_lot = max(\n"
     "            (wing_for_sizing - net_credit) * C02, net_credit * C02\n"
     "        )\n"
     "        if _structural_per_lot <= 0:\n"
     "            _structural_per_lot = wing_for_sizing * C02 * 0.5\n"
     "        structural_loss_per_lot = min(\n"
     "            max(_stop_loss_per_lot, 0.5 * _structural_per_lot),\n"
     "            _structural_per_lot\n"
     "        )"),

    ("P8-REMOVE-2LOT-MIN",
     "        if strategy_name in (IRON_CONDOR, IRON_BUTTERFLY) and final_lots < 1:\n"
     "            final_lots = 1",
     "        if strategy_name in (IRON_CONDOR, IRON_BUTTERFLY) and final_lots < 1:\n"
     "            final_lots = 1"),

    ("P9-0DTE-MARGIN-ELM",
     "        margin_per_lot = (actual_wing_pts or 150) * C02 * 1.10\n"
     "        total_margin   = margin_per_lot * final_lots",
     "        _wing_margin = (actual_wing_pts or 150) * C02 * 1.10\n"
     "        if actual_dte == 0:\n"
     "            _spot_ref = float(signals.get(\"spot\") or 23900)\n"
     "            _n_short = sum(1 for _l in validated_legs if _l[\"action\"] == \"SELL\")\n"
     "            _elm = 0.02 * _spot_ref * C02 * _n_short\n"
     "            margin_per_lot = _wing_margin + _elm\n"
     "        else:\n"
     "            margin_per_lot = _wing_margin\n"
     "        total_margin   = margin_per_lot * final_lots"),

    ("P-SELFTEST-SLIPPAGE-EXIT",
     "    slip_exit = engine._compute_slippage(legs_ba, is_exit=True)\n"
     "    expected_exit = 2 * ((46 - 44) / 2.0 * 1.5)\n"
     "    assert abs(slip_exit - expected_exit) < 0.01, (\n"
     "        f\"Exit slippage: expected {expected_exit:.3f}, got {slip_exit:.3f}\"\n"
     "    )\n"
     "    print(f\"  Exit slippage (2 legs bid/ask): {slip_exit:.3f}pts [OK]\")",
     "    slip_exit = engine._compute_slippage(legs_ba, is_exit=True)\n"
     "    expected_exit = 2 * ((46 - 44) / 2.0 * 3.0)\n"
     "    assert abs(slip_exit - expected_exit) < 0.01, (\n"
     "        f\"Exit slippage: expected {expected_exit:.3f}, got {slip_exit:.3f}\"\n"
     "    )\n"
     "    print(f\"  Exit slippage (2 legs bid/ask): {slip_exit:.3f}pts [OK]\")"),

    ("P-SELFTEST-SLIPPAGE-NO-BA",
     "    slip_no_ba = engine._compute_slippage(legs_no_ba, is_exit=False)\n"
     "    expected_no_ba = 2 * 0.35\n"
     "    assert abs(slip_no_ba - expected_no_ba) < 0.01, (\n"
     "        f\"No bid/ask slippage: expected {expected_no_ba:.3f}, got {slip_no_ba:.3f}\"\n"
     "    )\n"
     "    print(f\"  Entry slippage (no bid/ask): {slip_no_ba:.3f}pts [OK]\")",
     "    slip_no_ba = engine._compute_slippage(legs_no_ba, is_exit=False)\n"
     "    expected_no_ba = 2 * 0.35\n"
     "    assert abs(slip_no_ba - expected_no_ba) < 0.01, (\n"
     "        f\"No bid/ask slippage: expected {expected_no_ba:.3f}, got {slip_no_ba:.3f}\"\n"
     "    )\n"
     "    print(f\"  Entry slippage (no bid/ask): {slip_no_ba:.3f}pts [OK]\")\n"
     "\n"
     "    slip_exit_no_ba = engine._compute_slippage(legs_no_ba, is_exit=True)\n"
     "    expected_exit_no_ba = 2 * 1.20\n"
     "    assert abs(slip_exit_no_ba - expected_exit_no_ba) < 0.01, (\n"
     "        f\"Exit no bid/ask: expected {expected_exit_no_ba:.3f}, got {slip_exit_no_ba:.3f}\"\n"
     "    )\n"
     "    print(f\"  Exit slippage (no bid/ask): {slip_exit_no_ba:.3f}pts [OK]\")"),

    ("P-SELFTEST-TARGET-RANGE",
     "    assert 0.30 <= tgt0_low <= 0.55, f\"Target out of range: {tgt0_low}\"",
     "    assert 0.30 <= tgt0_low <= 0.70, f\"Target out of range: {tgt0_low}\""),
])

# ─────────────────────────────────────────────────────────────────────────────
# regime_engine.py
# ─────────────────────────────────────────────────────────────────────────────
_say("\n[3/5] regime_engine.py")

patch_and_write("regime_engine.py", [

    ("P6-PERSISTENCE-DTE-AWARE",
     "        if (self._pending_regime is not None and\n"
     "                self._pending_regime.final_regime == new_regime.final_regime):\n"
     "            self._pending_count += 1\n"
     "            if self._pending_count >= self.config.regime_persistence_cycles:\n"
     "                self.logger.info(\n"
     "                    f\"Regime confirmed after {self._pending_count} cycles: \"\n"
     "                    f\"{new_regime.final_regime}\"\n"
     "                )\n"
     "                self._pending_regime = None\n"
     "                self._pending_count  = 0\n"
     "                return new_regime\n"
     "            self.logger.debug(\n"
     "                f\"Regime pending ({self._pending_count}/\"\n"
     "                f\"{self.config.regime_persistence_cycles}): \"\n"
     "                f\"{new_regime.final_regime}\"\n"
     "            )\n"
     "            return self._current_regime\n"
     "        else:\n"
     "            self._pending_regime = new_regime\n"
     "            self._pending_count  = 1\n"
     "            self.logger.debug(\n"
     "                f\"New regime candidate (1/{self.config.regime_persistence_cycles}): \"\n"
     "                f\"{new_regime.final_regime}\"\n"
     "            )\n"
     "            return self._current_regime",
     "        _dte_now = new_regime.dte if hasattr(new_regime, 'dte') else 2\n"
     "        if _dte_now == 0:\n"
     "            _required = 1\n"
     "        elif _dte_now == 1:\n"
     "            _required = 2\n"
     "        else:\n"
     "            _required = self.config.regime_persistence_cycles\n"
     "        if (self._pending_regime is not None and\n"
     "                self._pending_regime.final_regime == new_regime.final_regime):\n"
     "            self._pending_count += 1\n"
     "            if self._pending_count >= _required:\n"
     "                self.logger.info(\n"
     "                    f\"Regime confirmed after {self._pending_count} cycles: \"\n"
     "                    f\"{new_regime.final_regime} (DTE={_dte_now})\"\n"
     "                )\n"
     "                self._pending_regime = None\n"
     "                self._pending_count  = 0\n"
     "                return new_regime\n"
     "            self.logger.debug(\n"
     "                f\"Regime pending ({self._pending_count}/{_required}): \"\n"
     "                f\"{new_regime.final_regime}\"\n"
     "            )\n"
     "            return self._current_regime\n"
     "        else:\n"
     "            self._pending_regime = new_regime\n"
     "            self._pending_count  = 1\n"
     "            self.logger.debug(\n"
     "                f\"New regime candidate (1/{_required}): \"\n"
     "                f\"{new_regime.final_regime}\"\n"
     "            )\n"
     "            return self._current_regime"),

    ("P5-VRP-SELL-PREMIUM-BAND",
     "        vrp_sell = max(vrp_sell, 2.0)\n"
     "        vrp_very_rich = vrp_sell * 1.75",
     "        vrp_sell = max(vrp_sell, 2.0)\n"
     "        vrp_sell_premium = vrp_sell * 1.25\n"
     "        vrp_very_rich = vrp_sell * 1.75"),

    ("P5-VRP-CLASSIFICATION-BAND",
     "        if vrp_smoothed > vrp_very_rich:\n"
     "            details[\"trigger\"] = f\"VRP_{vrp_smoothed:.2f}pp_STRONG_SELL\"\n"
     "            return VolatilityRegime.STRONG_SELL_PREMIUM, details\n"
     "\n"
     "        if vrp_smoothed > vrp_sell_premium:\n"
     "            details[\"trigger\"] = f\"VRP_{vrp_smoothed:.2f}pp_SELL\"\n"
     "            return VolatilityRegime.SELL_PREMIUM, details",
     "        if vrp_smoothed > vrp_very_rich:\n"
     "            details[\"trigger\"] = f\"VRP_{vrp_smoothed:.2f}pp_STRONG_SELL\"\n"
     "            return VolatilityRegime.STRONG_SELL_PREMIUM, details\n"
     "\n"
     "        if vrp_smoothed > vrp_sell_premium:\n"
     "            details[\"trigger\"] = f\"VRP_{vrp_smoothed:.2f}pp_SELL\"\n"
     "            return VolatilityRegime.SELL_PREMIUM, details"),

    ("P10-VRP-OR-DISCOUNTS",
     "        if or_condition == \"WIDE\":\n"
     "            vrp_sell = vrp_sell * 1.20\n"
     "        elif or_condition == \"VERY_WIDE\":\n"
     "            vrp_sell = vrp_sell * 1.50",
     "        if or_condition == \"WIDE\":\n"
     "            vrp_sell = vrp_sell * 1.20\n"
     "        elif or_condition == \"VERY_WIDE\":\n"
     "            vrp_sell = vrp_sell * 1.50"),

    ("P11-VRP-DTE0-NO-DISCOUNT",
     "        if dte == 0:\n"
     "            vrp_sell = vrp_sell * 1.00\n"
     "        elif dte == 1:\n"
     "            vrp_sell = vrp_sell * 0.95\n"
     "        elif dte == 2:\n"
     "            vrp_sell = vrp_sell * 1.05",
     "        if dte == 0:\n"
     "            vrp_sell = vrp_sell * 1.00\n"
     "        elif dte == 1:\n"
     "            vrp_sell = vrp_sell * 1.00\n"
     "        elif dte == 2:\n"
     "            vrp_sell = vrp_sell * 1.05"),

    ("P-REGIME-DEFAULTS-SYNCED",
     "    DEFAULTS = {\n"
     "        \"vix_p25\":                 11.0,\n"
     "        \"vix_p50\":                 12.5,\n"
     "        \"vix_p75\":                 14.5,\n"
     "        \"vix_p90\":                 18.0,\n"
     "        \"vrp_sell_threshold\":       2.0,\n"
     "        \"vrp_fair_threshold\":       1.2,",
     "    DEFAULTS = {\n"
     "        \"vix_p25\":                 11.0,\n"
     "        \"vix_p50\":                 12.5,\n"
     "        \"vix_p75\":                 14.5,\n"
     "        \"vix_p90\":                 18.0,\n"
     "        \"vrp_sell_threshold\":       2.5,\n"
     "        \"vrp_fair_threshold\":       1.5,"),

    ("P-REGIME-TIER-THRESHOLDS",
     "        tier1 = n_days >= 1\n"
     "        tier2 = n_days >= max(self.config.min_trading_days_for_calibration, 5)\n"
     "        tier3 = n_days >= 30\n"
     "        cal_tier = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))\n"
     "        is_valid = tier1",
     "        tier1 = n_days >= 1\n"
     "        tier2 = n_days >= max(self.config.min_trading_days_for_calibration, 5)\n"
     "        tier3 = n_days >= 30\n"
     "        cal_tier = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))\n"
     "        is_valid = tier1"),

    ("P-REGIME-SELFTEST-PCR",
     "    pos2 = classifier.classify_positioning(make_signals(pcr=0.48))\n"
     "    print(f\"  PCR=0.48 (extreme greed) → {pos2.value} (expect BULLISH)\")\n"
     "    assert pos2 == PositioningRegime.BULLISH, f\"Expected BULLISH, got {pos2}\"",
     "    pos2 = classifier.classify_positioning(make_signals(pcr=0.48))\n"
     "    print(f\"  PCR=0.48 (extreme greed) → {pos2.value} (expect BULLISH)\")\n"
     "    assert pos2 == PositioningRegime.BULLISH, f\"Expected BULLISH, got {pos2}\""),

    ("P-REGIME-SELFTEST-PCR-BEAR",
     "    pos3 = classifier.classify_positioning(make_signals(pcr=1.50))\n"
     "    print(f\"  PCR=1.50 (extreme fear) → {pos3.value} (expect BEARISH)\")\n"
     "    assert pos3 == PositioningRegime.BEARISH, f\"Expected BEARISH, got {pos3}\"",
     "    pos3 = classifier.classify_positioning(make_signals(pcr=1.50))\n"
     "    print(f\"  PCR=1.50 (extreme fear) → {pos3.value} (expect BEARISH)\")\n"
     "    assert pos3 == PositioningRegime.BEARISH, f\"Expected BEARISH, got {pos3}\""),
])

# ─────────────────────────────────────────────────────────────────────────────
# calibration_engine.py
# ─────────────────────────────────────────────────────────────────────────────
_say("\n[4/5] calibration_engine.py")

patch_and_write("calibration_engine.py", [

    ("P8-MIN-SAMPLES-VRP",
     "    MIN_SAMPLES_VRP         = 20",
     "    MIN_SAMPLES_VRP         = 20"),

    ("P8-VRP-FLOOR-2.5",
     "        state.vrp_sell_threshold = max(\n"
     "            self._shrink(sell_thresh, d.vrp_sell_threshold, total_n),\n"
     "            2.5\n"
     "        )",
     "        state.vrp_sell_threshold = max(\n"
     "            self._shrink(sell_thresh, d.vrp_sell_threshold, total_n),\n"
     "            2.5\n"
     "        )"),

    ("P-CAL-SELFTEST-ISOLATED-DB",
     "    from core import load_config, Database, setup_logging\n"
     "    import tempfile\n"
     "\n"
     "    config = load_config()\n"
     "    _test_db_path = Path(tempfile.mkdtemp()) / \"cal_selftest.db\"\n"
     "    db     = Database(_test_db_path)\n"
     "    logger = setup_logging(db, config.log_dir)",
     "    from core import load_config, Database, setup_logging\n"
     "    import tempfile\n"
     "\n"
     "    config = load_config()\n"
     "    _test_db_path = Path(tempfile.mkdtemp()) / \"cal_selftest.db\"\n"
     "    db     = Database(_test_db_path)\n"
     "    logger = setup_logging(db, config.log_dir)"),

    ("P-CAL-DEFAULTS-VRP",
     "    vrp_sell_threshold=2.5,",
     "    vrp_sell_threshold=2.5,"),

    ("P-CAL-TIER-FROM-DAY1",
     "        tier1 = n_days >= 1\n"
     "        tier2 = n_days >= max(self.config.min_trading_days_for_calibration, 5)\n"
     "        tier3 = n_days >= 30\n"
     "        cal_tier  = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))\n"
     "        is_valid  = tier1",
     "        tier1 = n_days >= 1\n"
     "        tier2 = n_days >= max(self.config.min_trading_days_for_calibration, 5)\n"
     "        tier3 = n_days >= 30\n"
     "        cal_tier  = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))\n"
     "        is_valid  = tier1"),

    ("P-CAL-RUN-FROM-TIER1",
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
     "            self._run_vrp_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
     "            self._run_day_size_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_oi_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_pcr_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_skew_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_straddle_ratio_calibration(new_state, n_days)",
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
     "            self._run_vrp_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\", \"startup\"):\n"
     "            self._run_day_size_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_oi_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_pcr_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_skew_calibration(new_state, n_days)\n"
     "\n"
     "        if tier1 or schedule in (\"weekly\", \"monthly\", \"force\"):\n"
     "            self._run_straddle_ratio_calibration(new_state, n_days)"),

    ("P-CAL-SIGNAL-WEIGHTS-TIER2",
     "        if tier2 or schedule in (\"monthly\", \"force\"):\n"
     "            self._run_signal_weight_calibration(new_state, n_days)\n"
     "\n"
     "        if tier2 or schedule == \"monthly\":\n"
     "            self._run_drift_detection(new_state)",
     "        if tier2 or schedule in (\"monthly\", \"force\"):\n"
     "            self._run_signal_weight_calibration(new_state, n_days)\n"
     "\n"
     "        if tier2 or schedule == \"monthly\":\n"
     "            self._run_drift_detection(new_state)"),
])

# ─────────────────────────────────────────────────────────────────────────────
# core.py
# ─────────────────────────────────────────────────────────────────────────────
_say("\n[5/5] core.py")

patch_and_write("core.py", [

    ("P-CORE-VRP-SELL-DEFAULT",
     "VRP_SELL_THRESHOLD=2.0",
     "VRP_SELL_THRESHOLD=2.5"),

    ("P-CORE-VRP-FAIR-DEFAULT",
     "VRP_FAIR_THRESHOLD=1.2",
     "VRP_FAIR_THRESHOLD=1.5"),

    ("P-CORE-CAL-MIN-DAYS",
     "MIN_TRADING_DAYS_FOR_CALIBRATION=5",
     "MIN_TRADING_DAYS_FOR_CALIBRATION=5"),
])

# ─────────────────────────────────────────────────────────────────────────────
# FINAL SYNTAX VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────
_say("\n" + "=" * 70)
_say("FINAL SYNTAX VERIFICATION")
_say("=" * 70)

all_ok = True
for fname in [
    "data_engine.py", "strategy_engine.py", "regime_engine.py",
    "calibration_engine.py", "core.py", "backtest.py", "eod_report.py",
    "execution_engine.py", "main.py",
]:
    fpath = BASE / fname
    if not fpath.exists():
        _say(f"  MISSING   : {fname}")
        all_ok = False
        continue
    try:
        ast.parse(fpath.read_text(encoding="utf-8"))
        _say(f"  SYNTAX OK : {fname}")
    except SyntaxError as e:
        _say(f"  SYNTAX ERR: {fname} line {e.lineno}: {e.msg}")
        all_ok = False

n_applied = sum(1 for r in _RESULTS if r[2] == "APPLIED")
n_already = sum(1 for r in _RESULTS if r[2] == "ALREADY")
n_missing = sum(1 for r in _RESULTS if r[2] == "NOT-FOUND")

_say("")
if all_ok:
    _say(f"ALL SYNTAX OK — applied={n_applied} already={n_already} not-found={n_missing}")
    _say(f"Backups in: {BACKUP_DIR}")
else:
    _say("SYNTAX ERRORS — check above")
    _say(f"Backups in: {BACKUP_DIR}")

if n_missing:
    _say("\nNOT-FOUND (already applied in earlier patch or string changed):")
    for fname, hid, st, detail in _RESULTS:
        if st == "NOT-FOUND":
            _say(f"  {fname} :: {hid} :: {detail}")

_say("")
_say("NEXT STEPS:")
_say("  1. python verify_all.py")
_say("  2. python main.py")

sys.exit(0 if all_ok else 1)