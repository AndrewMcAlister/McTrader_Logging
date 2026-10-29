"""Request and response shapes.

Validation is deliberately forgiving on everything except the fields that make a row
findable. A log line that arrives with a mistyped level, a 5,000-character message or a payload
that is not JSON is still a log line, and rejecting it would lose exactly the message someone
was trying to record when something went wrong. What *is* enforced: a service name and a
location, because a row that cannot be attributed is indistinguishable from noise.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import re

from pydantic import BaseModel, Field, field_validator

#: Characters that are not part of a canonical service name.
_NON_CANONICAL = re.compile(r"[^a-z0-9]+")


def canonical_service(value: str) -> str:
    """The canonical form of a client name: lowercase, punctuation collapsed to ``_``.

    Measured before this existed: one app logging as ``Mini-McTrader``, ``mini-mctrader``,
    ``MINI_MCTRADER`` and ``mini_mctrader`` became FOUR distinct services, so a query by
    name returned a quarter of its rows. The field exists to answer "which client wrote
    this", which it cannot do if the same client can appear under several spellings.

    Normalised rather than rejected: a client with a stale spelling should still get its
    line recorded, and the console copy is unaffected either way.
    """
    text = _NON_CANONICAL.sub("_", str(value or "").strip().lower()).strip("_")
    return text[:64]



class LogRecord(BaseModel):
    """One log line."""

    service: str = Field(..., min_length=1, max_length=64,
                         description="Which app wrote it, e.g. 'mctrader'. Canonicalised.")

    @field_validator("service")
    @classmethod
    def _canonical_service(cls, value: str) -> str:
        text = canonical_service(value)
        if not text:
            raise ValueError("service must contain at least one letter or digit")
        return text
    location: str = Field(..., min_length=1, max_length=255,
                          description="module.function, for finding the writer")
    message: str = Field("", max_length=2000)
    level: Union[str, int] = Field("INFO", description="DEBUG/INFO/WARNING/ERROR/CRITICAL")
    correlation_id: Optional[str] = Field(
        None, max_length=64,
        description="Spans services. The reason this service exists.")
    cycle_id: Optional[str] = Field(None, max_length=64)
    category: Optional[str] = Field(None, max_length=64,
                                    description="e.g. price, order, risk, startup")
    symbol: Optional[str] = Field(None, max_length=50)
    broker_id: Optional[int] = None
    order_id: Optional[str] = Field(None, max_length=64)
    occurred_utc: Optional[datetime] = Field(
        None, description="When the writer says it happened; not when we stored it")
    payload: Optional[Any] = Field(None, description="Structured detail, stored as JSON")


class LogBatch(BaseModel):
    records: List[LogRecord] = Field(..., min_length=1)


class EventRecord(BaseModel):
    """One domain event — an order submitted, a stop placed — as opposed to a log line."""

    service: str = Field(..., min_length=1, max_length=64)
    event_type: str = Field(..., min_length=1, max_length=64,
                            description="e.g. order.submitted, order.filled, stop.placed")

    @field_validator("service")
    @classmethod
    def _canonical_service(cls, value: str) -> str:
        text = canonical_service(value)
        if not text:
            raise ValueError("service must contain at least one letter or digit")
        return text
    #: An event has a severity too. Without this, "everything at WARNING or above" could not
    #: include events without a second query against a table with a different scale.
    level: Union[str, int] = Field(
        "INFO", description="DEBUG/INFO/WARNING/ERROR/CRITICAL, or any level in dbo.LogLevels")
    correlation_id: Optional[str] = Field(None, max_length=64)
    cycle_id: Optional[str] = Field(None, max_length=64)
    symbol: Optional[str] = Field(None, max_length=50)
    broker_id: Optional[int] = None
    order_id: Optional[str] = Field(None, max_length=64)
    occurred_utc: Optional[datetime] = None
    payload: Optional[Any] = None


class EventBatch(BaseModel):
    events: List[EventRecord] = Field(..., min_length=1)


class ServiceLevelRequest(BaseModel):
    """Set the minimum level a client's log lines must reach to be recorded.

    ``reason`` is not decoration: changing what gets recorded is an auditable act, and "why did the
    log go quiet on the 29th" needs an answer in the data rather than in someone's memory.
    """

    level: Union[str, int] = Field(
        ..., description="Minimum level to record, e.g. ERROR. DEBUG records everything")
    reason: Optional[str] = Field(None, max_length=255)


class AcceptedResponse(BaseModel):
    """What was stored, and what was refused.

    ``rejected`` is a count rather than a silent drop: a client whose batch is partly refused
    needs to know, or it will report "logged" for rows that never landed.
    """

    accepted: int
    rejected: int = 0
    #: Rows dropped by a per-service recording threshold. Reported rather than silently discarded:
    #: a client whose lines are being suppressed needs to know, or it will believe it has a record
    #: of something the sink never kept.
    suppressed: int = 0
    errors: List[str] = Field(default_factory=list)
