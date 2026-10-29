"""Tests for the recording threshold rule and its surface."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging.levels import (
    LevelCatalogue,
    get_catalogue,
    recorded,
    set_catalogue,
)
from mctrader_logging.models import AcceptedResponse, ServiceLevelRequest


@pytest.fixture(autouse=True)
def _restore_catalogue():
    previous = get_catalogue()
    set_catalogue(LevelCatalogue())
    yield
    set_catalogue(previous)


# ---------------------------------------------------------------------------
# the rule: at or above, never below; unset records everything
# ---------------------------------------------------------------------------

def test_no_threshold_records_everything():
    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "warnn", None):
        assert recorded(level, None) is True


def test_the_threshold_is_inclusive():
    """ERROR set to ERROR must still be recorded, or the setting would hide its own level."""
    assert recorded("ERROR", 40) is True
    assert recorded(40, 40) is True


def test_below_the_threshold_is_dropped():
    assert recorded("WARNING", 40) is False
    assert recorded("INFO", 40) is False
    assert recorded("DEBUG", 40) is False


def test_above_the_threshold_is_kept():
    assert recorded("CRITICAL", 40) is True


def test_debug_threshold_records_everything_standard():
    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        assert recorded(level, 10) is True


def test_an_alias_is_compared_by_its_number_not_its_spelling():
    """'warn' is WARNING(30); as a string 'warn' > 'error' would be false."""
    assert recorded("warn", 30) is True
    assert recorded("warn", 40) is False


def test_an_unknown_level_is_treated_as_info():
    """Consistent with the write path: an unrecognised level is kept, and resolved to INFO."""
    assert recorded("warnn", 20) is True
    assert recorded("warnn", 30) is False


def test_a_custom_level_works_with_a_threshold():
    set_catalogue(LevelCatalogue.from_rows([
        (20, "INFO", "", ""), (25, "NOTICE", "", "note"), (30, "WARNING", "", "warn"),
    ]))
    assert recorded("notice", 25) is True
    assert recorded("notice", 30) is False
    assert recorded("note", 25) is True


# ---------------------------------------------------------------------------
# the request and response shapes
# ---------------------------------------------------------------------------

def test_the_request_takes_a_name_number_or_alias():
    assert ServiceLevelRequest(level="ERROR").level == "ERROR"
    assert ServiceLevelRequest(level=40).level == 40
    assert ServiceLevelRequest(level="warn").level == "warn"


def test_the_request_carries_a_reason():
    request = ServiceLevelRequest(level="ERROR", reason="too noisy during testing")
    assert request.reason == "too noisy during testing"


def test_the_reason_is_optional():
    assert ServiceLevelRequest(level="ERROR").reason is None


def test_the_response_reports_what_was_suppressed_and_defaults_to_zero():
    assert AcceptedResponse(accepted=3).suppressed == 0
    assert AcceptedResponse(accepted=1, suppressed=3).suppressed == 3
