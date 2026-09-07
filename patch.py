# patch6.py
from __future__ import annotations
import ast
import shutil
import sys
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"_patch6_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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
print("PATCH 6 - NIFTY OPTIONS SIGNAL QUALITY FIXES")
print(f"Base: {BASE}")
print("=" * 68)

mde_path = BASE / "market_data_engine.py"
eod_path = BASE / "eod_report.py"

print("\n[1/2] market_data_engine.py")
src = read(mde_path)

src = rb(src,
'        if spot is not None and spot > 0:\n            band = spot * 0.03\n            total_put = sum(\n                legs.get("put", {}).get("oi", 0) or 0\n                for strike, legs in chain.items()\n                if (spot - band) <= strike < spot\n            )\n            total_call = sum(\n                legs.get("call", {}).get("oi", 0) or 0\n                for strike, legs in chain.items()\n                if spot < strike <= (spot + band)\n            )',
'        if spot is not None and spot > 0:\n            band = spot * 0.05\n            total_put = sum(\n                legs.get("put", {}).get("oi", 0) or 0\n                for strike, legs in chain.items()\n                if (spot - band) <= strike < spot\n            )\n            total_call = sum(\n                legs.get("call", {}).get("oi", 0) or 0\n                for strike, legs in chain.items()\n                if spot < strike <= (spot + band)\n            )',
"FIX-1: PCR band 3pct to 5pct for NIFTY meaningful OI range")

src = rb(src,
'    def compute_atm_iv(\n        self, chain: dict, spot: Optional[float]\n    ) -> Optional[float]:\n        if not chain or spot is None:\n            return None\n        step = self.config.nifty_strike_step\n        atm = round(spot / step) * step\n        if atm not in chain:\n            atm = min(chain.keys(), key=lambda k: abs(k - spot))\n        leg = chain.get(atm, {})\n        call, put = leg.get("call", {}), leg.get("put", {})\n        call_iv = call.get("iv", 0.0) or 0.0\n        put_iv = put.get("iv", 0.0) or 0.0\n        call_oi = call.get("oi", 0) or 0\n        put_oi = put.get("oi", 0) or 0\n\n        if call_iv <= 0 and put_iv <= 0:\n            return None\n\n        total_oi = call_oi + put_oi\n        if total_oi > 0:\n            atm_iv = (call_iv * call_oi + put_iv * put_oi) / total_oi\n        elif call_iv > 0 and put_iv > 0:\n            atm_iv = (call_iv + put_iv) / 2.0\n        elif call_iv > 0:\n            atm_iv = call_iv\n        else:\n            atm_iv = put_iv\n\n        if atm_iv < 0.05 or atm_iv > 0.80:\n            return None\n        try:\n            _vix_state = self.state.get("prev_vix")\n            if _vix_state and _vix_state > 0:\n                vix_decimal = _vix_state / 100.0\n                if atm_iv < vix_decimal * 0.60 or atm_iv > vix_decimal * 2.0:\n                    self.logger.warning(\n                        f"ATM IV {atm_iv*100:.2f}% vs VIX {_vix_state:.2f} — "\n                        f"ratio {atm_iv/vix_decimal:.2f} outside 0.60-2.00 range. "\n                        f"Chain data may be stale. Treating ATM IV as unavailable."\n                    )\n                    return None\n        except Exception:\n            pass\n        return atm_iv',
'    def compute_atm_iv(\n        self, chain: dict, spot: Optional[float]\n    ) -> Optional[float]:\n        if not chain or spot is None:\n            return None\n        step = self.config.nifty_strike_step\n        atm = round(spot / step) * step\n        if atm not in chain:\n            atm = min(chain.keys(), key=lambda k: abs(k - spot))\n        iv_samples = []\n        for s_strike in [atm - step, atm, atm + step]:\n            leg = chain.get(s_strike, {})\n            if not leg:\n                continue\n            c_leg = leg.get("call", {})\n            p_leg = leg.get("put", {})\n            c_iv = c_leg.get("iv", 0.0) or 0.0\n            p_iv = p_leg.get("iv", 0.0) or 0.0\n            c_oi = c_leg.get("oi", 0) or 0\n            p_oi = p_leg.get("oi", 0) or 0\n            if c_iv <= 0 and p_iv <= 0:\n                continue\n            t_oi = c_oi + p_oi\n            if t_oi > 0:\n                s_iv = (c_iv * c_oi + p_iv * p_oi) / t_oi\n            elif c_iv > 0 and p_iv > 0:\n                s_iv = (c_iv + p_iv) / 2.0\n            elif c_iv > 0:\n                s_iv = c_iv\n            else:\n                s_iv = p_iv\n            if 0.05 <= s_iv <= 0.80:\n                w = 2.0 if s_strike == atm else 1.0\n                iv_samples.append((s_iv, w))\n        if not iv_samples:\n            return None\n        total_w = sum(w for _, w in iv_samples)\n        atm_iv = sum(iv * w for iv, w in iv_samples) / total_w\n        if atm_iv < 0.05 or atm_iv > 0.80:\n            return None\n        try:\n            _vix_state = self.state.get("prev_vix")\n            if _vix_state and _vix_state > 0:\n                vix_decimal = _vix_state / 100.0\n                if atm_iv < vix_decimal * 0.60 or atm_iv > vix_decimal * 2.0:\n                    self.logger.warning(\n                        f"ATM IV {atm_iv*100:.2f}% vs VIX {_vix_state:.2f} — "\n                        f"ratio {atm_iv/vix_decimal:.2f} outside 0.60-2.00 range. "\n                        f"Chain data may be stale. Treating ATM IV as unavailable."\n                    )\n                    return None\n        except Exception:\n            pass\n        return atm_iv',
"FIX-2: ATM IV weighted average ATM-50 ATM ATM+50 reduces single-strike quote noise")

