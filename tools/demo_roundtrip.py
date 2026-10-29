"""End-to-end proof: write lines through the client, read them back by correlation id.

This is the query that dbo.Logs cannot answer today — every line from every service that took
part in one decision, in order, joined by CorrelationId.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging.client import LogClient, correlation

BASE = "http://127.0.0.1:8004"

try:
    health = requests.get(f"{BASE}/healthz", timeout=10).json()
    print("health:", health)
except Exception as exc:  # noqa: BLE001
    raise SystemExit(f"service not reachable on {BASE}: {exc}")

decision_id = f"demo-{uuid.uuid4().hex[:12]}"
print(f"\ncorrelation id: {decision_id}")

# Two 'services' writing inside the same correlation scope, as they would in one decision.
market_data = LogClient(base_url=BASE, service="market_data_service", flush_seconds=0.2)
trader = LogClient(base_url=BASE, service="mini_mctrader", flush_seconds=0.2)

with correlation(decision_id):
    market_data.info("api.post_prices", "priced 2757 symbols",
                     category="price", payload={"requested": 2757, "route": "bulk"})
    market_data.info("api.post_atr", "ATR14 for 6 candidates",
                     category="indicator", payload={"period": 14, "method": "tr"})
    trader.info("slot1_trader.run_entry", "12 movers, 6 selected",
                category="entry", symbol="AXGN")
    trader.event("order.submitted", symbol="AXGN", order_id="demo-order-1",
                 payload={"notional": 2500, "stop_atr_multiple": 1.0})
    trader.warning("slot1_trader.stop_for", "no ATR for SKHU",
                   category="risk", symbol="SKHU")

# Park a line outside the correlation block, to prove the ambient id is not sticky.
trader.info("slot1_trader.exit_cycle", "exit window check", category="exit")

market_data.flush(timeout=10)
trader.flush(timeout=10)
print("market_data client stats:", market_data.stats())
print("trader client stats     :", trader.stats())
market_data.close()
trader.close()

time.sleep(0.5)

# Read back by correlation id across both services.
response = requests.get(f"{BASE}/logs", params={"correlation_id": decision_id, "limit": 50},
                        timeout=20)
payload = response.json()
print(f"\nGET /logs?correlation_id=...  [{response.status_code}] count={payload.get('count')}")
for row in sorted(payload.get("rows", []), key=lambda r: r["id"]):
    print(f"  {row['service']:<22} {row['level_name']:<8} {row['location']:<28} "
          f"{row['message']}")

events = requests.get(f"{BASE}/events", params={"correlation_id": decision_id},
                      timeout=20).json()
print(f"\nGET /events?correlation_id=... count={events.get('count')}")
for row in events.get("rows", []):
    print(f"  {row['event_type']:<20} {row['symbol']} order={row['order_id']} "
          f"payload={row['payload']}")

# Level filtering: WARNING and above.
warn = requests.get(f"{BASE}/logs", params={"level": "WARNING", "minutes": 10},
                    timeout=20).json()
print(f"\nGET /logs?level=WARNING (last 10 min) count={warn.get('count')}")
for row in warn.get("rows", [])[:5]:
    print(f"  {row['level_name']:<8} {row['service']:<22} {row['message']}")

stats = requests.get(f"{BASE}/stats", timeout=20).json()
print("\nservice stats:", stats)
