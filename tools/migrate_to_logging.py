"""Move dbo.LogEntry and dbo.LogEvent from McTrader_Live into the new `Logging` database.

    python tools/migrate_to_logging.py --dry-run     # report only
    python tools/migrate_to_logging.py               # copy, verify, then drop

WHAT THIS DOES, IN ORDER

1. Creates the tables in the target database (idempotent; same schema file the service uses).
2. Copies every row, preserving ``LogId``/``EventId`` so existing references survive.
3. **Verifies** the target row count equals the source row count and that the id ranges match.
4. Only then drops the source tables.

Step 3 is why this is a script and not a ``SELECT ... INTO`` in a query window: dropping the only
copy of a table is not something to do on the strength of "the insert looked like it worked".

WHAT IT WILL NOT TOUCH

``dbo.Logs`` — McTrader's own table, 101,336 rows, nothing to do with this service. The drop is
guarded by an explicit table-name whitelist so a typo cannot reach it.

WHY IDENTITY_INSERT

Both tables use an IDENTITY primary key. Letting the target reassign ids would silently renumber
history, and a log id quoted in a ticket or a bug report would point at the wrong row afterwards.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging.store import Store

SERVER = "localhost"
DRIVER = "ODBC Driver 17 for SQL Server"
SOURCE_DB = "McTrader_Live"
TARGET_DB = "Logging"

#: Only these two may ever be dropped. dbo.Logs is McTrader's and is not ours to remove.
MOVABLE = ("LogEntry", "LogEvent")

#: Explicit column lists, because IDENTITY_INSERT refuses `SELECT *`: the server cannot verify a
#: positional copy includes the identity column, so it declines rather than guess.
COLUMNS = {
    "LogEntry": ("LogId, Received, Occurred, Service, CorrelationId, CycleId, Level, "
                 "LevelName, Category, Location, Message, Symbol, BrokerId, OrderId, Payload"),
    "LogEvent": ("EventId, Received, Occurred, Service, CorrelationId, CycleId, EventType, "
                 "Symbol, BrokerId, OrderId, Payload"),
}


def conn_string(database: str) -> str:
    return (f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={database};"
            "Trusted_Connection=yes;TrustServerCertificate=yes;")


def counts(cursor: pyodbc.Cursor, database: str) -> Dict[str, Tuple[int, Any, Any]]:
    """row count, min id, max id — per table."""
    out: Dict[str, Tuple[int, Any, Any]] = {}
    for table in MOVABLE:
        id_column = "LogId" if table == "LogEntry" else "EventId"
        cursor.execute(
            f"SELECT COUNT(*), MIN({id_column}), MAX({id_column}) "
            f"FROM {database}.dbo.{table}")
        row = cursor.fetchone()
        out[table] = (int(row[0] or 0), row[1], row[2])
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move logging tables to a new database")
    parser.add_argument("--source", default=SOURCE_DB)
    parser.add_argument("--target", default=TARGET_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    print(f"source : [{args.source}]")
    print(f"target : [{args.target}]")

    # -- 1. schema in the target ----------------------------------------
    target_store = Store(conn_string(args.target))
    batches = target_store.ensure_schema()
    print(f"schema : {batches} batch(es) applied to [{args.target}]")

    # -- 2. what moves? --------------------------------------------------
    conn = pyodbc.connect(conn_string(args.target), timeout=60, autocommit=True)
    conn.timeout = 120
    cursor = conn.cursor()

    before = counts(cursor, args.source)
    print("\nsource rows:")
    for table, (rows, low, high) in before.items():
        print(f"  {args.source}.dbo.{table:<10} {rows:>7} rows, ids {low}..{high}")

    if args.dry_run:
        print("\ndry run -- nothing copied, nothing dropped")
        conn.close()
        return 0

    # -- 3. copy, preserving ids ----------------------------------------
    print()
    for table in MOVABLE:
        rows = before[table][0]
        if rows == 0:
            print(f"  {table}: nothing to copy")
            continue
        id_column = "LogId" if table == "LogEntry" else "EventId"
        cursor.execute(f"SET IDENTITY_INSERT {args.target}.dbo.{table} ON")
        try:
            columns = COLUMNS[table]
            cursor.execute(f"""
                INSERT INTO {args.target}.dbo.{table} ({columns})
                SELECT {columns} FROM {args.source}.dbo.{table}
            """)
            print(f"  {table}: copied {cursor.rowcount} row(s)")
        finally:
            cursor.execute(f"SET IDENTITY_INSERT {args.target}.dbo.{table} OFF")

        # Keep the identity counter ahead of the copied ids, or the next insert collides.
        cursor.execute(f"SELECT MAX({id_column}) FROM {args.target}.dbo.{table}")
        high = cursor.fetchone()[0]
        if high is not None:
            cursor.execute(f"DBCC CHECKIDENT ('{args.target}.dbo.{table}', RESEED, {int(high)}) "
                           f"WITH NO_INFOMSGS")
            print(f"  {table}: identity reseeded to {int(high)}")

    # -- 4. verify before destroying anything ---------------------------
    after = counts(cursor, args.target)
    print("\nverification:")
    ok = True
    for table in MOVABLE:
        src_rows, src_low, src_high = before[table]
        dst_rows, dst_low, dst_high = after[table]
        matches = (src_rows == dst_rows and src_low == dst_low and src_high == dst_high)
        mark = "OK " if matches else "FAIL"
        print(f"  [{mark}] {table:<10} source {src_rows} rows ids {src_low}..{src_high} | "
              f"target {dst_rows} rows ids {dst_low}..{dst_high}")
        ok = ok and matches

    if not ok:
        print("\nABORT: the copy does not match the source. Nothing has been dropped.")
        conn.close()
        return 1

    # -- 5. drop the originals ------------------------------------------
    print("\ndropping source tables:")
    for table in MOVABLE:
        if table not in MOVABLE:  # belt and braces on the whitelist
            raise SystemExit(f"refusing to drop {table}")
        cursor.execute(f"DROP TABLE {args.source}.dbo.{table}")
        print(f"  dropped {args.source}.dbo.{table}")

    # dbo.Logs must still be there, untouched. The sys.tables lookup must be qualified with the
    # database, or it answers for the *current* connection's catalog -- which is the target.
    cursor.execute(
        f"SELECT COUNT(*) FROM {args.source}.sys.tables t "
        f"JOIN {args.source}.sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE s.name = 'dbo' AND t.name = 'Logs'")
    print(f"\n{args.source}.dbo.Logs still present: {bool(cursor.fetchone()[0])}")
    cursor.execute(f"SELECT COUNT(*) FROM {args.source}.dbo.Logs")
    print(f"{args.source}.dbo.Logs rows: {cursor.fetchone()[0]:,}")

    conn.close()
    print(f"\nfinal [{args.target}] state: {target_store.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
