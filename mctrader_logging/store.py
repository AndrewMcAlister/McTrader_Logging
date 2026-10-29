"""The SQL side: append batches, and answer queries about them.

Writes are batched with ``fast_executemany`` because log volume arrives in bursts — a trading
cycle emits dozens of lines in a second and nothing for a minute. One round trip per batch
rather than per row is the difference between a logging service and a load generator.

Nothing here ever updates or deletes a row. This is an append-only table, and treating it as
mutable is how an audit trail stops being one.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pyodbc

from .levels import level_name, level_number
from .models import canonical_service

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "001_schema.sql"

INSERT_ENTRY = """
INSERT INTO dbo.LogEntry
    (OccurredUtc, Service, CorrelationId, CycleId, Level, LevelName, Category,
     Location, Message, Symbol, BrokerId, OrderId, Payload)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

INSERT_EVENT = """
INSERT INTO dbo.LogEvent
    (OccurredUtc, Service, CorrelationId, CycleId, EventType, Level, LevelName,
     Symbol, BrokerId, OrderId, Payload)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


class StoreBusy(RuntimeError):
    """A write or read that could not get a lock in time."""


def _script_batches(text: str) -> List[str]:
    batches, current = [], []
    for line in text.splitlines():
        if line.strip().upper() == "GO":
            body = "\n".join(current).strip()
            if body:
                batches.append(body)
            current = []
        else:
            current.append(line)
    body = "\n".join(current).strip()
    if body:
        batches.append(body)
    return batches


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """UTC as a naive datetime, which is what datetime2 columns here store."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _json_or_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class Store:
    """Append-only writes and read queries for LogEntry and LogEvent."""

    LOCK_TIMEOUT_MS = 15_000

    def __init__(self, connection_string: str, timeout: int = 30,
                 max_message_chars: int = 2000) -> None:
        self.connection_string = connection_string
        self.timeout = int(timeout)
        self.max_message_chars = int(max_message_chars)

    def connect(self) -> pyodbc.Connection:
        conn = pyodbc.connect(self.connection_string, timeout=self.timeout,
                              autocommit=False, fast_executemany=True)
        cursor = conn.cursor()
        try:
            cursor.execute(f"SET LOCK_TIMEOUT {self.LOCK_TIMEOUT_MS}")
        finally:
            cursor.close()
        return conn

    # -- schema ------------------------------------------------------------

    def ensure_schema(self) -> int:
        text = SCHEMA_PATH.read_text(encoding="utf-8")
        batches = _script_batches(text)
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for index, batch in enumerate(batches, start=1):
                try:
                    cursor.execute(batch)
                    while cursor.nextset():
                        pass
                except pyodbc.Error as exc:
                    raise RuntimeError(
                        f"schema batch {index} failed: {exc}\n---\n{batch[:600]}") from exc
            conn.commit()
        finally:
            conn.close()
        log.info("schema ensured (%d batches)", len(batches))
        self.ensure_documentation()
        return len(batches)

    #: Self-documentation rows, following the TableDoc/ColumnDoc convention used in Back_Test.
    #: The `Logging` database has neither table, so these are skipped there and the method
    #: no-ops -- documentation is a nicety and must never stop the service starting.
    #: Kept in Python rather than in 001_schema.sql because SQL Server compiles a whole batch
    #: before it evaluates an ``IF``: a guarded INSERT naming a table that is absent fails at
    #: compile time, so ``IF OBJECT_ID(...) IS NOT NULL AND NOT EXISTS (SELECT ... FROM ...)``
    #: does not protect anything. Checking here means the table is known to exist first.
    DOC_STATEMENTS: List[tuple] = [
        ("TableDoc",
         """INSERT INTO dbo.TableDoc (TableName, Grain, Purpose, RowCountNote, DataSource)
            VALUES ('LogEntry', 'one row per log line',
            'APPEND-ONLY LOG written by D:\\McTrader_Logging. Sits BESIDE dbo.Logs, not over it: dbo.Logs is McTrader''s own table and keeps its writers until each app is migrated, so a query can UNION the two during the transition. What is new here is CorrelationId, which lets one trading decision be followed across Market_Data_Service -> trader -> broker; dbo.Logs has no equivalent, so that trail was previously unjoinable. ReceivedUtc is when this service stored the row, OccurredUtc is when the writer said it happened -- a widening gap between them is a backlog.',
            'grows without bound; archive by ReceivedUtc', 'measured')"""),
        ("TableDoc",
         """INSERT INTO dbo.TableDoc (TableName, Grain, Purpose, RowCountNote, DataSource)
            VALUES ('LogEvent', 'one row per domain event',
            'DOMAIN EVENTS (order submitted, filled, stop placed, signal fired), as opposed to free-text log lines. Separated from LogEntry because the two have different readers: an operator reads LogEntry when something breaks, a researcher reads LogEvent to join a decision to its outcome. dbo.BrokerEvents exists in this database with 0 rows, which is what happens when event logging lives inside the app that is busy trading.',
            'grows with trading activity', 'measured')"""),
        ("ColumnDoc",
         """INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
            VALUES ('LogEntry', 'CorrelationId', 'varchar(64)', 'opaque id',
            'Identifies one logical operation across every service that took part in it.',
            'Sent as the X-Correlation-Id header. This is the column the logging service exists for: without it a decision spanning Market_Data_Service, the trader and the broker cannot be joined, which is the state dbo.Logs is in today.')"""),
        ("ColumnDoc",
         """INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
            VALUES ('LogEntry', 'OccurredUtc', 'datetime2(3)', 'UTC instant',
            'When the writer says the event happened.',
            'ReceivedUtc is when this service stored it. Both are kept so a backlog shows up as a widening gap instead of being hidden by a single timestamp.')"""),
    ]

    def ensure_documentation(self) -> int:
        """Insert the TableDoc/ColumnDoc rows when those tables exist and the row does not."""
        written = 0
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for table, statement in self.DOC_STATEMENTS:
                try:
                    cursor.execute(
                        "SELECT CASE WHEN OBJECT_ID(?, 'U') IS NULL THEN 0 ELSE 1 END",
                        f"dbo.{table}")
                    if not cursor.fetchone()[0]:
                        continue
                    cursor.execute(statement)
                    while cursor.nextset():
                        pass
                    written += 1
                except pyodbc.Error as exc:
                    # Documentation is a nicety; never let it stop the service starting.
                    log.warning("could not write %s documentation: %s", table, exc)
            conn.commit()
        finally:
            conn.close()
        if written:
            log.info("documentation rows ensured (%d)", written)
        return written

    # -- writes ------------------------------------------------------------

    def append_entries(self, records: Sequence[Any]) -> int:
        """Insert log records. Each is a model with the fields the endpoint accepts."""
        if not records:
            return 0
        payload = []
        for record in records:
            message = (record.message or "")[: self.max_message_chars]
            payload.append((
                _naive_utc(record.occurred_utc),
                record.service,
                record.correlation_id,
                record.cycle_id,
                level_number(record.level),
                level_name(record.level),
                record.category,
                record.location,
                message,
                record.symbol,
                record.broker_id,
                record.order_id,
                _json_or_text(record.payload),
            ))
        return self._insert_resilient(INSERT_ENTRY, payload)

    def append_events(self, events: Sequence[Any]) -> int:
        if not events:
            return 0
        payload = [
            (
                _naive_utc(event.occurred_utc),
                event.service,
                event.correlation_id,
                event.cycle_id,
                event.event_type,
                level_number(getattr(event, "level", None)),
                level_name(getattr(event, "level", None)),
                event.symbol,
                event.broker_id,
                event.order_id,
                _json_or_text(event.payload),
            )
            for event in events
        ]
        return self._insert_resilient(INSERT_EVENT, payload)

    def _insert_resilient(self, sql: str, payload: List[tuple]) -> int:
        """Insert a batch; on any failure, retry row by row so one poison row costs one row.

        This exists because of a measured failure. A batch of five events carried
        ``order_id="demo-order-1"`` into a ``uniqueidentifier`` column; SQL Server rejected the
        INSERT and **all five** were lost, including four that were fine. A log sink that
        discards good lines because a neighbour was malformed is worse than no log sink, since
        it fails silently at exactly the moment the malformed thing is what you need to read.

        Row-by-row is far slower, so it is only the recovery path -- the batch attempt stays the
        normal case.
        """
        if not payload:
            return 0
        conn = self.connect()
        try:
            cursor = conn.cursor()
            try:
                cursor.executemany(sql, payload)
                conn.commit()
                return len(payload)
            except pyodbc.Error as exc:
                log.warning("batch insert of %d row(s) failed (%s); retrying row by row",
                            len(payload), str(exc)[:160])
                try:
                    conn.rollback()
                except pyodbc.Error:
                    pass

            written = 0
            rejected = 0
            for row in payload:
                try:
                    cursor.execute(sql, *row)
                    written += 1
                except pyodbc.Error:
                    rejected += 1
            conn.commit()
            if rejected:
                log.error("dropped %d unloggable row(s) of %d", rejected, len(payload))
            return written
        finally:
            conn.close()

    # -- queries -----------------------------------------------------------

    def query_entries(
        self,
        *,
        correlation_id: Optional[str] = None,
        symbol: Optional[str] = None,
        service: Optional[str] = None,
        level: Optional[str] = None,
        category: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Recent rows matching the filters, newest first.

        ``level`` is a *minimum*: asking for WARNING returns WARNING and above, which is what
        an operator means by it.
        """
        where, params = [], []
        if correlation_id:
            where.append("CorrelationId = ?")
            params.append(correlation_id)
        if symbol:
            where.append("Symbol = ?")
            params.append(str(symbol).strip().upper())
        if service:
            # Canonicalised the same way a write is, or a filter would silently miss every row
            # whose client spelled its own name differently.
            where.append("Service = ?")
            params.append(canonical_service(service))
        if level:
            where.append("Level >= ?")
            params.append(level_number(level))
        if category:
            where.append("Category = ?")
            params.append(category)
        if since:
            where.append("ReceivedUtc >= ?")
            params.append(_naive_utc(since))
        if until:
            where.append("ReceivedUtc <= ?")
            params.append(_naive_utc(until))

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT TOP (?) LogId, ReceivedUtc, OccurredUtc, Service, CorrelationId, CycleId,
                   Level, LevelName, Category, Location, Message, Symbol, BrokerId,
                   OrderId, Payload
            FROM dbo.LogEntry{clause}
            ORDER BY LogId DESC
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, int(limit), *params)
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [_entry_row(row) for row in rows]

    def query_events(
        self,
        *,
        correlation_id: Optional[str] = None,
        symbol: Optional[str] = None,
        event_type: Optional[str] = None,
        level: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Recent events, newest first. ``level`` is a *minimum*, as it is for entries."""
        where, params = [], []
        if correlation_id:
            where.append("CorrelationId = ?")
            params.append(correlation_id)
        if symbol:
            where.append("Symbol = ?")
            params.append(str(symbol).strip().upper())
        if event_type:
            where.append("EventType = ?")
            params.append(event_type)
        if level:
            where.append("Level >= ?")
            params.append(level_number(level))
        if since:
            where.append("ReceivedUtc >= ?")
            params.append(_naive_utc(since))

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT TOP (?) EventId, ReceivedUtc, OccurredUtc, Service, CorrelationId, CycleId,
                   EventType, Level, LevelName, Symbol, BrokerId, OrderId, Payload
            FROM dbo.LogEvent{clause}
            ORDER BY EventId DESC
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, int(limit), *params)
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [_event_row(row) for row in rows]

    def stats(self) -> Dict[str, Any]:
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM dbo.LogEntry),
                    (SELECT COUNT(*) FROM dbo.LogEvent),
                    (SELECT MAX(ReceivedUtc) FROM dbo.LogEntry),
                    (SELECT COUNT(*) FROM dbo.LogEntry
                     WHERE ReceivedUtc >= DATEADD(hour, -1, SYSUTCDATETIME()))
            """)
            row = cursor.fetchone()

            # Which clients are logging, and how much. Without this, "who wrote this?" is a
            # manual query against a table that only grows, and a client that has silently
            # stopped logging is invisible.
            cursor.execute("""
                SELECT Service,
                       COUNT(*) AS rows_total,
                       SUM(CASE WHEN ReceivedUtc >= DATEADD(day, -1, SYSUTCDATETIME())
                                THEN 1 ELSE 0 END) AS rows_24h,
                       MAX(ReceivedUtc) AS latest
                FROM dbo.LogEntry
                GROUP BY Service
                ORDER BY COUNT(*) DESC
            """)
            services = [
                {
                    "service": r[0],
                    "rows_total": int(r[1] or 0),
                    "rows_24h": int(r[2] or 0),
                    "latest": r[3].isoformat() if r[3] else None,
                }
                for r in cursor.fetchall()
            ]
        finally:
            conn.close()
        return {
            "log_entries": int(row[0] or 0),
            "log_events": int(row[1] or 0),
            "latest_entry_at": row[2].isoformat() if row[2] else None,
            "entries_last_hour": int(row[3] or 0),
            "services": services,
        }

    def service_levels(self) -> List[Dict[str, Any]]:
        """The per-client recording thresholds. An absent row means "record everything"."""
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT Service, MinLevel, Reason, UpdatedBy, UpdatedUtc "
                "FROM dbo.ServiceLogLevel ORDER BY Service")
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [
            {
                "service": row[0],
                "min_level": int(row[1]),
                "reason": row[2],
                "updated_by": row[3],
                "updated_utc": row[4].isoformat() if row[4] else None,
            }
            for row in rows
        ]

    def set_service_level(self, service: str, min_level: int, *,
                          reason: Optional[str] = None,
                          updated_by: Optional[str] = None) -> None:
        """Set (or replace) one client's recording threshold.

        An upsert rather than insert-or-ignore: the point of the endpoint is to change the value,
        and a second call that silently did nothing would leave the caller believing it took.
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                MERGE dbo.ServiceLogLevel AS target
                USING (SELECT ? AS Service, ? AS MinLevel, ? AS Reason, ? AS UpdatedBy)
                      AS source (Service, MinLevel, Reason, UpdatedBy)
                ON target.Service = source.Service
                WHEN MATCHED THEN
                    UPDATE SET MinLevel = source.MinLevel,
                               Reason = source.Reason,
                               UpdatedBy = source.UpdatedBy,
                               UpdatedUtc = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN
                    INSERT (Service, MinLevel, Reason, UpdatedBy)
                    VALUES (source.Service, source.MinLevel, source.Reason, source.UpdatedBy);
                """,
                canonical_service(service), int(min_level), reason, updated_by)
            conn.commit()
        finally:
            conn.close()

    def clear_service_level(self, service: str) -> bool:
        """Remove the threshold, so everything is recorded again. True if a row was deleted."""
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM dbo.ServiceLogLevel WHERE Service = ?",
                           canonical_service(service))
            removed = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return bool(removed)

    def levels(self) -> List[Dict[str, Any]]:
        """The rows of dbo.LogLevels, ordered by severity.

        The caller decides what to do with them: :mod:`mctrader_logging.levels` turns them into a
        catalogue, and falls back to the built-in levels if this raises.
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT Level, LevelName, Description, Aliases "
                "FROM dbo.LogLevels ORDER BY Level")
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [
            {
                "level": int(row[0]),
                "level_name": row[1],
                "description": row[2],
                "aliases": row[3],
            }
            for row in rows
        ]

    def ping(self) -> None:
        """Cheapest liveness check: one round trip, no table touched."""
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            conn.close()


def _entry_row(row: Any) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "received_utc": row[1].isoformat() if row[1] else None,
        "occurred_utc": row[2].isoformat() if row[2] else None,
        "service": row[3],
        "correlation_id": row[4],
        "cycle_id": row[5],
        "level": int(row[6]),
        "level_name": row[7],
        "category": row[8],
        "location": row[9],
        "message": row[10],
        "symbol": row[11],
        "broker_id": row[12],
        "order_id": str(row[13]) if row[13] else None,
        "payload": row[14],
    }


def _event_row(row: Any) -> Dict[str, Any]:
    return {
        "id": int(row[0]),
        "received_utc": row[1].isoformat() if row[1] else None,
        "occurred_utc": row[2].isoformat() if row[2] else None,
        "service": row[3],
        "correlation_id": row[4],
        "cycle_id": row[5],
        "event_type": row[6],
        "level": int(row[7]),
        "level_name": row[8],
        "symbol": row[9],
        "broker_id": row[10],
        "order_id": str(row[11]) if row[11] else None,
        "payload": row[12],
    }
