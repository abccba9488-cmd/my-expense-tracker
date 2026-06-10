import os
import sqlite3
from datetime import date, timedelta

DB = "/app/data/stocks.db"
YEARS = 5
cutoff = (date.today() - timedelta(days=YEARS * 365)).isoformat()

print(f"DB size before: {os.path.getsize(DB) // 1024 // 1024} MB")

conn = sqlite3.connect(DB)
cur = conn.cursor()

before = cur.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
cur.execute("DELETE FROM daily_prices WHERE date < ?", (cutoff,))
conn.commit()
after = cur.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
print(f"daily_prices: {before} -> {after} rows (cutoff={cutoff})")

print("VACUUM... this can take several minutes")
cur.execute("VACUUM")
conn.close()

print(f"DB size after: {os.path.getsize(DB) // 1024 // 1024} MB")
print("Done.")
