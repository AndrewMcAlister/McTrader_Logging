"""Apply sql/001_schema.sql and report what landed."""
import sys
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging import config
from mctrader_logging.store import Store

settings = config.load()
print(f"database : {settings.db_database} @ {settings.db_server}")
print(f"conn     : {settings.connection_string}")
print()

store = Store(settings.connection_string, timeout=settings.db_timeout_seconds,
              max_message_chars=settings.max_message_chars)
applied = store.ensure_schema()
print(f"applied {applied} batch(es)")
print("stats:", store.stats())
print()

conn = pyodbc.connect(settings.connection_string, timeout=30)
cur = conn.cursor()

# TableDoc/ColumnDoc live in Back_Test, not here, so the documentation rows are optional.
cur.execute("SELECT CASE WHEN OBJECT_ID('dbo.TableDoc','U') IS NULL THEN 0 ELSE 1 END")
has_tabledoc = bool(cur.fetchone()[0])
print(f"dbo.TableDoc present: {has_tabledoc}"
      f"{' (documentation rows written)' if has_tabledoc else ' (documentation skipped)'}")
if has_tabledoc:
    cur.execute("""SELECT TableName FROM dbo.TableDoc
                   WHERE TableName IN ('LogEntry','LogEvent','Logs') ORDER BY TableName""")
    print("  TableDoc rows:", [r[0] for r in cur.fetchall()])

# The tables this service owns must exist, and dbo.Logs must be untouched.
for table in ("LogEntry", "LogEvent", "Logs"):
    cur.execute("SELECT CASE WHEN OBJECT_ID(?, 'U') IS NULL THEN 0 ELSE 1 END", f"dbo.{table}")
    exists = bool(cur.fetchone()[0])
    if exists:
        cur.execute(f"SELECT COUNT(*) FROM dbo.{table}")
        print(f"  dbo.{table}: {cur.fetchone()[0]:,} rows")
    else:
        print(f"  dbo.{table}: MISSING")
conn.close()
