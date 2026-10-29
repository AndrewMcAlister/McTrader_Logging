# McTrader_Logging

One log sink for every service, and the one column that makes a trading decision traceable
across them.

```
McTrader ────────────┐
PaperTrader ─────────┤
PortfolioAdvisor ────┼──▶  McTrader_Logging :8004  ──▶  Logging.dbo.LogEntry
Mini-McTrader ───────┤         ▲                        Logging.dbo.LogEvent
Market_Data_Service ─┘         │
                     X-Correlation-Id on every request
```

**Why it exists.** `dbo.Logs` already holds 101,336 rows with
`Id, Received, Location, Description, Log_Level_Id, Message, Broker_Id, Symbol, Order_Id` — and
**no correlation ID**. So one trading decision cannot be followed across the processes that
took part in it. That is already a gap; after the micro-service split, a single decision spans
Market_Data_Service → trader → broker in three processes, and the trail becomes structurally
unjoinable. `dbo.BrokerEvents` exists with **0 rows**, which is what happens when event logging
lives inside the app that is busy trading.

---

## The one contract that is not negotiable

Market data has a fallback. **Logging does not.** If a trader blocks on a log write to record
"about to place this order", a logging outage stops trading — worse than the thing it was
observing.

So `client.py` is fire-and-forget by construction:

| Guarantee | How |
|---|---|
| **Never blocks** | `info()`/`warning()`/`error()`/`event()` append to a list and return. No I/O, no sleep, no lock held across a request. |
| **Never raises** | Every writer is wrapped; a logging failure can never become a trading failure. |
| **Bounded memory** | Queue caps at `max_queue`; the **newest** line is dropped and counted. Dropping is correct under pressure: a log line that costs a trade is a bad trade. |
| **No backup plan** | A refused batch is **dropped and counted**. There is no spool file and no replay: a client that quietly accumulates a disk of unsent logs is a second problem wearing the solution's clothes. The console copy — which the application owns — is the local record. |
| **Shutdown is bounded** | `close()` makes one bounded flush attempt and gives up. A process that will not exit is worse than a few lost lines. |
| **Loss is visible** | `stats()` reports `dropped` and `failed`. "No logs for an hour" shows as `dropped > 0`, not as an absence. |

Measured: 100 lines against a service that does not exist complete in **under one second**, and
`flush()` reports `failed: 100` and `dropped: 100` rather than raising.

---

## Using it

```python
from mctrader_logging.client import LogClient, correlation

log = LogClient("http://127.0.0.1:8004", service="mini_mctrader")

with correlation("decision-2026-10-29-AAPL"):
    log.info("slot1_trader.run_entry", "12 movers, 6 selected", category="entry")
    log.event("order.submitted", symbol="AXGN", order_id="9fab18a7",
              notional=2500, stop_atr_multiple=1.0)
    log.warning("slot1_trader.stop_for", "no ATR for SKHU", symbol="SKHU")
```

Every line written inside the `with` block inherits the correlation id, so a decision's trail is
joined by a query rather than by threading an argument through every call site.

Every write **returns the correlation id**, so it can go into the caller's own console message:

```python
line = log.info("slot1_trader.run_entry", "12 movers, 6 selected", symbol="AXGN")
print(f"{line}")            # or: log = LogClient(..., echo=print)
```

    >>> log.info("run_entry", "12 movers", symbol="AXGN")
    2026-10-29T04:21:03.412Z INFO     mini_mctrader.run_entry  [corr=15cb68c3-ba5d-46a1-8468-e1a1684ea10d] 12 movers symbol=AXGN

### The id is the client's to supply, and it is a UUID

The service cannot know what operation a caller is part of, so it **stores what it is given and
never invents one**. Each `LogClient` mints its own standard 36-character UUID at construction and
falls back to it whenever no scope is open, so a line is never written without one:

```python
client = LogClient(..., service="mini_mctrader")
client.info("m.f", "started")               # the client's own UUID
with correlation("cycle-1030"):
    client.info("m.f", "in the cycle")       # cycle-1030
client.info("m.f", "after the cycle")        # back to the client's own UUID
```

