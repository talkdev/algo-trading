# patch7.py
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch7_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
_errors = []
_applied = []
_skipped = []

def backup(fp):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fp, BACKUP_DIR / Path(fp).name)

def read(fp):
    return Path(fp).read_text(encoding="utf-8")

def write(fp, content):
    backup(Path(fp))
    Path(fp).write_text(content, encoding="utf-8")

def check_syntax(fp):
    src = Path(fp).read_text(encoding="utf-8")
    try:
        ast.parse(src)
        return True
    except SyntaxError as e:
        msg = f"SYNTAX ERROR in {Path(fp).name} line {e.lineno}: {e.msg}"
        _errors.append(msg)
        print(f"  [FAIL] {msg}")
        shutil.copy2(BACKUP_DIR / Path(fp).name, fp)
        return False

def rb(src, old, new, label):
    if old not in src:
        _skipped.append(label)
        print(f"  [SKIP] Not found: {label}")
        return src
    result = src.replace(old, new, 1)
    _applied.append(label)
    print(f"  [OK] {label}")
    return result

print("=" * 68)
print("PATCH 7 - NIFTY OPTIONS ENGINE STRUCTURAL FIXES")
print(f"Base: {BASE}")
print("=" * 68)

re_path   = BASE / "regime_engine.py"
mde_path  = BASE / "market_data_engine.py"
ee_path   = BASE / "execution_engine.py"
se_path   = BASE / "strategy_engine.py"
me_path   = BASE / "main.py"
core_path = BASE / "nifty_algo_core.py"

print("\n[1/6] regime_engine.py")
src = read(re_path)

src = rb(src,
'        if vol == VolatilityRegime.ABORT:\n            return FinalRegime.EMERGENCY_EXIT, ConfidenceLevel.NONE, 0.0, 0.0, False, "ABORT: VIX emergency"',
'        if vol == VolatilityRegime.ABORT:\n            return FinalRegime.NO_TRADE, ConfidenceLevel.NONE, 0.0, 0.0, False, "ABORT: VIX high — new entries blocked"',
"RE-P1: ABORT returns NO_TRADE not EMERGENCY_EXIT — blocks entries only never closes positions")

src = rb(src,
'        if self._check_straddle_explosion(straddle):\n            snap = RegimeSnapshot(\n                timestamp=ts, day_type=day_type, dte=dte,\n                event_day=self._event_day, event_name=self._event_name,\n                defined_risk_only=self._event_day,\n                volatility_regime=VolatilityRegime.ABORT.value,\n                price_regime=PriceRegime.OBSERVING.value,\n                price_regime_15=PriceRegime.OBSERVING.value,\n                price_regime_60=PriceRegime.OBSERVING.value,\n                mtf_aligned=False,\n                positioning_regime=PositioningRegime.UNCLEAR.value,\n                final_regime=FinalRegime.EMERGENCY_EXIT.value,',
'        if self._check_straddle_explosion(straddle):\n            snap = RegimeSnapshot(\n                timestamp=ts, day_type=day_type, dte=dte,\n                event_day=self._event_day, event_name=self._event_name,\n                defined_risk_only=self._event_day,\n                volatility_regime=VolatilityRegime.ABORT.value,\n                price_regime=PriceRegime.OBSERVING.value,\n                price_regime_15=PriceRegime.OBSERVING.value,\n                price_regime_60=PriceRegime.OBSERVING.value,\n                mtf_aligned=False,\n                positioning_regime=PositioningRegime.UNCLEAR.value,\n                final_regime=FinalRegime.NO_TRADE.value,',
"RE-P1b: straddle explosion returns NO_TRADE not EMERGENCY_EXIT")

src = rb(src,
'        vix_roc_emg = self.config.vix_roc_emergency_pct\n        if len(vix_df) >= 200:\n            vals = vix_df.sort_values(["date", "time"])["vix_value"].values\n            rocs = []\n            w = 6\n            for i in range(w, len(vals)):\n                if vals[i - w] > 0:\n                    rocs.append((vals[i] - vals[i - w]) / vals[i - w] * 100)\n            if rocs:\n                vix_roc_emg = float(np.percentile(rocs, 95))\n                self.logger.info(f"  VIX ROC emergency (p95): {vix_roc_emg:.2f}%")',
'        vix_roc_emg = 15.0\n        self.logger.info(f"  VIX ROC emergency: {vix_roc_emg:.1f}% (absolute threshold — VIX up 15pct in 30min)")',
"RE-P2: VIX ROC emergency absolute 15pct not self-calibrated from same-day ticks")

