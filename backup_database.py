import sqlite3
from pathlib import Path
from datetime import datetime

DB = Path(r"C:\Users\Administrator\Desktop\algo-trading\data\nifty_algo_v3.db")
BACKUP_DIR = DB.parent / "backup"

BACKUP_DIR.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = BACKUP_DIR / f"{DB.stem}_{timestamp}.db"

with sqlite3.connect(DB) as source:
    with sqlite3.connect(backup) as destination:
        source.backup(destination)

print(f"Backup created: {backup}")