`correlation()` with no argument generates one, which is the common case: open a scope per trading
cycle and every line inside it is joinable.

### The client name is captured, canonical, and indexed

Every row records **which client wrote it** in `Service`, and the field is required — a record
without one is a 422, because a line that cannot be attributed is indistinguishable from noise.

It is **canonicalised at the boundary**: lowercased, with punctuation collapsed to `_`. So
`Mini-McTrader`, `mini-mctrader`, `MINI_MCTRADER` and `mini_mctrader` all become one service.

That is not tidiness. Measured before the fix, those four spellings were stored as **four distinct
services**, so `WHERE Service = 'mini_mctrader'` returned a quarter of that app's rows — the field
existed to answer "which client wrote this" and could not.

Normalised rather than rejected: a client with a stale spelling should still get its line recorded,
and normalising is server-side so a raw-HTTP client gets it too. `Service` is indexed
`(Service, ReceivedUtc DESC)`, so filtering by client and the `/stats` breakdown are seeks rather
than scans of a table that only grows.

`GET /stats` reports it directly:

```json
"services": [
  { "service": "mini_mctrader", "rows_total": 1284, "rows_24h": 210,
    "latest": "2026-10-29T04:41:12.004" }
]
```

That is also how a client that has silently stopped logging becomes visible: its `rows_24h` goes
to zero while its `rows_total` stays put.

Then, afterwards:

```
GET /logs?correlation_id=decision-2026-10-29-AAPL
GET /events?correlation_id=decision-2026-10-29-AAPL
```

`occurred_utc` is the writer's clock; `received_utc` is when this service stored it. A widening
gap between them is a backlog, which is why both are kept.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/log` | Append a batch of log lines → **202** |
| `POST` | `/event` | Append a batch of domain events → **202** |
| `GET` | `/logs` | Query by `correlation_id`, `symbol`, `service`, `level` (minimum), `category`, `minutes`, `limit` |
| `GET` | `/events` | Query by `correlation_id`, `symbol`, `event_type`, `minutes`, `limit` |
| `GET` | `/healthz` | Alive, and can it reach its database |
| `GET` | `/stats` | Row counts, entries in the last hour, and a **per-client breakdown** |
| `GET` | `/docs` | OpenAPI |

**202, not 200.** The batch is written before the response, so `accepted` really means stored —
but the status says "accepted", because the client's contract is fire-and-forget and a caller
should not treat the round trip as part of its own latency.

Endpoints are plain `def`, not `async def`: every one calls blocking pyodbc, and a blocking call
inside an `async def` serialises the whole event loop. That exact mistake made a health check in
Market_Data_Service hang for 90 seconds.

---

## MCP wrapper — read-only, and narrower than the REST API

An agent asked "what happened in that decision?" needs to read the trail, so the service has an MCP
surface. It is deliberately **not** a thin proxy, unlike Market_Data_Service's.

That service's wrapper forwards requests unchanged because its answers are small and shaped: a price
is one object, an ATR reading a dozen fields. Logs are the opposite — `GET /logs` accepts `limit` up
to 5,000, and 500 lines handed to a model burns a large share of the context window while conveying
almost nothing. So the tools are chosen for what an agent can actually use:

Each tool answers a **question**, not a filter. An agent picks by question:

| Question | Tool |
|---|---|
| What is going wrong now? | `recent_problems(minutes, level)` — WARNING+ grouped by service and location, so 142 repeats read as one row |
| **What changed?** | `whats_new(minutes, baseline_minutes)` — problems active in the window that were **absent from the preceding baseline**. A location firing for weeks and one that started four minutes ago look identical in any list of errors; the second is almost always the one that matters |
| **What happened to this instrument?** | `investigate_symbol(symbol, minutes)` — every line and event for one name across all services, in order |
| What happened in this decision? | `trace_decision(correlation_id)` — logs and events merged into one ordered timeline |
| Who is logging, and is anything dropping? | `logging_status()` — health, volume, per-client breakdown |
| None of the above | `find_logs(...)` — an **escape hatch**, hard-bounded to 200 rows, explicitly not a starting point |

`whats_new` anchors its window on the newest row rather than the local clock, since the service's
clock and the caller's need not agree, and reports `baseline_truncated` so "new" is never a
truncation artefact.

Every tool reports `count` and `truncated`. A tool that silently returns a slice is worse than one
that refuses, because the agent will believe it saw everything.

```powershell
python -m mctrader_logging.mcp_server                          # stdio, for a local client
$env:LOG_MCP_TRANSPORT = 'streamable-http'                     # for a remote one
python -m mctrader_logging.mcp_server
```

The REST service must already be running — this is a client of it. `LOG_MCP_BASE_URL` (default
`http://127.0.0.1:8004`) points it elsewhere. It is **not** installed as a Windows service: stdio
MCP servers are launched by the client that uses them, unlike the API.