src = rb(src,
'        emg = self._t("vix_roc_emergency", "vix_roc_emergency_pct")\n        _vix_regime_now = self._engine_ref.market_engine.state.get("vix_regime", "NORMAL") if self._engine_ref else "NORMAL"\n        _emg_adjusted = emg * 1.5 if _vix_regime_now == "SUPPRESSED" else emg\n        if vix_roc >= _emg_adjusted:\n            details["trigger"] = "VIX_SPIKE"\n            return VolatilityRegime.ABORT, details',
'        _prev_vix_close = 0.0\n        try:\n            _prev_row = self.db.query_one(\n                "SELECT vix_close FROM daily_summary WHERE trading_date < ? "\n                "AND vix_close IS NOT NULL AND vix_close > 0 "\n                "ORDER BY trading_date DESC LIMIT 1",\n                (str(datetime.now().date()),)\n            )\n            if _prev_row and _prev_row.get("vix_close"):\n                _prev_vix_close = float(_prev_row["vix_close"])\n        except Exception:\n            pass\n        _vix_pct_from_close = ((vix - _prev_vix_close) / _prev_vix_close * 100.0) if _prev_vix_close > 0 else 0.0\n        if _vix_pct_from_close >= 15.0 and vix >= 14.0:\n            details["trigger"] = "VIX_SPIKE_REAL"\n            self.logger.warning(f"REAL VIX EMERGENCY: VIX up {_vix_pct_from_close:.1f}pct from prev close {_prev_vix_close:.2f} to {vix:.2f}")\n            return VolatilityRegime.ABORT, details',
"RE-P2b: VIX emergency = 15pct up from prev close AND VIX>=14 — spot-anchored not tick-noise")

src = rb(src,
'            4: getattr(_cal_state, "day_size_friday",    0.50) if _cal_state else 0.50,',
'            4: getattr(_cal_state, "day_size_friday",    0.65) if _cal_state else 0.65,',
"RE-P8: Friday day_size 0.50->0.65 same as Thursday (intraday-only no weekend risk)")

src = rb(src,
'        if now_ist().weekday() == 4:\n            raw_size = min(raw_size, 0.50)',
'        if now_ist().weekday() == 4:\n            raw_size = min(raw_size, 0.65)',
"RE-P8b: Friday raw_size cap 0.50->0.65 same as Thursday")

src = rb(src,
'            "day_size_friday": 0.45,',
'            "day_size_friday": 0.65,',
"RE-P8c: AutoCalibrator Friday default 0.45->0.65")

src = rb(src,
'            base_sizes = {1: 0.75, 2: 0.55, 3: 0.65, 4: 0.65, 5: 0.45}',
'            base_sizes = {1: 0.75, 2: 0.55, 3: 0.65, 4: 0.65, 5: 0.65}',
"RE-P8d: AutoCalibrator Friday base_size 0.45->0.65")

write(re_path, src)
check_syntax(re_path)

print("\n[2/6] execution_engine.py")
src = read(ee_path)

src = rb(src,
'        if final_regime == "EMERGENCY_EXIT":\n            open_positions = self._get_open_positions()\n            if open_positions:\n                self.logger.warning(\n                    f"REGIME ENGINE EMERGENCY_EXIT — force-closing "\n                    f"{len(open_positions)} position(s)"\n                )\n                self.close_all_positions("EMERGENCY_EXIT")\n            return',
'        if final_regime == "EMERGENCY_EXIT":\n            self.logger.info("REGIME EMERGENCY_EXIT — blocking new entries only, positions managed by own rules")\n            return',
"EE-P1: EMERGENCY_EXIT blocks new entries only never force-closes positions")

src = rb(src,
'        stt      = sell_pts * self.config.stt_options_sell\n        exchange = turnover * self.config.exchange_txn_rate\n        sebi     = turnover * self.config.sebi_rate\n        stamp    = buy_pts  * self.config.stamp_duty_buy_options',
'        if action == "EXIT":\n            stt = buy_pts * self.config.stt_options_sell\n        else:\n            stt = sell_pts * self.config.stt_options_sell\n        exchange = turnover * self.config.exchange_txn_rate\n        sebi     = turnover * self.config.sebi_rate\n        stamp    = buy_pts  * self.config.stamp_duty_buy_options',
"EE-P5: STT on exit applies to buy_pts (long wings sold) not sell_pts (shorts bought back)")

