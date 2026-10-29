"""The HTTP surface.

    POST /log         append a batch of log lines        -> 202
    POST /event       append a batch of domain events     -> 202
    GET  /logs        query log lines
    GET  /events      query domain events
    GET  /healthz     is it alive, and can it reach its database
    GET  /stats       row counts and per-hour volume

WHY 202 AND NOT 200

The batch is written before the response is sent, so ``accepted`` really does mean stored. The
status is 202 because the *client's* contract is fire-and-forget: it queues and returns, and
never learns the outcome on the trading thread. Answering 200 would invite a caller to treat
the round trip as part of its own latency, which is the coupling this design exists to avoid.

Endpoints are plain ``def``, not ``async def``: every one of them calls blocking pyodbc, and a
blocking call inside an ``async def`` serialises the whole event loop. That mistake cost real
time in Market_Data_Service — a health check hung for 90 s behind a slow request.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__, config as config_module
from .levels import (LevelCatalogue, get_catalogue, recorded, resolve_level,
                     set_catalogue)
from .models import (AcceptedResponse, EventBatch, LogBatch, ServiceLevelRequest,
                     canonical_service)
from .store import Store, StoreBusy

log = logging.getLogger("mctrader_logging.api")


class QueryResult(BaseModel):
    count: int
    rows: List[Dict[str, Any]]


def _store(request: Request) -> Store:
    return request.app.state.store


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = config_module.load()
    app.state.settings = settings
    app.state.store = Store(settings.connection_string, timeout=settings.db_timeout_seconds,
                            max_message_chars=settings.max_message_chars)
    try:
        app.state.store.ensure_schema()
    except Exception as exc:  # noqa: BLE001 - a schema failure must be visible, not fatal
        log.error("schema ensure failed: %s", exc)

    # The levels are data now. Load them once and keep them in memory: the write path must never
    # wait on a query, and if the table cannot be read the built-in levels are already in force,
    # so a catalogue problem degrades the vocabulary rather than stopping the service.
    try:
        rows = app.state.store.levels()
        catalogue = LevelCatalogue.from_rows(
            (row["level"], row["level_name"], row["description"], row["aliases"])
            for row in rows)
        set_catalogue(catalogue)
        log.info("log levels: %s", ", ".join(
            f"{entry['level']}={entry['name']}" for entry in catalogue.listing()))
    except Exception as exc:  # noqa: BLE001
        log.error("could not load dbo.LogLevels (%s); using the built-in levels", exc)

    # Per-client recording thresholds, held in memory so the write path never queries for them.
    # None set means record everything, which is the state on a fresh database.
    app.state.thresholds = {}
    app.state.suppressed = {}
    try:
        for row in app.state.store.service_levels():
            app.state.thresholds[row["service"]] = int(row["min_level"])
        if app.state.thresholds:
            log.info("recording thresholds: %s", ", ".join(
                f"{name}>={value}" for name, value in sorted(app.state.thresholds.items())))
    except Exception as exc:  # noqa: BLE001
        log.error("could not load dbo.ServiceLogLevel (%s); recording everything", exc)

    log.info("McTrader_Logging %s ready: db=%s port=%s",
             __version__, settings.db_database, settings.port)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="McTrader_Logging",
        version=__version__,
        description=(
            "One log sink for McTrader, PaperTrader, PortfolioAdvisor, Mini-McTrader and "
            "Market_Data_Service. Its reason to exist is CorrelationId: without it a single "
            "trading decision cannot be followed across the processes that took part in it."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(StoreBusy)
    def _busy(request: Request, exc: StoreBusy) -> JSONResponse:
        return JSONResponse(status_code=503,
                            content={"error": "database-busy", "detail": str(exc),
                                     "retryable": True})

    # -- writes ------------------------------------------------------------

    @app.post("/log", response_model=AcceptedResponse, status_code=202, tags=["write"])
    def post_log(request: Request, body: LogBatch) -> Any:
        settings = request.app.state.settings
        if len(body.records) > settings.max_batch:
            return JSONResponse(
                status_code=413,
                content={"error": "batch-too-large",
                         "detail": f"{len(body.records)} records exceeds "
                                   f"{settings.max_batch}; batch on the client side"})
        store = _store(request)
        accepted, errors = 0, []

        # The recording threshold: a row below the client's floor is dropped before it is written,
        # which is the point -- it saves the INSERT, not just the read.
        #
        # Applies to log lines only. Events are the record of what was done, and `order.submitted`
        # is an INFO event, so applying a diagnostic threshold to them would stop a client
        # recording its own orders in the name of reducing noise.
        thresholds = getattr(request.app.state, "thresholds", {})
        suppressed = 0
        if thresholds:
            kept = []
            for record in body.records:
                floor = thresholds.get(record.service)
                if not recorded(record.level, floor):
                    suppressed += 1
                    counts = request.app.state.suppressed
                    counts[record.service] = counts.get(record.service, 0) + 1
                    continue
                kept.append(record)
            records = kept
        else:
            records = body.records

        # Write in chunks so one malformed record cannot cost the whole batch.
        for start in range(0, len(records), 250):
            chunk = records[start:start + 250]
            try:
                accepted += store.append_entries(chunk)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"chunk {start}: {type(exc).__name__}: {exc}")
                log.warning("log chunk rejected: %s", exc)
        payload = AcceptedResponse(accepted=accepted,
                                   rejected=len(records) - accepted,
                                   suppressed=suppressed,
                                   errors=errors[:10])
        # A batch that was entirely suppressed is not a failure: nothing was refused, there was
        # simply nothing above the client's floor to keep.
        if accepted == 0 and records:
            return JSONResponse(status_code=503, content=payload.model_dump())
        return payload

    @app.post("/event", response_model=AcceptedResponse, status_code=202, tags=["write"])
    def post_event(request: Request, body: EventBatch) -> Any:
        settings = request.app.state.settings
        if len(body.events) > settings.max_batch:
            return JSONResponse(
                status_code=413,
                content={"error": "batch-too-large",
                         "detail": f"{len(body.events)} events exceeds {settings.max_batch}"})
        store = _store(request)
        accepted, errors = 0, []
        for start in range(0, len(body.events), 250):
            chunk = body.events[start:start + 250]
            try:
                accepted += store.append_events(chunk)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"chunk {start}: {type(exc).__name__}: {exc}")
                log.warning("event chunk rejected: %s", exc)
        payload = AcceptedResponse(accepted=accepted,
                                   rejected=len(body.events) - accepted,
                                   errors=errors[:10])
        if accepted == 0 and body.events:
            return JSONResponse(status_code=503, content=payload.model_dump())
        return payload

    # -- recording thresholds ----------------------------------------------

    @app.put("/services/{service}/level", tags=["ops"])
    def put_service_level(request: Request, service: str,
                          body: ServiceLevelRequest) -> Any:
        """Set the minimum level this client's log lines must reach to be **recorded**.

        This discards rows rather than hiding them, so it is applied before the write. ``ERROR``
        keeps only ERROR and CRITICAL; ``DEBUG`` records everything.

        It does **not** affect events. ``order.submitted`` is an INFO event, so a threshold that
        applied to events would stop a client recording its own orders -- deleting the audit trail
        in the name of reducing noise.
        """
        catalogue = get_catalogue()
        listing = catalogue.listing()
        known = {entry["name"].lower() for entry in listing}
        known |= {str(alias).lower() for entry in listing for alias in entry["aliases"]}
        supplied = str(body.level).strip().lower()
        if not supplied.lstrip("-").isdigit() and supplied not in known:
            return JSONResponse(
                status_code=400,
                content={"error": "unknown-level",
                         "detail": f"'{body.level}' is not a level",
                         "levels": listing})

        resolved = resolve_level(body.level)
        account = (request.headers.get("X-Client-Name")
                   or (request.client.host if request.client else "unknown"))
        try:
            _store(request).set_service_level(
                service, resolved.number, reason=body.reason, updated_by=account)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=503,
                                content={"error": "store-unavailable",
                                         "detail": f"{type(exc).__name__}: {exc}"})

        canonical = canonical_service(service)
        request.app.state.thresholds[canonical] = resolved.number
        log.warning("recording threshold for %s set to %s (%s) by %s",
                    canonical, resolved.name, body.reason or "no reason given", account)
        return {
            "service": canonical,
            "min_level": resolved.number,
            "min_level_name": resolved.name,
            "records": f"{resolved.name} and above",
            "affects_events": False,
            "reason": body.reason,
            "updated_by": account,
        }

    @app.get("/services/{service}/level", tags=["ops"])
    def get_service_level(request: Request, service: str) -> Any:
        """Report this client's recording threshold, and how much it has suppressed."""
        canonical = canonical_service(service)
        floor = getattr(request.app.state, "thresholds", {}).get(canonical)
        resolved = resolve_level(floor) if floor is not None else None
        return {
            "service": canonical,
            "min_level": floor,
            "min_level_name": resolved.name if resolved else None,
            "records": (f"{resolved.name} and above" if resolved
                        else "everything (no threshold set)"),
            "affects_events": False,
            "suppressed_since_start": getattr(
                request.app.state, "suppressed", {}).get(canonical, 0),
        }

    @app.get("/services", tags=["ops"])
    def list_service_levels(request: Request) -> Dict[str, Any]:
        """Every client with a threshold, plus anything suppressed since this process started.

        Reported from memory, so it describes what this service is actually doing now -- not what
        the table would do after a restart.
        """
        thresholds = getattr(request.app.state, "thresholds", {})
        suppressed = getattr(request.app.state, "suppressed", {})
        rows = []
        for name in sorted(set(thresholds) | set(suppressed)):
            floor = thresholds.get(name)
            resolved = resolve_level(floor) if floor is not None else None
            rows.append({
                "service": name,
                "min_level": floor,
                "min_level_name": resolved.name if resolved else None,
                "records": (f"{resolved.name} and above" if resolved
                            else "everything (no threshold set)"),
                "suppressed_since_start": suppressed.get(name, 0),
            })
        return {"count": len(rows), "default": "everything", "affects_events": False,
                "services": rows}

    @app.delete("/services/{service}/level", tags=["ops"])
    def delete_service_level(request: Request, service: str) -> Any:
        """Remove the threshold, so every level is recorded again."""
        try:
            removed = _store(request).clear_service_level(service)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=503,
                                content={"error": "store-unavailable",
                                         "detail": f"{type(exc).__name__}: {exc}"})
        canonical = canonical_service(service)
        request.app.state.thresholds.pop(canonical, None)
        log.warning("recording threshold for %s cleared; recording everything again", canonical)
        return {"service": canonical, "removed": removed,
                "records": "everything (no threshold set)"}

    # -- stopping ------------------------------------------------------------

    @app.post("/admin/shutdown", status_code=202, tags=["ops"])
    def shutdown(request: Request) -> Dict[str, Any]:
        """Stop this process, from inside this process.

        Session isolation means nothing in an interactive shell can terminate it: Task Scheduler
        starts the service in session 0, and a session-2 process gets "Access is denied" at any run
        level. A process can always end itself, which is what makes stop.bat a plain HTTP call with
        no elevation and no second scheduled task.

        Logged at WARNING: stopping the sink is a decision that should leave a trace.
        """
        caller = request.client.host if request.client else "unknown"
        log.warning("shutdown requested via /admin/shutdown by %s", caller)
        logging.shutdown()  # flush handlers before the process goes
        threading.Timer(0.4, lambda: os._exit(0)).start()
        return {"ok": True, "stopping": True, "requested_by": caller,
                "note": "the boot task is boot-only, so nothing restarts this; run start.bat"}

    # -- reads -------------------------------------------------------------

    @app.get("/logs", response_model=QueryResult, tags=["read"])
    def get_logs(
        request: Request,
        correlation_id: Optional[str] = Query(None, description="Every line in one decision"),
        symbol: Optional[str] = Query(None),
        service: Optional[str] = Query(None),
        level: Optional[str] = Query(None, description="Minimum level, e.g. WARNING"),
        category: Optional[str] = Query(None),
        minutes: Optional[int] = Query(None, description="Look back this many minutes"),
        limit: int = Query(200, ge=1, le=5000),
    ) -> Any:
        settings = request.app.state.settings
        since = None
        if minutes:
            since = datetime.now(timezone.utc) - timedelta(minutes=int(minutes))
        elif not correlation_id and not symbol:
            # A bare query is a recent query: without this it scans the whole table.
            since = datetime.now(timezone.utc) - timedelta(
                minutes=settings.default_query_minutes)
        rows = _store(request).query_entries(
            correlation_id=correlation_id, symbol=symbol, service=service, level=level,
            category=category, since=since, limit=min(limit, settings.max_query_rows))
        return QueryResult(count=len(rows), rows=rows)

    @app.get("/events", response_model=QueryResult, tags=["read"])
    def get_events(
        request: Request,
        correlation_id: Optional[str] = Query(None),
        symbol: Optional[str] = Query(None),
        event_type: Optional[str] = Query(None),
        level: Optional[str] = Query(None, description="Minimum level, e.g. WARNING"),
        minutes: Optional[int] = Query(None),
        limit: int = Query(200, ge=1, le=5000),
    ) -> Any:
        settings = request.app.state.settings
        since = None
        if minutes:
            since = datetime.now(timezone.utc) - timedelta(minutes=int(minutes))
        rows = _store(request).query_events(
            correlation_id=correlation_id, symbol=symbol, event_type=event_type, level=level,
            since=since, limit=min(limit, settings.max_query_rows))
        return QueryResult(count=len(rows), rows=rows)

    # -- ops ---------------------------------------------------------------

    @app.get("/healthz", tags=["ops"])
    def healthz(request: Request) -> Dict[str, Any]:
        settings = request.app.state.settings
        store = _store(request)
        db_ok, db_error = True, None
        try:
            store.ping()
        except Exception as exc:  # noqa: BLE001
            db_ok, db_error = False, f"{type(exc).__name__}: {exc}"
        return {
            "status": "ok" if db_ok else "degraded",
            "version": __version__,
            "database": settings.db_database,
            "database_ok": db_ok,
            "database_error": db_error,
        }

    @app.get("/levels", tags=["ops"])
    def levels() -> Dict[str, Any]:
        """The level catalogue in force, ordered by severity.

        Served from memory, not from the table, so this reports what the service will actually
        accept -- which is the question a client has.
        """
        catalogue = get_catalogue()
        return {"count": len(catalogue.numbers), "levels": catalogue.listing()}

    @app.get("/stats", tags=["ops"])
    def stats(request: Request) -> Dict[str, Any]:
        store = _store(request)
        settings = request.app.state.settings
        payload: Dict[str, Any] = {}
        try:
            payload["database"] = store.stats()
        except Exception as exc:  # noqa: BLE001
            payload["database_error"] = f"{type(exc).__name__}: {exc}"
        payload["settings"] = {
            "database": settings.db_database,
            "max_batch": settings.max_batch,
            "max_query_rows": settings.max_query_rows,
        }
        return payload

    return app


app = create_app()
