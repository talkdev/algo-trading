"""Compare signals at red-day loser entries vs green-day winner entries."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PER = Path(__file__).resolve().parents[1] / "data" / "per_day"

# From user's latest natural BT blotter
RED = {
    "2026-09-18": [("10:47", "BULL_PUT_SPREAD", -579)],
    "2026-09-28": [("10:19", "LONG_PUT", -366)],
    "2026-10-16": [("11:00", "LONG_CALL", -4927)],
    "2026-10-22": [("12:12", "BEAR_CALL_SPREAD", -384)],
    "2026-10-28": [("10:07", "BEAR_CALL_SPREAD", -1848)],
    "2026-10-29": [("13:06", "BEAR_CALL_SPREAD", -4948)],
    "2026-10-30": [
        ("09:49", "BULL_PUT_SPREAD", -1659),
        ("11:08", "BEAR_CALL_SPREAD", -3121),
    ],
    "2026-11-02": [("12:46", "BULL_PUT_SPREAD", -5675)],
    "2026-11-06": [("12:25", "BEAR_CALL_SPREAD", -3893)],
    "2026-11-12": [("11:03", "BULL_PUT_SPREAD", -3682)],
}

GREEN_CREDIT = {
    "2026-09-09": [("11:42", "BULL_PUT_SPREAD"), ("12:33", "BEAR_CALL_SPREAD")],
    "2026-09-10": [("10:51", "BULL_PUT_SPREAD"), ("12:21", "BEAR_CALL_SPREAD")],
    "2026-09-17": [("09:58", "BEAR_CALL_SPREAD"), ("11:07", "BULL_PUT_SPREAD")],
    "2026-09-30": [("11:15", "BULL_PUT_SPREAD"), ("12:23", "BEAR_CALL_SPREAD")],
    "2026-10-06": [("10:56", "BULL_PUT_SPREAD")],
    "2026-10-07": [("11:24", "BEAR_CALL_SPREAD")],
    "2026-10-14": [("11:35", "BULL_PUT_SPREAD"), ("12:19", "BEAR_CALL_SPREAD")],
    "2026-10-27": [("10:51", "BEAR_CALL_SPREAD"), ("12:42", "BEAR_CALL_SPREAD")],
    "2026-11-11": [("09:47", "BEAR_CALL_SPREAD")],
}

GREEN_MOM = {
    "2026-09-11": [("11:33", "LONG_CALL")],
    "2026-09-15": [("10:30", "LONG_PUT"), ("11:08", "LONG_PUT")],
    "2026-09-24": [("12:26", "LONG_PUT")],
    "2026-10-01": [("13:02", "LONG_PUT")],
    "2026-10-05": [("11:06", "LONG_PUT")],
    "2026-10-08": [("10:22", "LONG_PUT")],
    "2026-10-15": [("10:56", "LONG_CALL")],
    "2026-10-21": [("10:21", "LONG_PUT")],
    "2026-10-23": [("10:06", "LONG_PUT")],
    "2026-11-05": [("12:14", "LONG_CALL")],
}


def snap(day: str, hhmm: str) -> dict | None:
    db = PER / f"nifty_algo_{day}.db"
    if not db.exists():
        return None
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT timestamp, spot, final_regime, price_regime, vol_regime, "
        "confidence_level, adx_15, vix, day_move_used_pct, iv_behavior, time "
        "FROM market_snapshots WHERE time >= ? ORDER BY timestamp LIMIT 1",
        (hhmm + ":00",),
    ).fetchone()
    # ENTER decision near time
    dec = con.execute(
        "SELECT decision_time, reason, signals_json FROM strategy_decisions "
        "WHERE action='ENTER' AND substr(decision_time,12,5) >= ? "
        "ORDER BY decision_time LIMIT 1",
        (hhmm,),
    ).fetchone()
    con.close()
    if not row:
        return None
    out = {
        "ts": row[0],
        "final": row[2],
        "price": row[3],
        "vol": row[4],
        "conf": row[5],
        "adx": row[6],
        "vix": row[7],
        "day_move": row[8],
        "iv": row[9],
        "hhmm": hhmm,
    }
    if dec:
        try:
            sig = json.loads(dec[2] or "{}")
        except Exception:
            sig = {}
        out["or_w"] = sig.get("or_width")
        out["or_c"] = sig.get("or_condition")
        out["loc"] = sig.get("session_loc") or sig.get("day_location")
        out["tape"] = sig.get("tape_state")
        out["reason"] = (dec[1] or "")[:80]
        out["day_up"] = sig.get("day_up_used_pct")
        out["day_dn"] = sig.get("day_down_used_pct")
    return out


def dump(label: str, book: dict) -> None:
    print(f"\n======== {label}")
    for day, entries in sorted(book.items()):
        for e in entries:
            hhmm, sn = e[0], e[1]
            s = snap(day, hhmm)
            if not s:
                print(f"  {day} {hhmm} {sn} NO SNAP")
                continue
            print(
                f"  {day} {hhmm} {sn:16s} final={s.get('final')} px={s.get('price')} "
                f"vol={s.get('vol')} conf={s.get('conf')} adx={s.get('adx')} "
                f"dm={s.get('day_move')} or={s.get('or_c')} loc={s.get('loc')} "
                f"tape={s.get('tape')} iv={s.get('iv')} "
                f"up={s.get('day_up')} dn={s.get('day_dn')}"
            )


def main() -> None:
    dump("RED losers", RED)
    dump("GREEN credits", GREEN_CREDIT)
    dump("GREEN momentum", GREEN_MOM)


if __name__ == "__main__":
    main()
