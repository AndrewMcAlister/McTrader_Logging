"""The client's contract: fire and forget, and always hand back the correlation id.

Every test here is about a failure mode of logging itself, because logging is the one dependency
in this estate with no fallback: if it blocks, trading stops. There is deliberately no test for
"spool and replay" -- that was removed because the client is not supposed to have a backup plan.
The console copy, owned by the application, is the local record.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging.client import (
    LogClient, correlation, current_correlation, format_line, new_correlation,
)
from mctrader_logging.levels import Level, level_name, level_number, parse_level


class FakeResponse:
    def __init__(self, status_code=202, text="") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    """Records what was posted; can be told to refuse."""

    def __init__(self, fail: bool = False, status: int = 202) -> None:
        self.fail = fail
        self.status = status
        self.posts = []

    def post(self, url, json=None, timeout=None, headers=None):
        if self.fail:
            raise ConnectionError("service unreachable")
        self.posts.append({"url": url, "body": json, "headers": headers or {}})
        return FakeResponse(self.status)


def _client(tmp_path=None, **kwargs) -> LogClient:
    defaults = dict(
        base_url="http://127.0.0.1:8004", service="test",
        batch_size=50, flush_seconds=30.0, max_queue=100, session=FakeSession(),
    )
    defaults.update(kwargs)
    return LogClient(**defaults)


def _is_uuid(value: str) -> bool:
    """True when *value* is a standard 36-character UUID."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    groups = value.split("-")
    if [len(g) for g in groups] != [8, 4, 4, 4, 12]:
        return False
    return all(all(c in "0123456789abcdef" for c in group.lower()) for group in groups)


# ---------------------------------------------------------------------------
# levels
# ---------------------------------------------------------------------------

def test_levels_are_the_conventional_syslog_numbers():
    assert level_number("DEBUG") == 10
    assert level_number("INFO") == 20
    assert level_number("warn") == 30
    assert level_number("error") == 40
    assert level_number("CRITICAL") == 50


def test_a_mistyped_level_falls_back_rather_than_losing_the_line():
    """A line with a bad level is still a line, and it may be the important one."""
    assert parse_level("warnn") is Level.INFO
    assert parse_level(None) is Level.INFO
    assert parse_level("") is Level.INFO


def test_a_numeric_level_between_two_known_ones_lands_on_the_nearest():
    assert parse_level(25) is Level.INFO
    assert parse_level(35) is Level.WARNING


def test_level_name_is_canonical():
    assert level_name("warn") == "WARNING"
    assert level_name(40) == "ERROR"


# ---------------------------------------------------------------------------
# correlation — the returned id is the feature
# ---------------------------------------------------------------------------

def test_a_write_returns_the_correlation_id_it_carried():
    client = _client()
    with correlation("decision-42"):
        assert client.info("m.f", "line") == "decision-42"


def test_a_write_outside_a_scope_still_carries_a_uuid_minted_by_the_client():
    """The service never invents an id, so the client must always supply one."""
    client = _client()
    returned = client.info("m.f", "line")
    assert returned == client.default_correlation
    assert _is_uuid(returned)


def test_each_client_mints_its_own_id():
    first, second = _client(), _client()
    assert first.default_correlation != second.default_correlation
    assert _is_uuid(first.default_correlation)


def test_the_scope_reverts_to_the_clients_own_id_afterwards():
    client = _client()
    with correlation("cycle-1"):
        assert client.info("m.f", "inside") == "cycle-1"
    assert client.info("m.f", "after") == client.default_correlation


def test_an_explicit_correlation_id_overrides_the_ambient_one():
    client = _client()
    with correlation("ambient"):
        assert client.info("m.f", "explicit wins", correlation_id="explicit") == "explicit"


def test_correlation_generates_an_id_when_given_none():
    with correlation() as generated:
        assert generated
        assert current_correlation() == generated
    assert current_correlation() is None


def test_correlation_is_restored_after_a_nested_scope():
    with correlation("outer"):
        with correlation("inner"):
            assert current_correlation() == "inner"
        assert current_correlation() == "outer"


def test_new_correlation_returns_a_standard_uuid():
    """A UUID, not a shortened token: an id is quoted by a human and may be stored by a machine."""
    value = new_correlation()
    assert _is_uuid(value)
    assert len(value) == 36
    assert value.count("-") == 4
    assert new_correlation() != new_correlation()


def test_the_correlation_id_travels_as_a_header_too():
    session = FakeSession()
    client = _client(session=session)
    with correlation("hdr-1"):
        client.info("m.f", "line")
        client.flush()
    assert session.posts[0]["headers"].get("X-Correlation-Id") == "hdr-1"


# ---------------------------------------------------------------------------
# the console copy: the client renders it, the app owns the sink
# ---------------------------------------------------------------------------

def test_format_line_carries_the_correlation_id_so_a_human_can_quote_it():
    line = format_line({
        "occurred": "2026-10-29T04:21:03.412Z", "level_name": "WARNING",
        "service": "mini_mctrader", "location": "slot1_trader.stop_for",
        "correlation_id": "abc123", "message": "no ATR for SKHU", "symbol": "SKHU",
    })
    assert "[corr=abc123]" in line
    assert "WARNING" in line
    assert "no ATR for SKHU" in line
    assert "symbol=SKHU" in line


