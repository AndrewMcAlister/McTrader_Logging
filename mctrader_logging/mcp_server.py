"""The MCP wrapper for logs: **read-only, and deliberately narrower than the REST API.**

WHY THIS IS NOT A THIN PROXY, UNLIKE THE MARKET DATA ONE

The market data service's MCP wrapper forwards requests unchanged, because its answers are small
and already shaped: a price is one object, an ATR reading is a dozen fields. Logs are the opposite.
``GET /logs`` accepts ``limit`` up to 5,000, and 500 log lines handed to a model burns a large share
of the context window while conveying almost nothing -- the agent cannot read 500 lines, so the
call makes it *worse* informed than a smaller, grouped answer would.

So the tools here are chosen for what an agent can actually use:

* ``trace_decision`` -- one decision's whole timeline, logs and events merged, compacted.
* ``recent_problems`` -- WARNING and above **grouped** by service and location, with counts, so
  "142 x no ATR for X" is one line rather than 142.
* ``find_logs`` -- a targeted, hard-bounded query when the grouped views are not enough.
* ``logging_status`` -- volume, per-client counts, and whether anything was dropped.

Every one of them reports ``count`` and ``truncated``. A tool that silently returns a slice is worse
than one that refuses, because the agent will believe it saw everything.

NO WRITE TOOL, ON PURPOSE

There is no ``log_message``. Tools here can read the trail; they cannot add to it. A log line is
evidence of what a program did. If an observational agent could write into the same table, its
commentary would be indistinguishable from the events, and the trail would stop being evidence --
which is the only thing it is for. Agents that need to record something do it through their own
application's ``LogClient``, under that application's identity.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8004"
DEFAULT_TIMEOUT = 45.0

#: A model reads a few dozen lines usefully, not a few thousand. These caps are the tool's
#: guardrail, independent of what the REST endpoint would allow.
MAX_ROWS = 200
DEFAULT_ROWS = 50

mcp = FastMCP(
    name="McTrader_Logging",
    instructions=(
        "Read-only access to the estate's log sink: a curated investigation interface, not a log "
        "viewer. Each tool answers a question, so pick by question rather than by filter. "
        "'What is going wrong now?' -> recent_problems. 'What changed?' -> whats_new. 'What "
        "happened in this decision?' -> trace_decision with its correlation id. 'What happened to "
        "this instrument?' -> investigate_symbol. 'Who is logging, and is anything dropping?' -> "
        "logging_status. find_logs is an escape hatch for cases none of those fit, not a starting "
        "point. Results are grouped and bounded on purpose: read the summary rather than asking "
        "for thousands of rows."
    ),
)


def _base_url() -> str:
    return (os.getenv("LOG_MCP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _timeout() -> float:
    try:
        return float(os.getenv("LOG_MCP_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


async def _get(path: str, params: Dict[str, Any]) -> Any:
    """One GET on the REST service, with transport failures turned into a readable reason."""
    url = _base_url() + path
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        return {"error": "service-unreachable", "detail": f"{type(exc).__name__}: {exc}",
                "url": url}
    try:
        payload = response.json()
    except ValueError:
        return {"error": "unparseable", "status": response.status_code,
                "detail": response.text[:300]}
    if response.status_code >= 400:
        if isinstance(payload, dict):
            payload.setdefault("status", response.status_code)
            return payload
        return {"error": "http-error", "status": response.status_code}
    return payload


def _compact_log(row: Dict[str, Any]) -> Dict[str, Any]:
    """One log row reduced to what a reader needs, dropping null fields."""
    out: Dict[str, Any] = {
        "t": (row.get("occurred") or row.get("received") or "")[:23],
        "level": row.get("level_name"),
        "service": row.get("service"),
        "where": row.get("location"),
        "msg": row.get("message"),
    }
    if row.get("symbol"):
        out["symbol"] = row["symbol"]
    if row.get("category"):
        out["category"] = row["category"]
    if row.get("order_id"):
        out["order_id"] = row["order_id"]
    return out


def _compact_event(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "t": (row.get("occurred") or row.get("received") or "")[:23],
        "event": row.get("event_type"),
        "service": row.get("service"),
    }
    if row.get("symbol"):
        out["symbol"] = row["symbol"]
    if row.get("order_id"):
        out["order_id"] = row["order_id"]
    return out


@mcp.tool(description=(
    "Follow ONE decision end to end. Give a correlation id and get every log line and domain "
    "event recorded under it, from every service, in time order. This is the query that was "
    "impossible before this service existed: without a correlation id a decision spanning "
    "Market_Data_Service, the trader and the broker could not be joined. Returns `truncated` if "
    "the timeline exceeded the row cap, so you know you are seeing a slice."
))
async def trace_decision(correlation_id: str, limit: int = 120) -> Dict[str, Any]:
    cap = max(1, min(int(limit), MAX_ROWS))
    logs = await _get("/logs", {"correlation_id": correlation_id, "limit": cap})
    if isinstance(logs, dict) and logs.get("error"):
        return logs
    events = await _get("/events", {"correlation_id": correlation_id, "limit": cap})
    if isinstance(events, dict) and events.get("error"):
        return events

    log_rows = logs.get("rows") or []
    event_rows = events.get("rows") or []

    timeline: List[Dict[str, Any]] = [_compact_log(r) for r in log_rows]
    timeline += [_compact_event(r) for r in event_rows]
    timeline.sort(key=lambda item: item.get("t") or "")

    services = sorted({item.get("service") for item in timeline if item.get("service")})
    levels = Counter(item.get("level") for item in timeline if item.get("level"))

    return {
        "correlation_id": correlation_id,
        "spans": len(timeline),
        "services": services,
        "level_counts": dict(levels),
        "first": timeline[0]["t"] if timeline else None,
        "last": timeline[-1]["t"] if timeline else None,
        "timeline": timeline,
        "truncated": len(log_rows) >= cap or len(event_rows) >= cap,
        "note": None if timeline else (
            "nothing recorded under this id -- either it was never logged, or the rows are "
            "older than the 3-month retention window"),
    }


@mcp.tool(description=(
    "What is currently going wrong, GROUPED. Returns WARNING-and-above activity over the last N "
    "minutes aggregated by service and location, with counts and one sample line each, so 142 "
    "repeats of the same error read as one row with a count. Use this before `find_logs`: it is "
    "far more informative per token than raw rows."
))
async def recent_problems(minutes: int = 60, level: str = "WARNING",
                          limit: int = 200) -> Dict[str, Any]:
    cap = max(1, min(int(limit), MAX_ROWS * 5))
    payload = await _get("/logs", {"level": level, "minutes": int(minutes), "limit": cap})
    if isinstance(payload, dict) and payload.get("error"):
        return payload

    rows = payload.get("rows") or []
    grouped: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row.get("service"), row.get("level_name"), row.get("location"))
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "service": row.get("service"),
                "level": row.get("level_name"),
                "where": row.get("location"),
                "count": 1,
                "first": (row.get("occurred") or "")[:23],
                "last": (row.get("occurred") or "")[:23],
                "sample": row.get("message"),
                "symbols": sorted({row["symbol"]}) if row.get("symbol") else [],
            }
            continue
        entry["count"] += 1
        stamp = (row.get("occurred") or "")[:23]
        entry["first"] = min(entry["first"], stamp) if stamp else entry["first"]
        entry["last"] = max(entry["last"], stamp) if stamp else entry["last"]
        if row.get("symbol") and row["symbol"] not in entry["symbols"]:
            entry["symbols"].append(row["symbol"])

    problems = sorted(grouped.values(), key=lambda e: -e["count"])
    return {
        "window_minutes": int(minutes),
        "min_level": level,
        "total_rows": len(rows),
        "distinct_problems": len(problems),
        "problems": problems,
        "truncated": len(rows) >= cap,
    }


@mcp.tool(description=(
    "WHAT CHANGED? The investigation a raw log dump cannot answer. Returns the problem locations "
    "active in the last `minutes` that were NOT seen in the preceding `baseline_minutes`, with "
    "counts and a sample each. A location that has been firing for weeks and one that started "
    "four minutes ago look identical in any list of errors; the second is almost always the "
    "interesting one. `baseline_truncated` says whether the comparison window was cut short, so "
    "you know if 'new' might just mean 'not in the slice that was read'."
))
async def whats_new(minutes: int = 60, baseline_minutes: int = 1440,
                    level: str = "WARNING", limit: int = 200) -> Dict[str, Any]:
    cap = max(1, min(int(limit), MAX_ROWS))
    lookback = int(minutes) + int(baseline_minutes)
    payload = await _get("/logs", {"level": level, "minutes": lookback, "limit": cap})
    if isinstance(payload, dict) and payload.get("error"):
        return payload

    rows = payload.get("rows") or []
    if not rows:
        return {"window_minutes": int(minutes), "baseline_minutes": int(baseline_minutes),
                "new_problems": [], "note": "no rows in the window or the baseline"}

    # Anchor the split on the newest row rather than the local clock: the service's clock and
    # this process's clock need not agree, and the newest row is the latest thing that happened.
    stamps = sorted((r.get("occurred") or r.get("received") or "") for r in rows)
    newest = stamps[-1]
    try:
        newest_dt = datetime.fromisoformat(newest)
    except ValueError:
        return {"error": "unparseable-timestamp", "detail": newest}
    cutoff = (newest_dt - timedelta(minutes=int(minutes))).isoformat()

    def location_of(row: Dict[str, Any]) -> tuple:
        return (row.get("service"), row.get("location"), row.get("level_name"))

    in_window = [r for r in rows if (r.get("occurred") or "") >= cutoff]
    baseline = [r for r in rows if (r.get("occurred") or "") < cutoff]
    baseline_seen = {location_of(r) for r in baseline}

    fresh: Dict[tuple, Dict[str, Any]] = {}
    for row in in_window:
        key = location_of(row)
        if key in baseline_seen:
            continue
        entry = fresh.get(key)
        if entry is None:
            fresh[key] = {
                "service": row.get("service"), "where": row.get("location"),
                "level": row.get("level_name"), "count": 1,
                "first": (row.get("occurred") or "")[:23],
                "last": (row.get("occurred") or "")[:23],
                "sample": row.get("message"),
                "symbols": sorted({row["symbol"]}) if row.get("symbol") else [],
            }
            continue
        entry["count"] += 1
        stamp = (row.get("occurred") or "")[:23]
        entry["first"] = min(entry["first"], stamp) if stamp else entry["first"]
        entry["last"] = max(entry["last"], stamp) if stamp else entry["last"]
        if row.get("symbol") and row["symbol"] not in entry["symbols"]:
            entry["symbols"].append(row["symbol"])

    return {
        "window_minutes": int(minutes),
        "baseline_minutes": int(baseline_minutes),
        "min_level": level,
        "rows_examined": len(rows),
        "rows_in_window": len(in_window),
        "rows_in_baseline": len(baseline),
        "new_problems": sorted(fresh.values(), key=lambda e: -e["count"]),
        "baseline_truncated": len(rows) >= cap,
    }


@mcp.tool(description=(
    "What happened to ONE INSTRUMENT? Every log line and event for a symbol across all services, "
    "in time order. Use it when a position, an order or a stop is behaving oddly and you need the "
    "whole story for that name rather than for one decision: the data service pricing it, the "
    "scanner selecting it, the trader entering it and the broker filling it all appear together."
))
async def investigate_symbol(symbol: str, minutes: int = 1440,
                             limit: int = 150) -> Dict[str, Any]:
    cap = max(1, min(int(limit), MAX_ROWS))
    logs = await _get("/logs", {"symbol": symbol, "minutes": int(minutes), "limit": cap})
    if isinstance(logs, dict) and logs.get("error"):
        return logs
    events = await _get("/events", {"symbol": symbol, "limit": cap})
    if isinstance(events, dict) and events.get("error"):
        return events

    log_rows = logs.get("rows") or []
    event_rows = events.get("rows") or []
    timeline: List[Dict[str, Any]] = [_compact_log(r) for r in log_rows]
    timeline += [_compact_event(r) for r in event_rows]
    timeline.sort(key=lambda item: item.get("t") or "")

    return {
        "symbol": str(symbol).strip().upper(),
        "window_minutes": int(minutes),
        "spans": len(timeline),
        "services": sorted({i.get("service") for i in timeline if i.get("service")}),
        "level_counts": dict(Counter(i.get("level") for i in timeline if i.get("level"))),
        "first": timeline[0]["t"] if timeline else None,
        "last": timeline[-1]["t"] if timeline else None,
        "timeline": timeline,
        "truncated": len(log_rows) >= cap or len(event_rows) >= cap,
        "note": None if timeline else (
            f"nothing recorded for {symbol} in the last {minutes} minutes -- check the symbol "
            "spelling (it is stored upper-cased, bare) before concluding nothing happened"),
    }


@mcp.tool(description=(
    "ESCAPE HATCH, not the first resort. A targeted, hard-bounded query by correlation id, "
    "symbol, service, minimum level or look-back window, returning compacted rows. Prefer "
    "`trace_decision` for one decision, `investigate_symbol` for one instrument, `whats_new` for "
    "what changed, and `recent_problems` for what is going wrong now -- each of those is far more "
    "informative per token than a filtered list. This exists for the cases they do not fit."
))
async def find_logs(
    correlation_id: Optional[str] = None,
    symbol: Optional[str] = None,
    service: Optional[str] = None,
    level: Optional[str] = None,
    minutes: Optional[int] = None,
    limit: int = DEFAULT_ROWS,
) -> Dict[str, Any]:
    cap = max(1, min(int(limit), MAX_ROWS))
    params: Dict[str, Any] = {"limit": cap}
    if correlation_id:
        params["correlation_id"] = correlation_id
    if symbol:
        params["symbol"] = symbol
    if service:
        params["service"] = service
    if level:
        params["level"] = level
    if minutes:
        params["minutes"] = int(minutes)

    payload = await _get("/logs", params)
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    rows = payload.get("rows") or []
    return {
        "count": len(rows),
        "truncated": len(rows) >= cap,
        "filters": params,
        "rows": [_compact_log(r) for r in rows],
    }


@mcp.tool(description=(
    "Health of the log sink and who is writing to it: whether the database is reachable, total "
    "rows, entries in the last hour, and a per-client breakdown of rows in the last 24 hours. A "
    "client whose 24-hour count has fallen to zero while its total stays put has silently stopped "
    "logging -- which is how a dead app is noticed before somebody goes looking for a record that "
    "was never written."
))
async def logging_status() -> Dict[str, Any]:
    health = await _get("/healthz", {})
    if isinstance(health, dict) and health.get("error"):
        return health
    stats = await _get("/stats", {})
    if isinstance(stats, dict) and stats.get("error"):
        return stats
    database = stats.get("database") or {}
    return {
        "status": health.get("status"),
        "database": health.get("database"),
        "database_ok": health.get("database_ok"),
        "database_error": health.get("database_error"),
        "log_entries": database.get("log_entries"),
        "log_events": database.get("log_events"),
        "entries_last_hour": database.get("entries_last_hour"),
        "latest_entry_at": database.get("latest_entry_at"),
        "clients": database.get("services") or [],
        "retention": "rows older than 3 months are deleted; there is no archive",
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    transport = (os.getenv("LOG_MCP_TRANSPORT", "stdio").strip().lower() or "stdio")
    if transport not in {"stdio", "streamable-http", "sse"}:
        transport = "stdio"
    log.info("McTrader_Logging MCP on %s (transport=%s, read-only)", _base_url(), transport)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
