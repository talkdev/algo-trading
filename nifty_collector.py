#!/usr/bin/env python3
"""
NIFTY Backtest Data Collector — Upstox v2/v3
================================================
A streamlined data collector designed specifically to capture
the Spot, VIX, LTP, and Option Greeks required to backtest
upstox_nifty_short_straddle.py.
"""

import os
import sys
import time
import csv
import requests
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================
# Reads from the same env.txt format as your straddle script
ENV_FILE_PATH = os.environ.get("UPSTOX_ENV_FILE", "env.txt")

# ============================================================================
# CONFIGURATION
# ============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE_PATH = os.environ.get("UPSTOX_ENV_FILE", os.path.join(SCRIPT_DIR, "env.txt"))

def load_token(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                # Split at the first '=' to handle spaces on either side
                if "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() == "UPSTOX_ACCESS_TOKEN":
                        return val.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return os.environ.get("UPSTOX_ACCESS_TOKEN", "")

ACCESS_TOKEN = load_token(ENV_FILE_PATH)
if not ACCESS_TOKEN:
    print(f"ERROR: UPSTOX_ACCESS_TOKEN is missing. Checked path: {ENV_FILE_PATH}")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json",
}

STRIKES_EACH_SIDE = 10
POLL_INTERVAL_SEC = 10
CSV_FILE = "nifty_short_straddle_backtest_data.csv"

# Endpoints matching upstox_nifty_short_straddle.py
URL_LTP = "https://api.upstox.com/v2/market-quote/ltp"
URL_CONTRACTS = "https://api.upstox.com/v2/option/contract"
URL_GREEKS = "https://api.upstox.com/v3/market-quote/option-greek"

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
VIX_INDEX_KEY = "NSE_INDEX|India VIX"

CSV_COLUMNS = [
    "timestamp", "expiry", "spot", "vix", 
    "instrument_key", "strike", "side", "ltp", "delta", "theta", "gamma", "vega"
]

def get_current_expiry() -> str:
    """Find the nearest NIFTY weekly expiry date."""
    res = requests.get(URL_CONTRACTS, params={"instrument_key": NIFTY_INDEX_KEY}, headers=HEADERS)
    res.raise_for_status()
    contracts = res.json().get("data", [])
    today = datetime.now().date().isoformat()
    
    expiries = sorted({c["expiry"] for c in contracts if c.get("expiry") and c["expiry"] >= today})
    if not expiries:
        raise RuntimeError("No valid NIFTY expiries found.")
    return expiries[0]

def main():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="") as f:
            csv.writer(f).writerow(CSV_COLUMNS)

    print(f"Starting collection... Logging to {CSV_FILE}")
    
    try:
        expiry = get_current_expiry()
        print(f"Tracking Expiry: {expiry}")
    except Exception as e:
        print(f"Failed to fetch expiry: {e}")
        sys.exit(1)

    while True:
        try:
            timestamp = datetime.now().isoformat(timespec="seconds")
            
            # 1. Fetch Spot and VIX
            res_ltp = requests.get(URL_LTP, params={"instrument_key": f"{NIFTY_INDEX_KEY},{VIX_INDEX_KEY}"}, headers=HEADERS)
            res_ltp.raise_for_status()
            data_ltp = res_ltp.json().get("data", {})
            
            spot = data_ltp.get(NIFTY_INDEX_KEY.replace("|", ":"), {}).get("last_price")
            vix = data_ltp.get(VIX_INDEX_KEY.replace("|", ":"), {}).get("last_price")
            
            if not spot or not vix:
                print(f"[{timestamp}] Missing Spot or VIX. Retrying...")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            atm_strike = round(spot / 50) * 50
            
            # 2. Reconstruct expected Option Instrument Keys based on ATM
            # Format for NSE F&O is typically mapped, but the safest way is fetching the chain.
            # Using v2 chain to reliably grab the active instrument keys.
            res_chain = requests.get("https://api.upstox.com/v2/option/chain", 
                                     params={"instrument_key": NIFTY_INDEX_KEY, "expiry_date": expiry}, 
                                     headers=HEADERS)
            res_chain.raise_for_status()
            chain_data = res_chain.json().get("data", [])
            
            # Filter for ATM +/- strikes
            target_strikes = set(range(int(atm_strike) - (50 * STRIKES_EACH_SIDE), int(atm_strike) + (50 * (STRIKES_EACH_SIDE + 1)), 50))
            
            option_keys = []
            key_metadata = {}
            for row in chain_data:
                strike = int(row.get("strike_price", 0))
                if strike in target_strikes:
                    for side_key, side_str in [("call_options", "CE"), ("put_options", "PE")]:
                        opt = row.get(side_key, {})
                        inst_key = opt.get("instrument_key")
                        if inst_key:
                            option_keys.append(inst_key)
                            key_metadata[inst_key] = {"strike": strike, "side": side_str}

            # 3. Fetch v3 Greeks + LTP
            # Chunking keys as Upstox may limit URL length
            greeks_data = {}
            for i in range(0, len(option_keys), 50):
                chunk = option_keys[i:i+50]
                res_greeks = requests.get(URL_GREEKS, params={"instrument_key": ",".join(chunk)}, headers=HEADERS)
                if res_greeks.status_code == 200:
                    greeks_data.update(res_greeks.json().get("data", {}))

            # 4. Log to CSV
            rows_to_write = []
            for inst_key, meta in key_metadata.items():
                g_info = greeks_data.get(inst_key.replace("|", ":"), {})
                greeks = g_info.get("option_greeks", {})
                
                rows_to_write.append([
                    timestamp, expiry, spot, vix, inst_key, meta["strike"], meta["side"],
                    g_info.get("last_price", ""),
                    greeks.get("delta", ""),
                    greeks.get("theta", ""),
                    greeks.get("gamma", ""),
                    greeks.get("vega", "")
                ])

            with open(CSV_FILE, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerows(rows_to_write)
            
            print(f"[{timestamp}] Captured Spot: {spot}, VIX: {vix}. Logged {len(rows_to_write)} option legs.")

        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Error during collection loop: {e}")
        
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()