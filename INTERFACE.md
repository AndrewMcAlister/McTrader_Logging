# McTrader_Logging — interface reference

Everything needed to send logs to this service and read them back. No rationale: request and
response shapes, the client, and the rules that make an integration correct.

- **Base URL:** `http://127.0.0.1:8004`
- **Auth:** none. Localhost only.
- **Content type:** `application/json`
- **OpenAPI:** `GET /docs`
- **Python package:** `mctrader_logging` (project directory `McTrader_Logging`)

---

## 1. The one rule: never block on logging

**Do not await a logging write on a path that places orders.** The HTTP API is synchronous (a 202
means stored), but a client must not make a trading decision wait for it.

Use the provided client, which is fire-and-forget by design — it queues and returns, and a failed
batch is dropped and counted rather than retried or buffered to disk. **The client has no backup
plan, on purpose.** The console copy is the local record.

---

## 2. `POST /log` — batch of log lines

**Request**

```json
{
  "records": [
    {
      "service": "mini_mctrader",
      "location": "slot1_trader.run_entry",
      "message": "12 movers, 6 selected",
      "level": "INFO",
      "correlation_id": "15cb68c3-ba5d-46a1-8468-e1a1684ea10d",
      "cycle_id": "2026-10-29-1030",
      "category": "entry",
      "symbol": "AXGN",
      "broker_id": 1,
      "order_id": "9fab18a7-5e62-4330-85cd-331e7cb7cf7e",
      "occurred_utc": "2026-10-29T14:30:04.328879Z",
      "payload": { "notional": 2500, "stop_atr_multiple": 1.0 }
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `service` | **yes** | which app wrote it, max 64 chars. **Canonicalised**: lowercased with punctuation collapsed to `_`, so `Mini-McTrader` and `mini_mctrader` are one client |
| `location` | **yes** | `module.function`, max 255 chars |
| `message` | no | max 2000 chars; longer is truncated, not refused |
| `level` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (default `INFO`). Case-insensitive; `warn` works. An unknown level becomes `INFO` — the line is kept |
| `correlation_id` | no | **supply it.** UUID, max 64 chars. See §6 |
| `cycle_id` | no | one trading cycle, max 64 |
| `category` | no | `price`, `order`, `risk`, `entry`, `exit`, `startup` |
| `symbol` | no | max 50 |
| `broker_id` | no | integer |
| `order_id` | no | **any string**, max 64 — does not have to be a UUID |
| `occurred_utc` | no | when it happened on *your* clock; the service records its own receive time separately |
| `payload` | no | any JSON value, stored as JSON |

**202 response**

```json
{ "accepted": 1, "rejected": 0, "errors": [] }
```

**Errors**

| Status | Body `error` | Meaning |
|---|---|---|
| 413 | `batch-too-large` | more than 1000 records; batch on the client |
| 503 | `database-busy` | transient; retry |
| 503 | — | nothing could be written; `accepted: 0` |

Writes are chunked server-side, so one malformed record does not cost the rest of the batch.
`rejected` is a count, never a silent drop.

---

## 3. `POST /event` — batch of domain events

For things that happened, as opposed to free-text: `order.submitted`, `order.filled`, `stop.placed`,
`signal.fired`.

**Request**

```json
{
  "events": [
    {
      "service": "mini_mctrader",
      "event_type": "order.submitted",
      "level": "INFO",
      "correlation_id": "15cb68c3-ba5d-46a1-8468-e1a1684ea10d",
      "cycle_id": "2026-10-29-1030",
      "symbol": "AXGN",
      "broker_id": 1,
      "order_id": "9fab18a7-5e62-4330-85cd-331e7cb7cf7e",
      "occurred_utc": "2026-10-29T14:30:22Z",
      "payload": { "notional": 2500, "qty": null, "stop_price": 225.58 }
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `service` | **yes** | |
| `event_type` | **yes** | max 64 |
| `level` | no | defaults to `INFO`. Any level in `dbo.LogLevels`, by name, alias or number. An event that failed or was rejected should say so, or it cannot appear in a "WARNING or above" query |
| `correlation_id`, `cycle_id`, `symbol`, `broker_id`, `order_id`, `occurred_utc`, `payload` | no | as above |

**202 response:** same shape as `/log`.

---

## 4. `GET /logs` — query log lines

| Parameter | Notes |
|---|---|
| `correlation_id` | every line from one decision, across services — the main query |
| `symbol` | |
| `service` | |
| `level` | **minimum** — `WARNING` returns WARNING and above |
| `category` | |
| `minutes` | look back this far. Defaults to 60 when neither `correlation_id` nor `symbol` is given |
| `limit` | default 200, max 5000 |

**200 response**

```json
{
  "count": 2,
  "rows": [
    {
      "id": 42,
      "received_utc": "2026-10-29T04:30:04.412",
      "occurred_utc": "2026-10-29T04:30:04.328",
      "service": "mini_mctrader",
      "correlation_id": "15cb68c3-ba5d-46a1-8468-e1a1684ea10d",
      "cycle_id": "2026-10-29-1030",
      "level": 30,
      "level_name": "WARNING",
      "category": "risk",
      "location": "slot1_trader.stop_for",
      "message": "no ATR for SKHU",
      "symbol": "SKHU",
      "broker_id": 1,
      "order_id": null,
      "payload": "{\"reason\": \"no history\"}"
    }
  ]
}
```

Rows are newest first. `received_utc` is when the service stored it; `occurred_utc` is when the
writer said it happened — a widening gap between them is a backlog. `payload` is a **JSON string**,
not a nested object: parse it if you need it.

---

## 5. `GET /events` — query domain events

Parameters: `correlation_id`, `symbol`, `event_type`, `level`, `minutes`, `limit`.
`level` is a **minimum**, exactly as it is for `/logs`.
Response: `{ "count": n, "rows": [...] }` with the same field conventions, `event_type` in place of
`location`/`level`.

---

## 6. Correlation ids — the reason this service exists

**The client supplies the id; the service never invents one.** It cannot know what operation a caller
is part of, so if you do not send one, no id is recorded and the line cannot be joined.

- **Format: a standard 36-character UUID.**
- **One id per logical operation.** A trading cycle, an entry decision, a reconciliation run.
- Sent as a header (`X-Correlation-Id`) as well as in each record.
- Every record written inside a `correlation(...)` scope inherits it automatically.

**Every write returns the id**, so it can go straight into your own console message:

```
2026-10-29T04:21:03.412Z INFO     mini_mctrader.run_entry  [corr=15cb68c3-ba5d-46a1-8468-e1a1684ea10d] 12 movers symbol=AXGN
```

---

## 6b. Log levels — they are data, in `dbo.LogLevels`

Five levels ship, on the conventional syslog scale. The number compares (`WHERE Level >= 30`), the
name reads, and every row stores both:

| Number | Name | Meaning |
|---|---|---|
| 10 | `DEBUG` | diagnostic detail; off in normal running |
| 20 | `INFO` | something happened as expected |
| 30 | `WARNING` | something unexpected; the work carried on |
| 40 | `ERROR` | an operation failed |
| 50 | `CRITICAL` | the process, or a book, cannot continue |

**`GET /levels`** reports what this service will accept, from memory — i.e. what it has actually
loaded, which is the question a client has:

```json
{"count": 5, "levels": [
  {"level": 10, "name": "DEBUG", "description": "...", "aliases": ["debug","trace","verbose"]},
  {"level": 20, "name": "INFO",  "description": "...", "aliases": ["info","information","notice"]}
]}
```

**Adding a level is an INSERT, not a release.** Insert a row into `dbo.LogLevels` and it is accepted
after the next restart. `Aliases` is a comma-separated list, so `warn` keeps meaning `WARNING`
without every client carrying its own mapping.

**An unknown level is never an error on the way in.** `warnn` is stored as `INFO` and the line is
kept — losing a log line because of its level would lose exactly the message someone was trying to
record. (The one place an unknown level *is* rejected is `PUT /services/{service}/level`, because
that call decides what gets destroyed; see §6c.)

**There is no foreign key** from `LogEntry`/`LogEvent` to `LogLevels`, deliberately. The five
built-ins also live in code, so a write never depends on reading the table. An FK would reintroduce
that dependency: delete a level row and inserts start being refused.

**Events carry a level too.** Every `LogEvent` row has `Level`/`LevelName`, defaulting to `INFO`. The
fields are on `POST /event` and on the response from `GET /events`:

```
POST /event   {"events": [{"service": "mini_mctrader", "event_type": "order.rejected",
                           "level": "WARNING", ...}]}
GET /events?level=WARNING
```

```
| `level` | no | defaults to `INFO`. Any level in `dbo.LogLevels`, by name, alias or number |
```

An event that failed or was rejected should say so — otherwise it cannot appear in a "WARNING or
above" query, and the two paths would need two different notions of severity.

---

## 6c. Recording thresholds — what actually gets written

Setting a client's threshold stops **storing** its log lines below that level. This is a write-side
filter: the row is never inserted, so it saves the INSERT and not just the read. That is the point of
it, and also what makes it the most dangerous control here — a discarded row does not exist
afterwards.

```
PUT    /services/{service}/level      {"level": "ERROR", "reason": "too noisy"}
GET    /services/{service}/level
GET    /services
DELETE /services/{service}/level
```

**Set it**

```powershell
Invoke-RestMethod -Method Put http://127.0.0.1:8004/services/mini_mctrader/level `
  -ContentType 'application/json' `
  -Body '{"level":"ERROR","reason":"volume spike during the 10:30 scan"}'
```

```json
{"service": "mini_mctrader", "min_level": 40, "min_level_name": "ERROR",
 "records": "ERROR and above", "affects_events": false,
 "reason": "volume spike during the 10:30 scan", "updated_by": "127.0.0.1"}
```

**Report it**

```json
{"service": "mini_mctrader", "min_level": 40, "min_level_name": "ERROR",
 "records": "ERROR and above", "affects_events": false, "suppressed_since_start": 3}
```

`GET /services` lists every client with a threshold, plus anything suppressed since the process
started. It answers from memory, so it describes what the service is doing — not what the table would
do after a restart.

**Clear it** — `DELETE` removes the row, so everything is recorded again.

### The rules, and why each one

| Rule | Why |
|---|---|
| No row means **record everything** | A migration must not decide to start dropping DEBUG lines. Nothing is suppressed until someone says so |
| The comparison is **inclusive** (`>=`) | Setting a client to `ERROR` must still record ERROR, or the setting would hide its own level |
| A name, an alias or a number is accepted; an **unknown level is a 400** | `warn` works. `warnn` is refused rather than silently becoming INFO — this call decides what data is destroyed, so a typo must not be guessed at |
| **Events are never suppressed** | `order.submitted` is an INFO event. Applying a diagnostic threshold to events would stop a client recording its own orders — deleting the audit trail in the name of reducing noise |
| The write is **reported** as `suppressed` | A client whose lines are being dropped needs to know, or it will believe it has a record of something the sink never kept |
| `Reason` / `UpdatedBy` / `UpdatedUtc` are stored | "Why did the log go quiet on the 29th" needs an answer in the data, not in someone's memory |

### What a suppressed write looks like

```
POST /log  {"records": [DEBUG, INFO, WARNING, ERROR]}   ->  202
           {"accepted": 1, "rejected": 0, "suppressed": 3, "errors": []}
```

A batch that is **entirely** suppressed returns `202` with `accepted: 0` and `suppressed: N`, not a
503: nothing was refused, there was simply nothing above the floor to keep.

### The catch worth knowing

Suppression applies at write time, so raising a client to `ERROR` and then back to `DEBUG` does not
recover what was dropped. The service's own log file records the change:

```
WARNING  mctrader_logging.api  recording threshold for mini_mctrader set to ERROR (volume spike) by 127.0.0.1
```

## 7. Ops

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | `{"status","version","database","database_ok","database_error"}` |
| `GET /stats` | `log_entries`, `log_events`, `entries_last_hour`, `latest_entry_at` |

Retention deletes rows older than 3 months. There is no archive: a pruned row is gone.

---

## 8. The Python client — use this, do not hand-roll the HTTP

```python
from mctrader_logging.client import LogClient, correlation

log = LogClient("http://127.0.0.1:8004", service="mini_mctrader", echo=print)

with correlation("cycle-2026-10-29-1030"):          # every line inside inherits the id
    cid = log.info("slot1_trader.run_entry", "12 movers, 6 selected",
                   category="entry", symbol="AXGN")
    log.event("order.submitted", symbol="AXGN", order_id="9fab18a7",
              notional=2500, stop_atr_multiple=1.0)
    log.warning("slot1_trader.stop_for", "no ATR for SKHU", category="risk", symbol="SKHU")

log.close()      # one bounded flush attempt; never hangs
```

| Constructor argument | Default | Notes |
|---|---|---|
| `service` | `"unknown"` | the value stored in every record |
| `echo` | `None` | a callable for the console copy — `print`, `logging.info`, a file writer |
| `batch_size` | `50` | records per request |
| `flush_seconds` | `2.0` | background drain interval |
| `max_queue` | `10_000` | beyond this the **newest** line is dropped and counted |
| `enabled` | `True` | `False` makes every call a no-op |
| `timeout` | `5.0` | per HTTP request, on the background thread only |

Methods: `debug/info/warning/error/critical(location, message, **fields)` and `event(event_type, **fields)`.
All return the correlation id. `log(...)` takes an explicit level.
`stats()` returns `{"pending", "queued", "sent", "dropped", "failed", "batches", "last_error"}`.

**`payload` handling.** All three of these are accepted and produce the same stored payload:

```python
log.event("x", notional=2500)                    # -> {"notional": 2500}
log.event("x", payload={"notional": 2500})       # -> {"notional": 2500}
log.event("x", payload={"a": 1}, b=2)            # -> {"payload": {"a": 1}, "b": 2}
```

---

## 9. Traps

1. **Do not build a retry or spool of your own.** The client drops and counts. A queue of unsent
   logs on disk is a second problem, not a solution.
2. **`order_id` is a string, not a UUID.** Broker ids that are not UUIDs are fine.
3. **`payload` comes back as a JSON string** from `GET /logs`, not as an object.
4. **`level` in a query is a minimum**, not an exact match.
5. **A line with a bad level is kept**, not rejected — it becomes `INFO`.
6. **`dropped > 0` in `stats()` is the signal you lost lines.** There is no file to recover them from.
7. **`service` and `location` are the only required fields.** Without them the row cannot be
   attributed and the call is a 422.

---

## 10. Minimal raw-HTTP client (if the package is unavailable)

```python
import uuid
from datetime import datetime, timezone
import requests

BASE = "http://127.0.0.1:8004"
CID = str(uuid.uuid4())          # one per logical operation, standard UUID

def log_line(location, message, level="INFO", **fields):
    requests.post(f"{BASE}/log", json={"records": [{
        "service": "myapp",
        "location": location,
        "message": message,
        "level": level,
        "correlation_id": CID,
        "occurred_utc": datetime.now(timezone.utc).isoformat(),
        "payload": fields or None,
    }]}, headers={"X-Correlation-Id": CID}, timeout=5)
```

Wrap that in a queue and a thread, or simply use the packaged client — which already is one.