### No write tool, on purpose

There is no `log_message`. Tools here can read the trail; they cannot add to it.

A log line is evidence of what a program did. If an observational agent could write into the same
table, its commentary would be indistinguishable from the events and the trail would stop being
evidence — which is the only thing it is for. An agent that needs to record something does so
through its own application's `LogClient`, under that application's identity.

---

## Tables

Three tables in the `Logging` database, all created on startup if absent.

| Table | Holds |
|---|---|
| `LogEntry` | one row per log line |
| `LogEvent` | one row per domain event (order submitted, stop placed) — now with a level too |
| `LogLevels` | the catalogue of levels: number, name, description, aliases |
| `ServiceLogLevel` | per-client recording threshold; an absent row means "record everything" |

### `LogLevels` — levels are data, not code

Five levels ship, on the conventional syslog scale. Numbers compare, names read, and every row
stores both:

| Number | Name | Meaning |
|---|---|---|
| 10 | `DEBUG` | diagnostic detail; off in normal running |
| 20 | `INFO` | something happened as expected |
| 30 | `WARNING` | something unexpected; the work carried on |
| 40 | `ERROR` | an operation failed |
| 50 | `CRITICAL` | the process, or a book, cannot continue |

**Adding a level is an INSERT, not a release.** Insert a row and it is accepted after the next
restart. `Aliases` is a comma-separated list, so `warn` keeps meaning `WARNING` without every client
carrying its own mapping. `GET /levels` reports what is actually loaded.

The service reads the table once at startup and keeps it in memory; the write path never queries for
it. If the table cannot be read, the five built-ins in code are already in force, so a catalogue
problem degrades the vocabulary rather than stopping the service.

**There is no foreign key** from `LogEntry`/`LogEvent` to `LogLevels`, deliberately. With an FK,
deleting a level row or reading a catalogue that disagrees with a writer would start refusing
inserts — a log sink that can silently discard a line is not a log sink. Values are normalised in
code instead.

### `ServiceLogLevel` — the recording threshold

Turning the volume down for one noisy client:

```powershell
Invoke-RestMethod -Method Put http://127.0.0.1:8004/services/mini_mctrader/level `
  -ContentType 'application/json' -Body '{"level":"ERROR","reason":"volume spike"}'
