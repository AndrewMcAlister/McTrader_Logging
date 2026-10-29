"""Run the service: ``python -m mctrader_logging``."""
from __future__ import annotations

import logging

import uvicorn

from .config import load


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = load()
    uvicorn.run(
        "mctrader_logging.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        # One worker: the write path is a batched INSERT and SQL Server serialises those
        # anyway, so extra workers add contention without throughput.
        workers=1,
    )


if __name__ == "__main__":
    main()
