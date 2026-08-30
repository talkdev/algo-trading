import sqlite3
import pandas as pd

conn = sqlite3.connect("data/state.db")

# View all closed trades
trades = pd.read_sql("SELECT * FROM closed_trades", conn)
print(trades[["strategy_name", "realized_pnl", "exit_reason"]])

# View regime history
regimes = pd.read_sql(
    "SELECT timestamp, confirmed_regime, composite_score "
    "FROM regime_history ORDER BY id DESC LIMIT 20",
    conn
)
print(regimes)

# View order log
orders = pd.read_sql(
    "SELECT * FROM order_log ORDER BY id DESC LIMIT 50",
    conn
)
print(orders)

conn.close()