```

From then on that client's `DEBUG`/`INFO`/`WARNING` lines are **not stored**; `ERROR` and above are.
`{"level":"DEBUG"}` records everything again, and `DELETE /services/{service}/level` removes the
threshold entirely.

Three deliberate constraints:

- **Events are exempt.** `order.submitted` is an INFO event, so attributing a diagnostic threshold to
  events would stop a client recording its own orders — deleting the audit trail to reduce noise.
- **No row means record everything.** A migration must not quietly decide to start dropping lines.
- **The drop is reported**, as `suppressed` on the `POST /log` response and in `GET /services`. A
  client whose lines are being discarded needs to know.

`GET /services` reports per-client thresholds and suppression counts since start; `GET /levels`
reports the catalogue. Full contract in `INTERFACE.md` §6b–6c.


## Starting at boot

Started by **Task Scheduler**, as your interactive account — the same way McTrader is started.

> **`.cmd` or `.ps1` — both work.** The execution policy was `Restricted`, which refused `.ps1` files
> outright; it is now `RemoteSigned` for the current user (`Set-ExecutionPolicy -Scope CurrentUser
> RemoteSigned`, no elevation required), so either form runs.
>
> The `.cmd` wrappers remain as a fallback for `cmd.exe` and for anything that runs outside your
> profile — a scheduled task under another account, for instance, would not see a `CurrentUser`
> setting. The task actions registered by `install-task.ps1` pass `-ExecutionPolicy Bypass` for the
> same reason: they should not depend on a policy in someone else's registry hive.

```cmd
install-task.cmd -Status
install-task.cmd -Install     :: from an ELEVATED prompt; it asks for your password once
install-task.cmd -Run
install-task.cmd -Remove
```

Task Scheduler stores the password; this script never writes it anywhere.

`LogonType` is `Password`, matching its sibling service. This one needs no vendor key, so `S4U` would
also have worked — but an S4U task runs *without* your credential vault, and keeping one pattern for
both services is worth more than the distinction.

### Why a repeating trigger as well as boot

A boot trigger starts the service once and never notices it die. `Ensure-LoggingService.ps1` runs every
10 minutes and is idempotent: it judges readiness by **port** (8004, unambiguous — two projects could
both be `-m ...service`), and it caps starts at 6/hour so a service crashing on startup leaves a record
in `logs\ensure-history.txt` rather than an invisible relaunch loop.

For a log sink the failure is quiet: nothing on the trading path breaks, so nobody notices it is down
until they go looking for a record of an incident that is now unrecoverable.

### Restarting it — and the trap

```powershell
# elevated
cd <project>
.\install-task.ps1 -Restart
```

It stops whatever holds the port, then starts the service through the same launcher the task uses.

**Task Scheduler's "End" does not stop this service.** It ends the *task*, and the launcher starts the
service *detached* — deliberately, so a task that finishes in two seconds does not take the service
down with it. The process therefore survives, and the next scheduled run finds the port held:

```
16:53:36  ensure: port=8004 pid=22300
16:53:37  already listening on 8004 -- nothing to do      <- result 0x0
```

That reports success and changes nothing, which is the worst of both. Measured on 2026-10-29 while
restarting after a code change: the task ran, returned `0x0`, and the same process (created 16:29) kept
serving the old code.

Use `-Restart`, or stop the process by port first. It needs elevation because the task runs at
`RunLevel Highest`:

```powershell
Get-NetTCPConnection -LocalPort <port> -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

**How to tell a restart actually happened.** The process id changes, and the service log gets a new
`starting ...` line with a fresh `pid=`. Checking only the task's result code will not tell you.

### The alternative: a real Windows service

`install-service.ps1` (NSSM) is still here for a LocalSystem deployment. It costs nothing for this
service — no vendor key is involved — but the task-based route keeps both services identical.

> **`.cmd`, not `.ps1`.** This machine's PowerShell execution policy is `Restricted`, so a `.ps1` file
> is refused before it runs — `File ... cannot be loaded because running scripts is disabled`. The
> `.cmd` wrappers exist for that: they pass `-ExecutionPolicy Bypass` for one process instead of
> changing a setting for your whole account.
>
> If you would rather run `.ps1` directly everywhere, `Set-ExecutionPolicy -Scope CurrentUser
> RemoteSigned` does it without elevation and is a common setting (local scripts run, downloaded ones
> must be signed). That is your call, not something these scripts change for you.

```cmd
install-service.cmd -Status
install-service.cmd -Install     :: from an ELEVATED prompt
install-service.cmd -Remove      :: elevated
```

`.cmd` and not `.ps1` because this machine's PowerShell execution policy is `Restricted`, which
refuses `.ps1` files outright. The wrapper passes `-ExecutionPolicy Bypass` for one process rather
than changing a security setting for the whole account.

| Setting | Value | Why |
|---|---|---|
| Start | `SERVICE_AUTO_START` | the point of the exercise |
| Account | `LocalSystem` | no password stored anywhere (see below) |
| DependOnService | `MSSQLSERVER` | the app calls `ensure_schema()` on startup |
| AppExit | `Restart`, delay 5 s, throttle 10 s | a crash is retried, without a tight loop |
| AppStopMethodConsole | 15 s | in-flight requests finish on stop |
| AppStdout/AppStderr | `logs\nssm-*.log`, rotated 10 MB | a crash *before* file logging is up still leaves a trace |

