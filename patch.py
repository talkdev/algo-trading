import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def patch_market_data_engine():
    path = BASE_DIR / "market_data_engine.py"
    content = read_file(path)

    old = '            "open_positions": 0, "daily_pnl_net": s.get("daily_pnl", 0.0),\n            total_pnl_for_cycle_log = True,\n            "vix_regime": s["vix_regime"], "day_mode": s["day_mode"],'
    new = '            "open_positions": 0, "daily_pnl_net": s.get("daily_pnl", 0.0),\n            "vix_regime": s["vix_regime"], "day_mode": s["day_mode"],'
    content = content.replace(old, new)

    old2 = '        if rv < 0.02 or rv > 0.80:\n            return None\n        return rv'
    new2 = '        if rv < 0.03 or rv > 0.60:\n            return None\n        return rv'
    content = content.replace(old2, new2)

    old3 = '        if atm_iv < 0.05 or atm_iv > 0.80:\n            self.logger.warning(f"ATM IV {atm_iv:.4f} outside valid range [0.05,0.80] — discarding")\n            return None'
    new3 = '        if atm_iv < 0.04 or atm_iv > 0.70:\n            self.logger.warning(f"ATM IV {atm_iv:.4f} outside valid range [0.04,0.70] — discarding")\n            return None'
    content = content.replace(old3, new3)

    old4 = '        if pcr < 0.2 or pcr > 6.0:\n            self.logger.warning(f"PCR {pcr:.3f} outside valid range [0.2,6.0] — discarding")\n            return None'
    new4 = '        if pcr < 0.3 or pcr > 4.0:\n            self.logger.warning(f"PCR {pcr:.3f} outside valid range [0.3,4.0] — discarding")\n            return None'
    content = content.replace(old4, new4)

    old5 = '        if skew < 0.5 or skew > 3.0:\n            return None'
    new5 = '        if skew < 0.6 or skew > 2.5:\n            return None'
    content = content.replace(old5, new5)

    old6 = '        if not (10000 < spot < 50000):'
    new6 = '        if not (20000 < spot < 35000):'
    content = content.replace(old6, new6)

    old7 = '        if vix is not None and not (5.0 < vix < 90.0):'
    new7 = '        if vix is not None and not (8.0 < vix < 60.0):'
    content = content.replace(old7, new7)

    old8 = '        annual_variance = variance_per_bar * (75.0 * 252.0)'
    new8 = '        annual_variance = variance_per_bar * (78.0 * 252.0)'
    if old8 in content:
        content = content.replace(old8, new8)

    old9 = '        if len(valid_bars) < 30:\n            return self._parkinson_fallback(vix), "vix_proxy_insufficient_bars"'
    new9 = '        if len(valid_bars) < 20:\n            return self._parkinson_fallback(vix), "vix_proxy_insufficient_bars"'
    content = content.replace(old9, new9)

    old10 = '        if n < 10:\n            return self._parkinson_fallback(vix), "vix_proxy_too_few_valid"'
    new10 = '        if n < 8:\n            return self._parkinson_fallback(vix), "vix_proxy_too_few_valid"'
    content = content.replace(old10, new10)

    old11 = '        if rv < 0.03 or rv > 0.50:\n            return self._parkinson_fallback(vix), "vix_proxy_out_of_range"'
    new11 = '        if rv < 0.04 or rv > 0.45:\n            return self._parkinson_fallback(vix), "vix_proxy_out_of_range"'
    content = content.replace(old11, new11)

    old12 = '        if vix and vix > 0:\n            return (vix / 100.0) * 0.65\n        return 0.065'
    new12 = '        if vix and vix > 0:\n            return (vix / 100.0) * 0.70\n        return 0.085'
    content = content.replace(old12, new12)

    old13 = '        if len(candles_today) < 3:\n            return None, False'
    new13 = '        if len(candles_today) < 4:\n            return None, False'
    content = content.replace(old13, new13)

    old14 = '        if _or_width_pct < 0.18: or_condition, or_score = "VERY_NARROW", 2\n        elif _or_width_pct < 0.32: or_condition, or_score = "NARROW", 1\n        elif _or_width_pct < 0.50: or_condition, or_score = "MODERATE", 0\n        elif _or_width_pct < 0.70: or_condition, or_score = "WIDE", -1\n        else: or_condition, or_score = "VERY_WIDE", -2'
    new14 = '        if _or_width_pct < 0.20: or_condition, or_score = "VERY_NARROW", 2\n        elif _or_width_pct < 0.35: or_condition, or_score = "NARROW", 1\n        elif _or_width_pct < 0.55: or_condition, or_score = "MODERATE", 0\n        elif _or_width_pct < 0.75: or_condition, or_score = "WIDE", -1\n        else: or_condition, or_score = "VERY_WIDE", -2'
    content = content.replace(old14, new14)

    old15 = '        if adx_value < 22: return "FLAT", 2\n        if adx_value < 28: return "WEAK", 1\n        if adx_value < 35: return "MODERATE", 0\n        if adx_value < 42: return "STRONG", -1\n        return "VERY_STRONG", -2'
    new15 = '        if adx_value < 20: return "FLAT", 2\n        if adx_value < 25: return "WEAK", 1\n        if adx_value < 32: return "MODERATE", 0\n        if adx_value < 40: return "STRONG", -1\n        return "VERY_STRONG", -2'
    content = content.replace(old15, new15)

    old16 = '        if vwap_dist_pct > 0.50: vwap_signal, vwap_score = "BULLISH_EXTENDED", 1\n        elif vwap_dist_pct > 0.15: vwap_signal, vwap_score = "BULLISH", 1\n        elif vwap_dist_pct > -0.15: vwap_signal, vwap_score = "NEUTRAL", 0\n        elif vwap_dist_pct > -0.50: vwap_signal, vwap_score = "BEARISH", -1\n        else: vwap_signal, vwap_score = "BEARISH_EXTENDED", -1'
    new16 = '        if vwap_dist_pct > 0.40: vwap_signal, vwap_score = "BULLISH_EXTENDED", 1\n        elif vwap_dist_pct > 0.12: vwap_signal, vwap_score = "BULLISH", 1\n        elif vwap_dist_pct > -0.12: vwap_signal, vwap_score = "NEUTRAL", 0\n        elif vwap_dist_pct > -0.40: vwap_signal, vwap_score = "BEARISH", -1\n        else: vwap_signal, vwap_score = "BEARISH_EXTENDED", -1'
    content = content.replace(old16, new16)

    old17 = '        if iv_change_pct < -15.0: iv_behavior, iv_mod = "CRUSHING", 0.3\n            elif iv_change_pct < -5.0: iv_behavior, iv_mod = "DECLINING", 0.1\n            elif iv_change_pct <= 5.0: iv_behavior, iv_mod = "STABLE", 0.0\n            elif iv_change_pct <= 15.0:'
    new17 = '        if iv_change_pct < -12.0: iv_behavior, iv_mod = "CRUSHING", 0.3\n            elif iv_change_pct < -4.0: iv_behavior, iv_mod = "DECLINING", 0.1\n            elif iv_change_pct <= 4.0: iv_behavior, iv_mod = "STABLE", 0.0\n            elif iv_change_pct <= 12.0:'
    content = content.replace(old17, new17)

    write_file(path, content)
    print("Patched: market_data_engine.py")