def test_format_line_tolerates_a_record_with_no_correlation_id():
    line = format_line({"service": "s", "location": "l", "message": "m"})
    assert "[corr=-]" in line


def test_an_echo_sink_receives_the_formatted_line():
    seen = []
    client = _client(echo=seen.append)
    with correlation("echo-1"):
        client.info("m.f", "hello", symbol="AAPL")
    assert len(seen) == 1
    assert "[corr=echo-1]" in seen[0]
    assert "hello" in seen[0]


def test_a_broken_echo_sink_cannot_break_the_caller():
    def boom(_line):
        raise RuntimeError("console is broken")

    client = _client(echo=boom)
    with correlation("x"):
        assert client.info("m.f", "line") == "x"


# ---------------------------------------------------------------------------
# fire and forget
# ---------------------------------------------------------------------------

def test_a_log_call_returns_immediately_even_when_the_service_is_unreachable():
    client = _client(session=FakeSession(fail=True))
    started = time.perf_counter()
    for index in range(100):
        client.info("m.f", f"line {index}")
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0


def test_a_dead_service_never_raises_at_the_caller():
    client = _client(session=FakeSession(fail=True))
    client.info("m.f", "line")
    client.event("order.submitted", symbol="AAPL")
    result = client.flush()
    assert result["failed"] == 2
    stats = client.stats()
    assert stats["failed"] == 2
    # Fire and forget: a failed batch is dropped and counted, not retained.
    assert stats["dropped"] == 2
    assert stats["pending"] == 0


def test_the_queue_is_bounded_and_the_loss_is_counted():
    """Under pressure the newest line is dropped and counted -- never a trade traded for it."""
    client = _client(max_queue=5, session=FakeSession(fail=True))
    for index in range(20):
        client.info("m.f", f"line {index}")
    stats = client.stats()
    assert stats["pending"] == 5
    assert stats["dropped"] == 15


def test_disabled_client_does_nothing_and_costs_nothing():
    session = FakeSession()
    client = _client(session=session, enabled=False)
    with correlation("off-1"):
        assert client.info("m.f", "not sent") == "off-1"
    client.flush()
    assert session.posts == []
    assert client.stats()["pending"] == 0


def test_close_is_bounded_and_does_not_hang_on_a_dead_service():
    client = _client(session=FakeSession(fail=True))
    client.info("m.f", "line")
    started = time.perf_counter()
    client.close(timeout=0.5)
    assert time.perf_counter() - started < 5.0


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------

def test_lines_are_sent_in_batches_not_one_request_each():
    session = FakeSession()
    client = _client(session=session, batch_size=25)
    for index in range(60):
        client.info("m.f", f"line {index}")
    client.flush()
    assert len(session.posts) == 3          # 25 + 25 + 10
    total = sum(len(post["body"]["records"]) for post in session.posts)
    assert total == 60


def test_a_record_reaches_the_endpoint_with_the_fields_it_expects():
    session = FakeSession()
    client = _client(session=session)
    client.info("m.f", "hello", symbol="AAPL", category="price")
    client.flush()
    record = session.posts[0]["body"]["records"][0]
    assert session.posts[0]["url"].endswith("/log")
    assert record["service"] == "test"
    assert record["location"] == "m.f"
    assert record["level"] == "INFO"
    assert record["symbol"] == "AAPL"
    assert record["category"] == "price"
    assert record["occurred"]          # the writer's clock travels with the line


def test_events_go_to_the_event_endpoint_not_the_log_endpoint():
    session = FakeSession()
    client = _client(session=session)
    client.event("order.filled", symbol="AAPL", order_id="abc")
    client.flush()
    assert session.posts[0]["url"].endswith("/event")
    assert session.posts[0]["body"]["events"][0]["event_type"] == "order.filled"


def test_payload_is_not_wrapped_in_a_key_of_its_own_name():
    """`payload=` is both a field and the natural keyword; it must not nest itself."""
    session = FakeSession()
    client = _client(session=session)
    client.event("a", payload={"notional": 2500})
    client.flush()
    assert session.posts[0]["body"]["events"][0]["payload"] == {"notional": 2500}

    session2 = FakeSession()
    client2 = _client(session=session2)
    client2.event("b", notional=2500)
    client2.flush()
    assert session2.posts[0]["body"]["events"][0]["payload"] == {"notional": 2500}

    session3 = FakeSession()
    client3 = _client(session=session3)
    client3.event("c", payload={"a": 1}, b=2)
    client3.flush()
    assert session3.posts[0]["body"]["events"][0]["payload"] == {"payload": {"a": 1}, "b": 2}


def test_an_order_id_need_not_be_a_uuid():
    """A broker id like 'demo-order-1' must not cost the batch.

    The column was uniqueidentifier in the first version, and a non-UUID value made SQL Server
    reject the whole INSERT -- four good events lost to one malformed field.
    """
    session = FakeSession()
    client = _client(session=session)
    client.event("order.ack", order_id="demo-order-1")
    client.flush()
    assert session.posts[0]["body"]["events"][0]["order_id"] == "demo-order-1"