src = rb(src,
'    def compute_oi_change(\n        self, atm_strike: int, expiry_str: str,\n        current_ce_oi: int, current_pe_oi: int\n    ) -> float:\n        current_total = current_ce_oi + current_pe_oi\n        if current_total <= 0:\n            return 0.0\n        lookback = self.config.oi_change_lookback_min\n        cutoff = (now_ist() - timedelta(minutes=lookback + 5)).isoformat()\n        limit_ts = (now_ist() - timedelta(minutes=lookback)).isoformat()\n        row = self.db.query_one(\n            "SELECT ce_oi, pe_oi FROM options_chain "\n            "WHERE strike=? AND expiry_date=? AND timestamp>=? AND timestamp<=? "\n            "ORDER BY timestamp ASC LIMIT 1",\n            (atm_strike, expiry_str, cutoff, limit_ts),\n        )\n        if row:\n            prior = (row.get("ce_oi") or 0) + (row.get("pe_oi") or 0)\n            if prior > 0:\n                return (current_total - prior) / prior\n        return 0.0',
'    def compute_oi_change(\n        self, atm_strike: int, expiry_str: str,\n        current_ce_oi: int, current_pe_oi: int\n    ) -> float:\n        current_total = current_ce_oi + current_pe_oi\n        if current_total <= 0:\n            return 0.0\n        lookback = self.config.oi_change_lookback_min\n        today_str = today_ist().isoformat()\n        cutoff_ts = (now_ist() - timedelta(minutes=lookback + 5)).isoformat()\n        limit_ts = (now_ist() - timedelta(minutes=lookback)).isoformat()\n        row = self.db.query_one(\n            "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "\n            "WHERE trading_date=? AND strike=? AND expiry=? "\n            "AND capture_time >= ? AND capture_time <= ? "\n            "LIMIT 1",\n            (today_str, atm_strike, expiry_str, cutoff_ts, limit_ts),\n        )\n        if row and row.get("total_oi"):\n            prior = row["total_oi"]\n            if prior > 0:\n                return (current_total - prior) / prior\n        row2 = self.db.query_one(\n            "SELECT ce_oi, pe_oi FROM options_chain "\n            "WHERE strike=? AND expiry_date=? AND timestamp>=? AND timestamp<=? "\n            "ORDER BY timestamp ASC LIMIT 1",\n            (atm_strike, expiry_str, cutoff_ts, limit_ts),\n        )\n        if row2:\n            prior2 = (row2.get("ce_oi") or 0) + (row2.get("pe_oi") or 0)\n            if prior2 > 0:\n                return (current_total - prior2) / prior2\n        row3 = self.db.query_one(\n            "SELECT SUM(oi) as total_oi FROM option_chain_snapshot "\n            "WHERE trading_date=? AND strike=? AND expiry=? "\n            "ORDER BY capture_time ASC LIMIT 1",\n            (today_str, atm_strike, expiry_str),\n        )\n        if row3 and row3.get("total_oi"):\n            prior3 = row3["total_oi"]\n            if prior3 > 0 and prior3 != current_total:\n                return (current_total - prior3) / prior3\n        return 0.0',
"FIX-3: OI change uses option_chain_snapshot every cycle with fallback to options_chain")

src = rb(src,
'        if skew < -1.5:\n            return otm_ce_iv, otm_pe_iv, 0.0\n        return otm_ce_iv, otm_pe_iv, skew',
'        if skew < -1.5:\n            self.logger.debug(f"OTM skew={skew:.2f} negative calls>puts treating as neutral")\n            return otm_ce_iv, otm_pe_iv, 0.0\n        return otm_ce_iv, otm_pe_iv, skew',
"FIX-4a: negative OTM skew debug log only not warning")

src = rb(src,
'        if skew is not None and skew < -1.5:\n            _now_t = now_ist().time()\n            from datetime import time as _dtime\n            if _now_t >= _dtime(10, 0):\n                self.logger.warning(\n                    f"OTM skew={skew:.2f} is negative beyond -1.5 — "\n                    f"NIFTY structural skew violation. Treating skew as UNKNOWN."\n                )\n            skew = None\n            skew_ratio = None',
'        if skew is not None and skew < -1.5:\n            self.logger.debug(f"OTM skew={skew:.2f} negative treating as neutral 0.0")\n            skew = 0.0',
"FIX-4b: negative skew in run_cycle set to 0.0 neutral eliminates UNKNOWN volatility cycles")

write(mde_path, src)
ok1 = check_syntax(mde_path)
if ok1:
    print("  market_data_engine.py syntax OK")

print("\n[2/2] eod_report.py")
src = read(eod_path)

src = rb(src,
'    volumes = [c["volume"] for c in candles if c.get("volume")]',
'    volumes = [c["volume"] for c in candles if c.get("volume") is not None]',
"FIX-5a: zero_volume_bars use is not None to include zero values in list")

src = rb(src,
'        "zero_volume_bars":  sum(1 for v in volumes if v == 0),',
'        "zero_volume_bars":  sum(1 for v in volumes if (v or 0) == 0),\n        "volume_note":       "NSE index volume always 0 via Upstox API confirmed",',
"FIX-5b: add volume_note to candle stats for LLM context")

write(eod_path, src)
ok2 = check_syntax(eod_path)
if ok2:
    print("  eod_report.py syntax OK")

print("\n" + "=" * 68)
print("PATCH 6 SUMMARY")
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
    print("\n[SUCCESS] Patch 6 complete.")
    sys.exit(0)