def patch_execution_engine():
    path = BASE_DIR / "execution_engine.py"
    content = read_file(path)

    old = '''        for leg in legs:
            price = leg.get("exit_price") if leg.get("exit_price") is not None else leg.get("entry_price", 0)
            price = price or 0
            qty = lots * C02
            if price <= 0:
                continue
            premium_value = price * qty
            if leg["action"] == "SELL":
                total_sell_premium += premium_value
            else:
                total_buy_premium += premium_value'''
    new = '''        for leg in legs:
            if action == "ENTRY":
                price = leg.get("entry_price", 0) or 0
            else:
                price = leg.get("exit_price", 0) or 0
            if price <= 0:
                price = leg.get("entry_price", 0) or 0
            qty = lots * C02
            if price <= 0:
                continue
            premium_value = price * qty
            if leg["action"] == "SELL":
                total_sell_premium += premium_value
            else:
                total_buy_premium += premium_value'''
    content = content.replace(old, new)

    old2 = '            "stt": round(stt, 2), "exchange": round(exchange, 2), "ipft": round(ipft, 6),\n                "sebi": round(sebi, 4), "stamp": round(stamp, 4), "brokerage": round(brokerage, 2),\n                "gst": round(gst, 2), "total": round(total_costs, 2),'
    new2 = '            "stt": round(stt, 2), "exchange": round(exchange, 2),\n                "sebi": round(sebi, 4), "stamp": round(stamp, 4), "brokerage": round(brokerage, 2),\n                "gst": round(gst, 2), "total": round(total_costs, 2),'
    content = content.replace(old2, new2)

    old3 = '        if state["consecutive_stops"] >= 3:\n                state["daily_halted"] = True\n                self.logger.warning("3 consecutive stops — halting trading for the day")'
    new3 = '        if state["consecutive_stops"] >= 2:\n                state["daily_halted"] = True\n                self.logger.warning("2 consecutive stops — halting trading for the day")'
    content = content.replace(old3, new3)

    old4 = '        if current_delta > 0.45:\n                        return "CLOSE_STOP", {"reason_detail": f"short_leg_delta_breach_{current_delta:.3f}"}\n                    if current_delta > 0.38 and not position.get("stop_tightened_for_delta"):'
    new4 = '        if current_delta > 0.40:\n                        return "CLOSE_STOP", {"reason_detail": f"short_leg_delta_breach_{current_delta:.3f}"}\n                    if current_delta > 0.32 and not position.get("stop_tightened_for_delta"):'
    content = content.replace(old4, new4)

    old5 = '        if (adx_value is not None and adx_value > 35 and strategy_type == "SELL"\n                and strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")):'
    new5 = '        if (adx_value is not None and adx_value > 30 and strategy_type == "SELL"\n                and strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")):'
    content = content.replace(old5, new5)

    old6 = '        if vwap_dist is not None and _vwap_exits_active:\n            if strategy_name == "BULL_PUT_SPREAD" and vwap_dist < -0.20:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}\n            if strategy_name == "BEAR_CALL_SPREAD" and vwap_dist > 0.20:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}\n            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY"):\n                if vwap_dist > 0.30:\n                    return "CLOSE_CALL_SIDE", {"vwap_dist": vwap_dist}\n                if vwap_dist < -0.30:\n                    return "CLOSE_PUT_SIDE", {"vwap_dist": vwap_dist}'
    new6 = '        if vwap_dist is not None and _vwap_exits_active:\n            if strategy_name == "BULL_PUT_SPREAD" and vwap_dist < -0.15:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}\n            if strategy_name == "BEAR_CALL_SPREAD" and vwap_dist > 0.15:\n                return "CLOSE_VWAP", {"vwap_dist": vwap_dist}\n            if strategy_name in ("IRON_CONDOR", "IRON_BUTTERFLY"):\n                if vwap_dist > 0.25:\n                    return "CLOSE_CALL_SIDE", {"vwap_dist": vwap_dist}\n                if vwap_dist < -0.25:\n                    return "CLOSE_PUT_SIDE", {"vwap_dist": vwap_dist}'
    content = content.replace(old6, new6)

    old7 = '        _cheap_threshold = 2.00'
    new7 = '        _cheap_threshold = 3.00'
    content = content.replace(old7, new7)

    write_file(path, content)
    print("Patched: execution_engine.py")


