"""Logging setup: quiet by default, ``--log-level debug`` when hunting bugs.

``FORGESMITH_LOG_JSON=true`` switches structlog to one JSON object per line so
``docker compose logs`` / Task Scheduler output can be collected by any log
shipper. The default stays human-readable.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.types import Processor


def configure_logging(level: str = "info", *, json: bool = False) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=numeric, format="%(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True)
        if json
        else structlog.processors.TimeStamper(fmt="%H:%M:%S"),
    ]
    if json:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )
