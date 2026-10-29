"""Configuration for McTrader_Logging.

Every value is an environment variable. The defaults are the safe ones: bind to localhost, send
nothing to the vendor, and write to the ``Logging`` database.

WHY ITS OWN DATABASE

Logs are high-volume, append-only, and grow without bound; they deserve their own recovery model
and backup schedule rather than sharing one with a trading database. Keeping them apart also means
a retention delete or a bulk log query cannot hold a lock that matters to a position.

The service writes ``dbo.LogEntry`` and ``dbo.LogEvent`` there. McTrader's own ``dbo.Logs`` stays
where it is, in ``McTrader_Live``, and is not touched by anything this project does.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name, "1" if default else "0").lower()
    return value not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    """Resolved settings for the service."""

    # -- database ----------------------------------------------------------
    db_server: str = "localhost"
    db_database: str = "Logging"
    db_driver: str = "ODBC Driver 17 for SQL Server"
    db_trusted: bool = True
    db_trust_server_certificate: bool = True
    db_timeout_seconds: int = 30

    # -- behaviour ---------------------------------------------------------
    #: Rows one POST may carry. A batch larger than this is refused rather than split, because
    #: a client that sends 10,000 lines in one request has a loop that should be batched.
    max_batch: int = 1000
    #: How long a query may look back by default when no window is given.
    default_query_minutes: int = 60
    #: Rows a query may return. A log query is for reading, not for exporting.
    max_query_rows: int = 5000
    #: Bodies longer than this are truncated rather than refused: a huge message must not cost
    #: the whole batch.
    max_message_chars: int = 2000

    # -- server ------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8004
    service_name: str = "McTrader_Logging"

    @property
    def connection_string(self) -> str:
        parts = [
            f"DRIVER={{{self.db_driver}}}",
            f"SERVER={self.db_server}",
            f"DATABASE={self.db_database}",
        ]
        if self.db_trusted:
            parts.append("Trusted_Connection=yes")
        if self.db_trust_server_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"


def load() -> Settings:
    return Settings(
        db_server=_env("LOG_DB_SERVER", "localhost"),
        db_database=_env("LOG_DATABASE", "Logging"),
        db_driver=_env("LOG_DB_DRIVER", "ODBC Driver 17 for SQL Server"),
        db_trusted=_env_bool("LOG_DB_TRUSTED", True),
        db_trust_server_certificate=_env_bool("LOG_DB_TRUST_CERT", True),
        db_timeout_seconds=_env_int("LOG_DB_TIMEOUT", 30),
        max_batch=_env_int("LOG_MAX_BATCH", 1000),
        default_query_minutes=_env_int("LOG_DEFAULT_QUERY_MINUTES", 60),
        max_query_rows=_env_int("LOG_MAX_QUERY_ROWS", 5000),
        max_message_chars=_env_int("LOG_MAX_MESSAGE_CHARS", 2000),
        host=_env("LOG_HOST", "127.0.0.1"),
        port=_env_int("LOG_PORT", 8004),
    )