def patch_strategy_engine():
    path = BASE_DIR / "strategy_engine.py"
    content = read_file(path)

    old = '''    MIN_CREDITS = {
    "IRON_BUTTERFLY": 20, "IRON_CONDOR": 18,
    "BULL_PUT_SPREAD": 15, "BEAR_CALL_SPREAD": 14, "POST_EVENT_STRADDLE": 25,
}
MIN_CREDITS_TUESDAY = {
    "IRON_BUTTERFLY": 25, "IRON_CONDOR": 18, "BULL_PUT_SPREAD": 16, "BEAR_CALL_SPREAD": 15,
}'''
    new = '''    MIN_CREDITS = {
    "IRON_BUTTERFLY": 25, "IRON_CONDOR": 20,
    "BULL_PUT_SPREAD": 18, "BEAR_CALL_SPREAD": 16, "POST_EVENT_STRADDLE": 30,
}
MIN_CREDITS_TUESDAY = {
    "IRON_BUTTERFLY": 30, "IRON_CONDOR": 22, "BULL_PUT_SPREAD": 20, "BEAR_CALL_SPREAD": 18,
}'''
    content = content.replace(old, new)

    old2 = 'MIN_CREDITS = {\n    "IRON_BUTTERFLY": 20, "IRON_CONDOR": 18,\n    "BULL_PUT_SPREAD": 15, "BEAR_CALL_SPREAD": 14, "POST_EVENT_STRADDLE": 25,\n}\nMIN_CREDITS_TUESDAY = {\n    "IRON_BUTTERFLY": 25, "IRON_CONDOR": 18, "BULL_PUT_SPREAD": 16, "BEAR_CALL_SPREAD": 15,\n}'
    new2 = 'MIN_CREDITS = {\n    "IRON_BUTTERFLY": 25, "IRON_CONDOR": 20,\n    "BULL_PUT_SPREAD": 18, "BEAR_CALL_SPREAD": 16, "POST_EVENT_STRADDLE": 30,\n}\nMIN_CREDITS_TUESDAY = {\n    "IRON_BUTTERFLY": 30, "IRON_CONDOR": 22, "BULL_PUT_SPREAD": 20, "BEAR_CALL_SPREAD": 18,\n}'