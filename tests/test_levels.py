"""The level catalogue, and every write path accepting a level.

The contract these pin down:

  * the built-in levels are the fallback and are always available, so a write never depends on a
    database read;
  * a catalogue loaded from ``dbo.LogLevels`` can add a level, which is the point of moving them
    out of the code;
  * an unrecognised level is never an error -- a line with a mistyped level is still a line;
  * events carry a level too, which is what makes "everything at WARNING or above" one query.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mctrader_logging.levels import (
    DEFAULT_LEVELS,
    Level,
    LevelCatalogue,
    LevelInfo,
    get_catalogue,
    level_name,
    level_number,
    parse_level,
    resolve_level,
    set_catalogue,
)
from mctrader_logging.models import EventRecord, LogRecord


@pytest.fixture(autouse=True)
def _restore_catalogue():
    """Every test starts and ends on the built-in catalogue."""
    previous = get_catalogue()
    set_catalogue(LevelCatalogue())
    yield
    set_catalogue(previous)


# ---------------------------------------------------------------------------
# the built-in fallback
# ---------------------------------------------------------------------------

def test_the_five_standard_levels_are_the_defaults():
    assert [entry[0] for entry in DEFAULT_LEVELS] == [10, 20, 30, 40, 50]
    assert [entry[1] for entry in DEFAULT_LEVELS] == \
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def test_a_name_resolves_to_its_number_and_back():
    assert level_number("WARNING") == 30
    assert level_name(30) == "WARNING"


def test_aliases_resolve_to_the_canonical_name():
    assert level_name("warn") == "WARNING"
    assert level_name("fatal") == "CRITICAL"
    assert level_name("err") == "ERROR"
    assert level_name("trace") == "DEBUG"


def test_case_and_whitespace_are_ignored():
    assert level_name("  wArNing ") == "WARNING"


def test_an_unknown_level_falls_back_rather_than_raising():
    assert level_name("warnn") == "INFO"
    assert level_name(None) == "INFO"
    assert level_name("") == "INFO"


def test_a_number_between_levels_takes_the_nearest():
    assert level_name(35) == "WARNING"
    assert level_name(25) == "INFO"
    assert level_name(11) == "DEBUG"


def test_an_exact_number_wins_over_nearest():
    assert level_name(40) == "ERROR"
    assert level_number("40") == 40


def test_parse_level_is_unchanged_for_callers_that_do_not_want_the_catalogue():
    """It stays total and dependency-free: no catalogue, no database, no surprises."""
    assert parse_level("warn") is Level.WARNING
    assert parse_level(35) is Level.WARNING
    assert parse_level("warnn") is Level.INFO


# ---------------------------------------------------------------------------
# a catalogue from the database
# ---------------------------------------------------------------------------

def test_a_custom_level_is_accepted_once_it_is_in_the_catalogue():
    catalogue = LevelCatalogue.from_rows([
        (10, "DEBUG", "", "trace"),
        (20, "INFO", "", None),
        (25, "NOTICE", "Between info and warning.", "note"),
        (30, "WARNING", "", "warn"),
        (40, "ERROR", "", None),
        (50, "CRITICAL", "", None),
    ])
    set_catalogue(catalogue)

    assert resolve_level("notice") == LevelInfo(25, "NOTICE")
    assert resolve_level("note").number == 25      # alias from the table
    assert level_name(25) == "NOTICE"
    assert level_number("NOTICE") == 25


def test_a_level_only_in_the_table_is_written_not_mapped_to_a_neighbour():
    """The old behaviour mapped 25 to INFO. With a catalogue it must survive as itself."""
    set_catalogue(LevelCatalogue.from_rows([(25, "NOTICE", "", "note")]))
    assert resolve_level(25) == LevelInfo(25, "NOTICE")


def test_aliases_are_parsed_from_the_comma_separated_column():
    catalogue = LevelCatalogue.from_rows([(30, "WARNING", "", "warn, caution , alert")])
    for word in ("warn", "caution", "alert"):
        assert catalogue.resolve(word).number == 30


def test_an_empty_table_means_no_opinion_not_no_levels():
    """If the table is empty the built-ins must still apply, or nothing could ever be logged."""
    catalogue = LevelCatalogue.from_rows([])
    assert catalogue.numbers == [10, 20, 30, 40, 50]


def test_the_listing_is_ordered_by_severity_and_carries_the_aliases():
    catalogue = LevelCatalogue()
    listing = catalogue.listing()
    assert [entry["level"] for entry in listing] == [10, 20, 30, 40, 50]
    assert listing[2]["name"] == "WARNING"
    assert "warn" in listing[2]["aliases"]
    assert listing[2]["description"]


def test_set_catalogue_returns_what_was_there_before():
    previous = LevelCatalogue()
    set_catalogue(previous)
    replacement = LevelCatalogue.from_rows([(20, "INFO", "", "")])
    returned = set_catalogue(replacement)
    assert returned is previous
    assert get_catalogue() is replacement


# ---------------------------------------------------------------------------
# every write path accepts a level
# ---------------------------------------------------------------------------

def test_a_log_record_defaults_to_info():
    assert LogRecord(service="s", location="a.b").level == "INFO"


def test_a_log_record_takes_a_name_or_a_number():
    assert LogRecord(service="s", location="a.b", level="ERROR").level == "ERROR"
    assert LogRecord(service="s", location="a.b", level=40).level == 40


def test_an_event_defaults_to_info():
    """Events used to carry no level at all, so they could not take part in a severity query."""
    assert EventRecord(service="s", event_type="order.ack").level == "INFO"


def test_an_event_takes_a_level_too():
    assert EventRecord(service="s", event_type="order.ack", level="WARNING").level == "WARNING"
    assert EventRecord(service="s", event_type="order.ack", level=50).level == 50


def test_a_mistyped_event_level_is_kept_as_sent_and_normalised_on_write():
    """The model does not reject it; the store resolves it. A log line is never lost to its level."""
    assert EventRecord(service="s", event_type="x", level="warnn").level == "warnn"
    assert level_number("warnn") == 20
