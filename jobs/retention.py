"""Delete log rows older than the retention window. Dry-run by default.

    python -m jobs.retention                 # report what would go, change nothing
    python -m jobs.retention --apply         # delete it
    python -m jobs.retention --months 3 --apply

WHY DELETION IS BATCHED

A single ``DELETE FROM dbo.LogEntry WHERE Received < ...`` over a few million rows takes one
enormous transaction: the log grows by as much as the table shrank, locks are held for the whole
run, and a cancellation rolls all of it back. Deleting ``TOP (n)`` at a time keeps each
transaction small enough to commit and gives the server room between batches.

WHY DRY-RUN IS THE DEFAULT

Retention is the one operation here that destroys data, and the window is a policy decision
rather than a fact. The default reports the count and the oldest row so the policy can be
checked against reality before anything is removed.

NO ARCHIVE, BY DECISION

Retention deletes. There is no archive step and no second copy -- a pruned row is gone. That is
deliberate: an archive is a second store with its own lifetime, restore path and retention
question, and keeping one "just in case" is how a log sink becomes two systems.

If a decision from four months ago ever needs explaining, the record belongs somewhere that is
meant to keep it, not in a file this job happened to write on the way past.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging import config as config_module
from mctrader_logging.store import Store

log = logging.getLogger("mctrader_logging.retention")

DEFAULT_MONTHS = 3
#: Rows per transaction. Small enough to commit quickly, large enough not to loop forever.
BATCH = 20_000


def _cutoff(months: int) -> datetime:
    """The oldest instant to keep: *months* before now, in whole 30-day months.

    Days rather than calendar months because the retention policy is a duration ("three
    months"), and a calendar implementation makes February shorter than January for no reason
    a log reader cares about.
    """
    return datetime.now().replace(tzinfo=None) - timedelta(days=30 * months)


def _counts(store: Store, cutoff: datetime) -> Dict[str, Any]:
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*), MIN(Received) FROM dbo.LogEntry WHERE Received < ?",
            cutoff)
        entries, oldest_entry = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*), MIN(Received) FROM dbo.LogEvent WHERE Received < ?",
            cutoff)
        events, oldest_event = cursor.fetchone()
    finally:
        conn.close()
    return {
        "entries": int(entries or 0),
        "events": int(events or 0),
        "oldest_entry": oldest_entry.isoformat() if oldest_entry else None,
        "oldest_event": oldest_event.isoformat() if oldest_event else None,
    }


def _delete_batched(store: Store, table: str, cutoff: datetime, batch: int) -> int:
    """Delete in committed batches; returns the number removed."""
    removed = 0
    conn = store.connect()
    try:
        cursor = conn.cursor()
        while True:
            cursor.execute(
                f"DELETE TOP (?) FROM dbo.{table} WHERE Received < ?", int(batch), cutoff)
            deleted = cursor.rowcount
            conn.commit()
            removed += max(0, deleted)
            if deleted < batch:
                break
            log.info("  %s: %d removed so far", table, removed)
    finally:
        conn.close()
    return removed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Log retention")
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS,
                       help=f"Keep this many 30-day months (default {DEFAULT_MONTHS})")
    parser.add_argument("--apply", action="store_true",
                       help="Actually delete. Without this, only report.")
    parser.add_argument("--batch", type=int, default=BATCH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    settings = config_module.load()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
    cutoff = _cutoff(args.months)

    log.info("database=%s retention=%d month(s) cutoff=%s",
             settings.db_database, args.months, cutoff.isoformat())
    before = _counts(store, cutoff)
    log.info("would remove: %d LogEntry, %d LogEvent", before["entries"], before["events"])
    if before["oldest_entry"]:
        log.info("oldest LogEntry: %s", before["oldest_entry"])

    if not args.apply:
        log.info("dry run -- pass --apply to delete")
        return 0

    entries = _delete_batched(store, "LogEntry", cutoff, args.batch)
    events = _delete_batched(store, "LogEvent", cutoff, args.batch)
    log.info("removed %d LogEntry and %d LogEvent row(s)", entries, events)
    log.info("remaining: %s", store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
