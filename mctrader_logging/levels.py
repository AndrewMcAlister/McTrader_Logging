"""Log levels: a catalogue stored in the database, with the code as its fallback.

The numbers are the conventional syslog scale (10..50) rather than 1..5, deliberately: it is the
scale every other logging tool speaks, and translating later is a silent source of "why is DEBUG
above ERROR".

WHY A TABLE AND A FALLBACK RATHER THAN ONE OR THE OTHER

``dbo.LogLevels`` is the catalogue: it says what levels exist, what they mean, and which spellings
mean them. Adding a level is an INSERT, not a release. But the write path must not depend on a read
of that table — a log line refused because the catalogue was unavailable would lose exactly the
message someone was recording when things went wrong. So the built-in levels below are also the
last-resort defaults, and :func:`resolve_level` falls back to them if no catalogue was loaded.

Storing the number *and* the name is deliberate too. The number is what can be compared
(``WHERE Level >= 30``); the name is what can be read. Storing only one forces the reader to know
the mapping, or turns "everything at or above WARNING" into a string comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class Level(IntEnum):
    """The built-in levels. Values match ``dbo.LogLevels.Level``."""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


#: ``(number, name, description, aliases)`` — seeded into ``dbo.LogLevels`` and used as the
#: fallback catalogue. Adding a row to the table adds a level; this tuple is what happens when the
#: table cannot be read.
DEFAULT_LEVELS: Tuple[Tuple[int, str, str, Tuple[str, ...]], ...] = (
    (10, "DEBUG", "Diagnostic detail; off in normal running.",
     ("debug", "trace", "verbose")),
    (20, "INFO", "Something happened as expected.",
     ("info", "information", "notice")),
    (30, "WARNING", "Something unexpected; the work carried on.",
     ("warn", "warning")),
    (40, "ERROR", "An operation failed.",
     ("error", "err", "exception")),
    (50, "CRITICAL", "The process, or a book, cannot continue.",
     ("critical", "fatal", "panic")),
)

#: What an unrecognised level becomes. Never raise: a line with a mistyped level is still a line.
DEFAULT_NUMBER = int(Level.INFO)


@dataclass(frozen=True)
class LevelInfo:
    """One level: the number that compares and the name that reads."""

    number: int
    name: str

    def __str__(self) -> str:  # so f-strings print the name, not the dataclass
        return self.name


class LevelCatalogue:
    """The levels this deployment accepts, resolved by number, name or alias.

    Aliases are held in the table as a comma-separated list so a caller can keep writing ``warn``
    and mean WARNING without every client carrying its own mapping.
    """

    def __init__(self, entries: Optional[Iterable[Sequence[Any]]] = None) -> None:
        self._by_number: Dict[int, LevelInfo] = {}
        self._by_word: Dict[str, LevelInfo] = {}
        self._description: Dict[int, str] = {}
        self._aliases: Dict[int, List[str]] = {}
        for entry in entries if entries is not None else DEFAULT_LEVELS:
            self._add(entry)

    # ------------------------------------------------------------------

    def _add(self, entry: Sequence[Any]) -> None:
        number = int(entry[0])
        name = str(entry[1]).strip().upper()
        description = str(entry[2] or "") if len(entry) > 2 else ""
        aliases = entry[3] if len(entry) > 3 else ()

        info = LevelInfo(number, name)
        self._by_number[number] = info
        self._by_word[name.lower()] = info
        self._description[number] = description
        self._aliases[number] = []

        if isinstance(aliases, str):
            aliases = [part for part in aliases.split(",")]
        for alias in aliases or ():
            word = str(alias).strip().lower()
            if word:
                self._by_word[word] = info
                self._aliases[number].append(word)

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence[Any]]) -> "LevelCatalogue":
        """Build from ``dbo.LogLevels`` rows: ``(Level, LevelName, Description, Aliases)``."""
        catalogue = cls(entries=())
        count = 0
        for row in rows:
            catalogue._add(row)
            count += 1
        if not count:
            return cls()  # an empty table means "no opinion", not "no levels"
        return catalogue

    # ------------------------------------------------------------------

    @property
    def numbers(self) -> List[int]:
        return sorted(self._by_number)

    def resolve(self, value: object, default: int = DEFAULT_NUMBER) -> LevelInfo:
        """The :class:`LevelInfo` for *value*, tolerating a name, an alias, a number or an enum."""
        if isinstance(value, LevelInfo):
            return value
        if isinstance(value, bool):
            return self._fallback(default)

        if isinstance(value, (int, float)):
            return self._by_number_or_nearest(int(value), default)

        text = str(value or "").strip().lower()
        if not text:
            return self._fallback(default)
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return self._by_number_or_nearest(int(text), default)
        hit = self._by_word.get(text)
        return hit if hit is not None else self._fallback(default)

    def _by_number_or_nearest(self, number: int, default: int) -> LevelInfo:
        exact = self._by_number.get(number)
        if exact is not None:
            return exact
        # Nearest known level, so 35 becomes WARNING rather than an error.
        if self._by_number:
            nearest = min(self._by_number, key=lambda candidate: abs(candidate - number))
            return self._by_number[nearest]
        return self._fallback(default)

    def _fallback(self, default: int) -> LevelInfo:
        hit = self._by_number.get(int(default))
        if hit is not None:
            return hit
        if self._by_number:
            return self._by_number[min(self._by_number)]
        return LevelInfo(DEFAULT_NUMBER, Level.INFO.name)

    def listing(self) -> List[Dict[str, Any]]:
        """The catalogue as JSON-ready dicts, ordered by severity."""
        return [
            {
                "level": number,
                "name": self._by_number[number].name,
                "description": self._description.get(number) or None,
                "aliases": list(self._aliases.get(number) or []),
            }
            for number in self.numbers
        ]


#: The catalogue in force. Replaced at startup if ``dbo.LogLevels`` can be read.
_CATALOGUE = LevelCatalogue()


def get_catalogue() -> LevelCatalogue:
    return _CATALOGUE


def set_catalogue(catalogue: LevelCatalogue) -> LevelCatalogue:
    """Install *catalogue* process-wide and return whatever was in force before."""
    global _CATALOGUE
    previous = _CATALOGUE
    _CATALOGUE = catalogue
    return previous


def resolve_level(value: object, default: int = DEFAULT_NUMBER) -> LevelInfo:
    """The level for *value* under the catalogue in force."""
    return _CATALOGUE.resolve(value, default)


def level_name(value: object) -> str:
    """The stored name for *value* (``WARNING``, not ``warn``)."""
    return resolve_level(value).name


def level_number(value: object) -> int:
    """The stored number for *value* (30 for WARNING)."""
    return resolve_level(value).number


def recorded(value: object, minimum: Optional[int] = None) -> bool:
    """Whether a line at *value* should be written, given a client's *minimum* recording level.

    ``None`` means no threshold, which records everything. Kept as a pure function rather than an
    ``if`` inside the endpoint so the rule -- at or above, never below; unset means all -- is
    testable without a database.
    """
    if minimum is None:
        return True
    return resolve_level(value).number >= int(minimum)


# ---------------------------------------------------------------------------
# The built-in-only resolver, kept for callers that must not depend on the catalogue.
# ---------------------------------------------------------------------------

_ALIASES = {
    word: Level(number)
    for number, _name, _description, aliases in DEFAULT_LEVELS
    for word in ((_name.lower(),) + tuple(aliases))
}


def parse_level(value: object, default: Level = Level.INFO) -> Level:
    """The built-in :class:`Level` for *value*, ignoring the catalogue.

    Kept because it is total, cheap and dependency-free: :mod:`data_service`-style callers that only
    ever use the five standard levels do not need a database read to answer. Anything that must
    honour a custom level should use :func:`resolve_level`.
    """
    if isinstance(value, Level):
        return value
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = int(value)
        for level in Level:
            if level.value == number:
                return level
        return min(Level, key=lambda level: abs(level.value - number))
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text.isdigit():
        return parse_level(int(text), default)
    return _ALIASES.get(text, default)
