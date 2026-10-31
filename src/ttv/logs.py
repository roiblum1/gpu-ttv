"""Shared logging setup for the engine and the collector job."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging() -> None:
    """Emit JSON log lines on stdout."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
