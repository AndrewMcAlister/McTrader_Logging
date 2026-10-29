"""The client name is captured, canonical, indexed and reported."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging.models import (
    EventRecord, LogRecord, canonical_service,
)


# ---------------------------------------------------------------------------
# canonical form
# ---------------------------------------------------------------------------

def test_punctuation_and_case_collapse_to_one_name():
    """Measured before this existed: four spellings became four distinct services."""
    for spelling in ("Mini-McTrader", "mini-mctrader", "MINI_MCTRADER", "mini_mctrader",
                     "  mini mctrader  ", "mini.mctrader"):
        assert canonical_service(spelling) == "mini_mctrader", spelling


def test_a_plain_name_is_unchanged():
    assert canonical_service("mctrader") == "mctrader"
    assert canonical_service("data_service") == "data_service"


def test_a_name_with_digits_and_underscores_survives():
    assert canonical_service("paper_trader_1") == "paper_trader_1"
    assert canonical_service("PortfolioAdvisor2") == "portfolioadvisor2"


def test_a_name_of_pure_punctuation_is_empty_and_so_refused():
    assert canonical_service("---") == ""
    assert canonical_service("   ") == ""
    with pytest.raises(Exception):
        LogRecord(service="---", location="m.f")


# ---------------------------------------------------------------------------
# the models apply it
# ---------------------------------------------------------------------------

def test_a_log_record_stores_the_canonical_name():
    record = LogRecord(service="Mini-McTrader", location="m.f", message="x")
    assert record.service == "mini_mctrader"


def test_an_event_record_stores_the_canonical_name():
    event = EventRecord(service="MINI-MCTRADER", event_type="order.submitted")
    assert event.service == "mini_mctrader"


def test_service_is_still_required():
    with pytest.raises(Exception):
        LogRecord(location="m.f", message="no service")


def test_two_spellings_produce_the_same_stored_value():
    a = LogRecord(service="Mini-McTrader", location="a", message="1")
    b = LogRecord(service="mini_mctrader", location="b", message="2")
    assert a.service == b.service
