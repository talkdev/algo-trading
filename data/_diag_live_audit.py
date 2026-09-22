"""Audit all live days for failure patterns like Sep22."""
import sqlite3
from collections import Counter, defaultdict

con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row

print("=== LIVE positions all days ===")
for r in con.execute(
    """
    SELECT trading_date, strategy_name,
           substr(entry_time,12,8) ent, substr(coalesce(exit_time,''),12,8) ext,
           round(net_pnl_rupees,1) pnl, exit_reason
    FROM positions
    WHERE trading_date >= '2026-09-08'
    ORDER BY trading_date, entry_time
    """
):
    print(dict(r))

print("\n=== Daily PnL ===")
for r in con.execute(
    """
    SELECT trading_date, round(SUM(net_pnl_rupees),1) pnl, COUNT(*) n
    FROM positions WHERE trading_date >= '2026-09-08'
    GROUP BY 1 ORDER BY 1
    """
):
    print(dict(r))

days = [r["trading_date"] for r in con.execute(
    "SELECT DISTINCT trading_date FROM cycle_log WHERE trading_date>='2026-09-08' ORDER BY 1"
)]
print("\ncycle_log days", days)

# Top refusal families per day (post entry window-ish)
print("\n=== Top refusal prefixes per day ===")
for day in days:
    ctr = Counter()
    n = 0
    for r in con.execute(
        """
        SELECT reason FROM strategy_decisions
        WHERE trading_date=? AND action='NO_TRADE'
        """,
        (day,),
    ):
        n += 1
        reason = r["reason"] or ""
        # prefer the sell-side clause before momentum
        first = reason.split("|")[0]
        # normalize
        for tok in (
            "before_entry_window", "BEFORE_09:45", "max_concurrent",
            "straddle_expanding", "max_pain", "credit_ratio", "ev_gate",
            "entry_cooldown", "CHOPPY", "PAST_14", "slot_conflict",
            "same_side_chase", "counter_trend", "condor_", "range_wait",
            "two_way", "strategy_rules_failed", "iv_expanding", "momentum_refused",
            "EVENT", "day_move", "params_invalid", "wing_cost", "spread",
            "OPEN_SPIKE", "second_slot", "MAX_ENTRIES",
        ):
            if tok.lower() in first.lower() or tok.lower() in reason.lower():
                # classify by first matching interesting token
                pass
        # bucket
        if "straddle_expanding" in reason:
            ctr["straddle_expanding"] += 1
        elif "max_pain" in reason:
            ctr["max_pain"] += 1
        elif "before_entry_window" in reason or "BEFORE_09:45" in reason:
            ctr["before_window"] += 1
        elif "max_concurrent" in reason:
            ctr["max_concurrent"] += 1
        elif "credit_ratio" in reason:
            ctr["credit_ratio"] += 1
        elif "ev_gate" in reason or "ev_negative" in reason:
            ctr["ev_gate"] += 1
        elif "counter_trend" in reason:
            ctr["counter_trend"] += 1
        elif "range_wait" in reason:
            ctr["range_wait"] += 1
        elif "condor_" in reason or "IRON_CONDOR" in reason:
            ctr["condor_path"] += 1
        elif "two_way" in reason:
            ctr["two_way"] += 1
        elif "CHOPPY" in reason:
            ctr["choppy"] += 1
        elif "entry_cooldown" in reason or "cooldown" in reason:
            ctr["cooldown"] += 1
        elif "after_credit_stop" in reason:
            ctr["after_credit_stop"] += 1
        elif "slot_conflict" in reason:
            ctr["slot_conflict"] += 1
        elif "strategy_rules_failed" in reason:
            # extract subreason
            sub = first
            if "strategy_rules_failed:" in first:
                sub = first.split("strategy_rules_failed:", 1)[1][:50]
            ctr[f"rules:{sub}"] += 1
        else:
            ctr[first[:55]] += 1
    print(f"\n-- {day} (n={n}) --")
    for k, v in ctr.most_common(12):
        print(f"  {v:5d}  {k}")

print("\n=== STRATEGY_SELECTED per day ===")
for day in days:
    rows = list(con.execute(
        """
        SELECT substr(decision_time,12,8) t, strategy_name, substr(reason,1,100) r
        FROM strategy_decisions
        WHERE trading_date=? AND action='STRATEGY_SELECTED'
        ORDER BY decision_time
        """,
        (day,),
    ))
    if rows:
        print(f"\n-- {day} --")
        for r in rows:
            print(dict(r))