src = rb(src,
'        vwap_dist = signals.get("vwap_dist_pct")\n        if vwap_dist is not None and current_time < dtime(14, 30):\n            if strategy_name == "BULL_PUT_SPREAD" and vwap_dist < -0.30:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}\n            if strategy_name == "BEAR_CALL_SPREAD" and vwap_dist > 0.30:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}\n            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY"):\n                if vwap_dist > 0.25:\n                    return "CLOSE_CALL_SIDE", {"vwap_dist": vwap_dist}\n                if vwap_dist < -0.25:\n                    return "CLOSE_PUT_SIDE", {"vwap_dist": vwap_dist}',
'        vwap_dist = signals.get("vwap_dist_pct")\n        if vwap_dist is not None and current_time < dtime(14, 30):\n            if strategy_name == "BULL_PUT_SPREAD" and vwap_dist < -0.40:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}\n            if strategy_name == "BEAR_CALL_SPREAD" and vwap_dist > 0.40:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}',
"EE-P12: remove VWAP side-close for IC/IB (normal NIFTY excursion triggers it), widen spread exits to 0.40pct")

src = rb(src,
'        if strategy_type == "SELL" and current_time >= dtime(14, 30):\n            cheap_thresh = 5.00 if strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD") else 3.00',
'        if strategy_type == "SELL" and current_time >= dtime(13, 0):\n            cheap_thresh = 3.00 if strategy_name in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD") else 2.00',
"EE-P11: cheap buyback from 13:00 not 14:30 threshold 2pts IC 3pts spreads")

write(ee_path, src)
check_syntax(ee_path)

print("\n[3/6] strategy_engine.py")
src = read(se_path)

src = rb(src,
'MIN_CREDITS = {\n    "IRON_BUTTERFLY":  12,\n    "IRON_CONDOR":     10,\n    "BULL_PUT_SPREAD": 8,\n    "BEAR_CALL_SPREAD":8,\n    "POST_EVENT_STRADDLE": 20,\n}',
'MIN_CREDITS = {\n    "IRON_BUTTERFLY":  25,\n    "IRON_CONDOR":     22,\n    "BULL_PUT_SPREAD": 18,\n    "BEAR_CALL_SPREAD":18,\n    "POST_EVENT_STRADDLE": 25,\n}',
"SE-P6: min credits raised for DTE 0-1 NIFTY 2026 Δ0.20-0.25 shorts 150-200pt wing")

src = rb(src,
'MIN_CREDITS_TUESDAY = {\n    "IRON_BUTTERFLY":  15,\n    "IRON_CONDOR":     12,\n    "BULL_PUT_SPREAD": 10,\n    "BEAR_CALL_SPREAD":10,\n}',
'MIN_CREDITS_TUESDAY = {\n    "IRON_BUTTERFLY":  28,\n    "IRON_CONDOR":     25,\n    "BULL_PUT_SPREAD": 20,\n    "BEAR_CALL_SPREAD":20,\n}',
"SE-P6b: Tuesday 0DTE min credits higher for gamma risk management")

src = rb(src,
'            _vix_min_scale = 0.65 if (s.get("vix") or 15.0) < 12.0 else (0.75 if (s.get("vix") or 15.0) < 14.0 else 1.0)\n            min_credit = min_credit * _vix_min_scale',
'            _vix_min_scale = 1.0',
"SE-P6c: remove VIX-based discount on min credit — low VIX is when 4-leg costs dominate most")

src = rb(src,
'            vix_regime = s.get("vix_regime", "NORMAL")\n            _vix_val_rp = s.get("vix") or 15.0\n            if _vix_val_rp < 11.5:\n                min_rupee = 100\n            elif _vix_val_rp < 13.0:\n                min_rupee = 150\n            elif _vix_val_rp < 15.0:\n                min_rupee = 175\n            else:\n                min_rupee = {"SUPPRESSED": 200, "LOW": 250, "NORMAL": 350,\n                             "ELEVATED": 450, "HIGH": 550}.get(vix_regime, 200)\n            if net_profit_at_target * C02 < min_rupee:\n                return {"valid": False, "reason": f"projected_profit_below_Rs{min_rupee}"}',
'            _rtrip_costs_rs = (total_costs_pts + total_slippage) * 2.0 * C02\n            min_rupee = max(int(_rtrip_costs_rs * 3.0), 150)\n            if net_profit_at_target * C02 < min_rupee:\n                return {"valid": False, "reason": f"projected_profit_below_Rs{min_rupee}"}',
"SE-P7: rupee gate = 3x round-trip costs scales with lot size VIX and structure")

