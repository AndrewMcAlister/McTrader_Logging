"""Service entry point: the same app, run in a way the Service Control Manager can manage.

WHY THIS EXISTS SEPARATELY FROM ``__main__``

``python -m mctrader_logging`` is for a developer at a console. A Windows service has **no
console**: anything written to stdout/stderr goes nowhere, so a service that logs only to the
stream appears to run silently whether it is healthy or broken. This module therefore logs to a
rotating file before it starts serving.

WHAT A SERVICE NEEDS THAT A CONSOLE RUN DOES NOT

* **A log file.** ``logs/mctrader_logging.log``, rotated at 10 MB, kept for 5 files. The
  NSSM-captured stdout is a separate, smaller safety net for crashes before logging is up.
* **A non-zero exit code on fatal startup failure.** NSSM restarts on a non-zero exit; a service
  that exits 0 after failing to bind its port looks like a clean stop and is never retried.
* **Graceful shutdown on stop.** uvicorn handles SIGINT/SIGTERM; NSSM's console stop method sends
  Ctrl+C, so in-flight requests finish instead of being killed.
* **``log_config=None``.** uvicorn normally reconfigures the root logger on startup, which would
  discard the file handler configured above and send every request log line back to nowhere.

Run it directly to check it works before installing:
    .\\.venv\\Scripts\\python.exe -m mctrader_logging.service
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import uvicorn

from .config import load

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s  %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(log_dir: Path) -> Path:
    """Attach a rotating file handler and a stream handler to the root logger."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "mctrader_logging.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Idempotent: a second call (or a reload) must not double every line.
    for handler in list(root.handlers):
        if getattr(handler, "_mctrader", False):
            return log_path

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._mctrader = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    # Keep the stream too: harmless under SCM (it goes to NSSM's captured file) and useful when
    # the module is run by hand.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler._mctrader = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)

    return log_path


def main() -> int:
    settings = load()
    log_path = configure_logging(Path(__file__).resolve().parent.parent / "logs")
    log = logging.getLogger("mctrader_logging.service")

    log.info("starting McTrader_Logging on %s:%s (database=%s, pid=%d)",
             settings.host, settings.port, settings.db_database, os.getpid())
    log.info("log file: %s", log_path)

    try:
        uvicorn.run(
            "mctrader_logging.api:app",
            host=settings.host,
            port=settings.port,
            log_level="info",
            # Keep the handlers configured above; uvicorn would otherwise replace them.
            log_config=None,
            # One worker: the write path is a batched INSERT and SQL Server serialises those
            # anyway, so extra workers add contention without throughput.
            workers=1,
        )
    except Exception:  # noqa: BLE001 - a fatal start must exit non-zero so SCM retries
        log.exception("fatal error; exiting non-zero so the service manager restarts us")
        return 1

    log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
