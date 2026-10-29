"""The client the apps import. Fire and forget, in the literal sense.

WHAT "FIRE AND FORGET" MEANS HERE

Logging has no fallback, so it must never be able to stop a trade. This client therefore does
exactly three things and no more:

1. **Queue.** ``info()``/``warning()``/``error()``/``event()`` append to a list and return. No
   I/O, no sleep, no exception.
2. **Send, or not.** A daemon thread drains the queue in batches. If the service is down, the
   lines are **dropped and counted**. There is no spool file and no replay: a client that
   silently accumulates a disk full of unsent logs is a second problem wearing the solution's
   clothes.
3. **Count.** ``stats()`` reports ``dropped`` and ``failed``, so an outage is visible as a
   number rather than as an absence.

WHO OWNS THE CORRELATION ID

The **client** does. The service cannot know what operation a caller is part of, so it stores what
it is given and never invents one. Each ``LogClient`` mints its own UUID at construction and falls
back to it whenever no scope is open:

    client = LogClient(..., service="mini_mctrader")
    client.info("m.f", "started")              -> the client's own UUID
    with correlation("cycle-1030"):
        client.info("m.f", "in the cycle")      -> cycle-1030
    client.info("m.f", "after the cycle")       -> back to the client's own UUID

THE LOCAL RECORD IS THE CLIENT'S OWN

The console/file log stays with the application -- that is your existing logging, and this
service is the *aggregate* sink beside it. To keep the two joinable, every write **returns the
correlation id** and ``format_line()`` renders it into a console line, so the message on screen
carries the id that the query needs:

    >>> line = log.info("run_entry", "12 movers, 6 selected", symbol="AXGN")
    >>> print(line)
    2026-10-29T04:21:03.412Z INFO     mctrader_demo.run_entry  [corr=2f9c1a7b] 12 movers, 6 selected symbol=AXGN

Pass ``echo=print`` (or any callable) to have the client do that for you, or ignore the return
value and format your own. The client never configures logging itself.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from .levels import Level, level_name

log = logging.getLogger(__name__)

#: The id every line written inside a ``with correlation(...)`` block inherits.
_correlation: ContextVar[Optional[str]] = ContextVar("mctrader_correlation", default=None)

#: Header name, shared with Market_Data_Service so one id spans both.
CORRELATION_HEADER = "X-Correlation-Id"


def new_correlation() -> str:
    """A fresh correlation id: a standard 36-character UUID.

    UUID rather than a short token, because an id is a thing a human quotes and a machine may
    want to store: a prefixed or truncated token is neither recognisable nor storable as-is.
    """
    return str(uuid.uuid4())


def current_correlation() -> Optional[str]:
    """The correlation id in force, or ``None``."""
    return _correlation.get()


@contextmanager
def correlation(value: Optional[str] = None) -> Iterator[str]:
    """Run a block with a correlation id for every log line inside it.

    With no argument an id is generated, which is the common case: one trading cycle opens a
    scope and every line it writes is automatically joinable.
    """
    resolved = str(value) if value else new_correlation()
    token = _correlation.set(resolved)
    try:
        yield resolved
    finally:
        _correlation.reset(token)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_line(record: Dict[str, Any]) -> str:
    """One record as a console line, with the correlation id in it.

    ``[corr=...]`` is part of the message on purpose: a human reporting a problem quotes what
    they can see, and if the id is only in the database they cannot quote it.
    """
    occurred = str(record.get("occurred_utc") or "")[:23]
    level = str(record.get("level_name") or record.get("level") or "INFO")
    service = record.get("service") or "?"
    location = record.get("location") or record.get("event_type") or "?"
    correlation_id = record.get("correlation_id") or "-"
    message = record.get("message") or record.get("event_type") or ""
    symbol = record.get("symbol")
    suffix = f" symbol={symbol}" if symbol else ""
    return (f"{occurred} {level:<8} {service}.{location}  "
            f"[corr={correlation_id}] {message}{suffix}")


class LogClient:
    """A non-blocking writer for LogEntry and LogEvent."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8004",
        service: str = "unknown",
        *,
        batch_size: int = 50,
        flush_seconds: float = 2.0,
        max_queue: int = 10_000,
        timeout: float = 5.0,
        enabled: bool = True,
        echo: Optional[Callable[[str], Any]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service = service
        self.batch_size = max(1, int(batch_size))
        self.flush_seconds = max(0.1, float(flush_seconds))
        self.max_queue = max(1, int(max_queue))
        self.timeout = float(timeout)
        self.enabled = bool(enabled)
        #: Optional sink for the console copy: ``print``, ``logging.info``, a file, anything.
        #: The client does not configure it and does not depend on it.
        self.echo = echo
        #: This client's own correlation id, used when no ``correlation()`` scope is open. A
        #: UUID, minted here because the client is the only party that knows what it is doing.
        self.default_correlation = new_correlation()
        self._session = session or requests.Session()

        self._entries: List[Dict[str, Any]] = []
        self._events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()

        self.counters = {
            "queued": 0, "sent": 0, "dropped": 0, "failed": 0, "batches": 0,
        }
        self._last_error: Optional[str] = None

        self._thread: Optional[threading.Thread] = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._run, name=f"logclient-{service}", daemon=True)
            self._thread.start()
            atexit.register(self.close)

    # ------------------------------------------------------------------
    # writers -- none of these may block, raise or sleep
    # ------------------------------------------------------------------

    def log(self, level: Any, location: str, message: str = "", **fields: Any) -> Optional[str]:
        """Queue one line and return the correlation id it will carry.

        Returning the id is what lets a caller put it in its **own** console message without
        threading an argument around, and it is the only reason this method is not ``None``.
        """
        if not self.enabled:
            return self.resolve_correlation(fields.get("correlation_id"))
        try:
            service = fields.pop("service", self.service)
            correlation_id = self.resolve_correlation(
                fields.pop("correlation_id", None))
            name = level_name(level)
            record = {
                "service": service,
                "location": str(location)[:255],
                "message": str(message),
                "level": name,
                "level_name": name,
                "correlation_id": correlation_id,
                "cycle_id": fields.pop("cycle_id", None),
                "category": fields.pop("category", None),
                "symbol": fields.pop("symbol", None),
                "broker_id": fields.pop("broker_id", None),
                "order_id": fields.pop("order_id", None),
                "occurred_utc": _utc_now().isoformat(),
                "payload": _payload_from(fields),
            }
            self._enqueue(self._entries, record)
            self._echo(record)
            return correlation_id
        except Exception:  # noqa: BLE001 - logging must never break the caller
            self.counters["dropped"] += 1
            return self.default_correlation

    def debug(self, location: str, message: str = "", **fields: Any) -> Optional[str]:
        return self.log(Level.DEBUG, location, message, **fields)

    def info(self, location: str, message: str = "", **fields: Any) -> Optional[str]:
        return self.log(Level.INFO, location, message, **fields)

    def warning(self, location: str, message: str = "", **fields: Any) -> Optional[str]:
        return self.log(Level.WARNING, location, message, **fields)

    def error(self, location: str, message: str = "", **fields: Any) -> Optional[str]:
        return self.log(Level.ERROR, location, message, **fields)

    def critical(self, location: str, message: str = "", **fields: Any) -> Optional[str]:
        return self.log(Level.CRITICAL, location, message, **fields)

    def event(self, event_type: str, level: Any = None, **fields: Any) -> Optional[str]:
        """Queue one domain event (order.submitted, stop.placed, ...). Returns the id.

        *level* defaults to INFO. An event is a statement that something happened, not a severity,
        so most callers leave it alone -- but a rejected order or a failed fill is not the same as
        an acknowledged one, and without a level "everything at WARNING or above" could not include
        events at all.
        """
        if not self.enabled:
            return self.resolve_correlation(fields.get("correlation_id"))
        try:
            service = fields.pop("service", self.service)
            correlation_id = self.resolve_correlation(
                fields.pop("correlation_id", None))
            name = level_name(level) if level is not None else "INFO"
            # The console keeps events visibly distinct from log lines, but must not hide a
            # severe one: an INFO event reads EVENT, a WARNING one reads EVENT/WARNING.
            tag = "EVENT" if name == "INFO" else f"EVENT/{name}"
            record = {
                "service": service,
                "event_type": str(event_type)[:64],
                "location": str(event_type)[:255],
                "level": name,
                "level_name": tag,
                "correlation_id": correlation_id,
                "cycle_id": fields.pop("cycle_id", None),
                "symbol": fields.pop("symbol", None),
                "broker_id": fields.pop("broker_id", None),
                "order_id": fields.pop("order_id", None),
                "occurred_utc": _utc_now().isoformat(),
                "payload": _payload_from(fields),
            }
            self._enqueue(self._events, record)
            self._echo(record)
            return correlation_id
        except Exception:  # noqa: BLE001
            self.counters["dropped"] += 1
            return self.default_correlation

    def _echo(self, record: Dict[str, Any]) -> None:
        """Hand the console copy to whatever sink the caller supplied, if any."""
        if self.echo is None:
            return
        try:
            self.echo(format_line(record))
        except Exception:  # noqa: BLE001 - a broken console sink is the caller's problem
            pass

    def resolve_correlation(self, explicit: Optional[str] = None) -> str:
        """The id this line will carry: explicit, then the ambient scope, then our own UUID."""
        return explicit or current_correlation() or self.default_correlation

    # ------------------------------------------------------------------
    # queue and flush
    # ------------------------------------------------------------------

    def _enqueue(self, queue: List[Dict[str, Any]], record: Dict[str, Any]) -> None:
        with self._lock:
            if len(queue) >= self.max_queue:
                # Drop the NEWEST line: the oldest are the ones that explain how the process
                # got here, and a log line that costs a trade is a bad trade.
                self.counters["dropped"] += 1
                return
            queue.append(record)
            self.counters["queued"] += 1
            enough = len(queue) >= self.batch_size
        if enough:
            self._wake.set()

    def _drain(self, queue: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        with self._lock:
            taken, queue[:] = queue[:limit], queue[limit:]
            return taken

    def flush(self, timeout: Optional[float] = None) -> Dict[str, int]:
        """Send what is queued now. Safe from any thread; bounded by *timeout*."""
        sent = failed = 0
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout * 2)
        while time.monotonic() < deadline:
            batch = self._drain(self._entries, self.batch_size)
            event_batch = self._drain(self._events, self.batch_size)
            if not batch and not event_batch:
                break
            if batch:
                if self._post("/log", {"records": batch}):
                    sent += len(batch)
                    self.counters["sent"] += len(batch)
                    self.counters["batches"] += 1
                else:
                    # Fire and forget: a failed batch is gone. The console copy already said
                    # what it had to say, and the counter records the loss.
                    failed += len(batch)
                    self.counters["dropped"] += len(batch)
            if event_batch:
                if self._post("/event", {"events": event_batch}):
                    sent += len(event_batch)
                    self.counters["sent"] += len(event_batch)
                else:
                    failed += len(event_batch)
                    self.counters["dropped"] += len(event_batch)
        return {"sent": sent, "failed": failed}

    def _post(self, path: str, body: Dict[str, Any]) -> bool:
        try:
            headers = {CORRELATION_HEADER: self.resolve_correlation()}
            response = self._session.post(
                self.base_url + path, json=body, timeout=self.timeout, headers=headers)
            if response.status_code in (200, 201, 202):
                return True
            self.counters["failed"] += 1
            self._last_error = f"HTTP {response.status_code}: {response.text[:180]}"
            return False
        except Exception as exc:  # noqa: BLE001 - a logging failure is never a caller failure
            self.counters["failed"] += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False

    # -- background thread ---------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(timeout=self.flush_seconds)
            self._wake.clear()
            if self._stopping.is_set():
                break
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - the thread must survive a bad flush
                self.counters["failed"] += 1

    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            pending = len(self._entries) + len(self._events)
        return {"service": self.service, "pending": pending,
                "last_error": self._last_error, **dict(self.counters)}

    def close(self, timeout: float = 3.0) -> None:
        """Stop the thread and make one bounded flush attempt.

        Bounded on purpose: a shutdown that waits for a logging service can hang, and a process
        that will not exit is worse than a few lost log lines.
        """
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        try:
            self.flush(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "LogClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _payload_from(fields: Dict[str, Any]) -> Optional[Any]:
    """Build the stored payload from the caller's keyword arguments.

    ``payload`` is both a field name and the keyword a caller naturally reaches for, so
    ``event("x", payload={"a": 1})`` must not produce ``{"payload": {"a": 1}}``. All three
    shapes a caller might write are accepted:

        event("x", notional=2500)                  -> {"notional": 2500}
        event("x", payload={"notional": 2500})     -> {"notional": 2500}
        event("x", payload={"a": 1}, b=2)          -> {"payload": {"a": 1}, "b": 2}
    """
    explicit = fields.pop("payload", None)
    if explicit is None:
        return fields or None
    if fields:
        return {"payload": explicit, **fields}
    return explicit