### The service account and SQL Server

A service with no `ObjectName` runs as `NT AUTHORITY\SYSTEM`, and SQL Server sees **that** identity —
not the account you are logged in as. SYSTEM had a login here but no database users, so the service
would have installed cleanly and then failed to connect on every start.

Fixed by grant, not by password:

```
Logging: db_datareader, db_datawriter
```

Least privilege, and no credential on disk. If you would rather run it as an interactive account,
`install-service.cmd -Install -AsUser BIGMACSHOME\AndrewBot` prompts for the password once — but
that account has far more rights than this service needs.

### What a service has that a console run does not

`mctrader_logging/service.py` exists because a Windows service has **no console**: anything written
to stdout/stderr goes nowhere, so a service logging only to the stream looks equally healthy whether
it is working or dead. It logs to `logs\mctrader_logging.log` (rotated 10 MB × 5) and passes
`log_config=None` to uvicorn so uvicorn's own lines reach that file too.

Run it by hand to check before installing:

```powershell
.\.venv\Scripts\python.exe -m mctrader_logging.service
```

---

## Running

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\run.ps1 -Schema   # create dbo.LogEntry / dbo.LogEvent (idempotent, includes migrations)
.\run.ps1 -Test     # 20 tests, no database and no network
.\run.ps1 -Demo     # write lines through the client, read them back by correlation id
.\run.ps1           # serve on 127.0.0.1:8004
```

Configuration is all environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `LOG_DATABASE` | `Logging` | target database |
| `LOG_DB_SERVER` | `localhost` | SQL Server |
| `LOG_MAX_BATCH` | `1000` | rows one request may carry |
| `LOG_MAX_QUERY_ROWS` | `5000` | rows a query may return |
| `LOG_DEFAULT_QUERY_MINUTES` | `60` | window applied when no filter is given |
| `LOG_PORT` | `8004` | port |
| `LOG_HOST` | `127.0.0.1` | bind address — localhost only |

**Why its own database.** Logs are high-volume, append-only and grow without bound; they deserve
their own recovery model and backup schedule rather than sharing one with a trading database. Kept
apart, a retention delete or a bulk log query also cannot hold a lock that matters to a position.

The service owns `Logging.dbo.LogEntry` and `Logging.dbo.LogEvent`. McTrader's own `dbo.Logs` stays
in `McTrader_Live` and is not touched by anything here — it keeps its existing writers until each
app is migrated, and the two can be UNIONed during the transition.

---

## Retention: 3 months

`jobs/retention.py` prunes the sink. **Dry-run by default**, because retention is the one thing here
that destroys data and the window is a policy rather than a fact.

```powershell
python -m jobs.retention                 # report what would go
python -m jobs.retention --apply         # delete it
python -m jobs.retention --months 3 --apply
```

- **Batched deletes** (`TOP (n)` per transaction). One `DELETE` over a few million rows grows the
  log by as much as the table shrinks, holds locks for the whole run, and rolls back entirely on a
  cancellation.
- **No archive.** Retention deletes; there is no second copy and no JSONL export. An archive is a
  second store with its own lifetime, restore path and retention question, and keeping one "just in
  case" is how a log sink becomes two systems. If a decision from four months ago needs explaining,
  its record belongs somewhere meant to keep it — not in a file this job wrote on the way past.
- Schedule it monthly. It is not wired to a scheduler because the cadence is an operational choice.

---

## What this does not do

- **No local buffering beyond the in-memory queue.** SQL is the sink; the console copy belongs to
the application. Nothing is written to disk by this client.
- **No alerting.** `entries_last_hour`, the per-client `rows_24h` in `/stats` and the client's
  `dropped` counter are the raw material;
  alerting belongs in whatever watches the estate, not in the sink.
- **No authentication.** Localhost only, like the other services here. Put it behind the same
  perimeter as McTrader if that ever changes.
- **Does not read the vendor's EODHD counter.** Accounting for vendor spend stays in
  `McTrader_Live.dbo.EodhdUsage`; this service records behaviour, not billing.