src = rb(src,
'LOT_CAPS_BY_DAY = {\n    "MONDAY": 3, "TUESDAY": 2, "WEDNESDAY": 2,\n    "THURSDAY": 2, "FRIDAY": 1,\n}',
'LOT_CAPS_BY_DAY = {\n    "MONDAY": 4, "TUESDAY": 3, "WEDNESDAY": 3,\n    "THURSDAY": 3, "FRIDAY": 3,\n}',
"SE-P8: Friday lot cap 1->3 same as Thursday (intraday-only no weekend risk)")

write(se_path, src)
check_syntax(se_path)

print("\n[4/6] market_data_engine.py")
src = read(mde_path)

src = rb(src,
'            coverage_ok = len(orb_bars) >= 45',
'            coverage_ok = len(orb_bars) >= 10',
"MDE-P3: ORB coverage 45->10 bars (15min window max 15 bars, 10 is sufficient for NIFTY)")

src = rb(src,
'            _tue_hard_exit = "14:30"',
'            _tue_hard_exit = "15:00"',
"MDE-P10: Tuesday 0DTE hard exit 14:30->15:00 captures maximum theta decay on expiry day")

write(mde_path, src)
check_syntax(mde_path)

print("\n[5/6] main.py")
src = read(me_path)

src = rb(src,
'        if current_time >= dtime(15, 0):\n            open_positions = self.execution_engine._get_open_positions()\n            if open_positions:\n                self.logger.info(\n                    f"HARD EXIT SWEEP @ 15:00 — "\n                    f"closing {len(open_positions)} position(s)"\n                )\n                self.execution_engine.close_all_positions("HARD_EXIT_15:00")',
'        if current_time >= dtime(15, 15):\n            open_positions = self.execution_engine._get_open_positions()\n            if open_positions:\n                self.logger.info(\n                    f"HARD EXIT SWEEP @ 15:15 — "\n                    f"closing {len(open_positions)} position(s)"\n                )\n                self.execution_engine.close_all_positions("HARD_EXIT_15:00")',
"ME-P9: hard exit sweep 15:00->15:15 captures 15 more minutes of theta decay")

src = rb(src,
'        entry_possible = (\n            current_time <= dtime(15, 0) and\n            not self.market_engine.state.get("daily_halted")\n        )',
'        entry_possible = (\n            current_time <= dtime(14, 30) and\n            not self.market_engine.state.get("daily_halted")\n        )',
"ME-P9b: last new entry at 14:30 need 45min hold before 15:15 hard exit")

write(me_path, src)
check_syntax(me_path)

print("\n[6/6] nifty_algo_core.py")
src = read(core_path)

src = rb(src,
'STT_RATE=0.000625\nSTT_OPTIONS_SELL=0.000625',
'STT_RATE=0.001\nSTT_OPTIONS_SELL=0.001',
"CORE-P4: STT rate 0.0625pct->0.1pct (NSE rate from Oct 2024)")

src = rb(src,
'        stt_rate=_get_float(env, "STT_RATE", 0.000625),\n        stt_options_sell=_get_float(env, "STT_OPTIONS_SELL", 0.000625),',
'        stt_rate=_get_float(env, "STT_RATE", 0.001),\n        stt_options_sell=_get_float(env, "STT_OPTIONS_SELL", 0.001),',
"CORE-P4b: STT default in load_config 0.000625->0.001")

write(core_path, src)
check_syntax(core_path)

print("\n" + "=" * 68)
print("PATCH 7 SUMMARY")
print("=" * 68)
print(f"Applied : {len(_applied)}")
for a in _applied:
    print(f"  [OK] {a}")
print(f"Skipped : {len(_skipped)}")
for s in _skipped:
    print(f"  [SKIP] {s}")
print(f"Errors  : {len(_errors)}")
for e in _errors:
    print(f"  [ERR] {e}")
print(f"Backups : {BACKUP_DIR}")

if _errors:
    print("\n[FAIL]")
    sys.exit(1)
else:
    print("\n[SUCCESS] Patch 7 complete.")
    sys.exit(0)