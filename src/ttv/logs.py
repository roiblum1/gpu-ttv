"""Shared logging setup for the engine and the collector job: one JSON object per line on stdout."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

import structlog

# Requests that would add a line every few seconds; /decisions and /explain stay logged.
_QUIET_ACCESS_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/metrics/"})
_STAMP = [structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso", utc=True)]


def _keep(record: logging.LogRecord) -> bool:
    """
    Whether a standard-library record is worth a log line.

    Args:
        record (logging.LogRecord): The record, before formatting.

    Returns:
        bool: False for httpx's per-request INFO lines and uvicorn's probe and scrape access lines.
    """
    if record.name.startswith("httpx") and record.levelno < logging.WARNING:
        return False
    args = record.args
    if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) >= 3:
        return str(args[2]).split("?", 1)[0] not in _QUIET_ACCESS_PATHS
    return True


def json_handler(stream: TextIO) -> logging.Handler:
    """
    A handler that writes standard-library records (uvicorn, httpx) as JSON, like structlog's lines.

    Args:
        stream (TextIO): Where to write.

    Returns:
        logging.Handler: The handler, with the noise filter applied.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[structlog.stdlib.add_logger_name, *_STAMP],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    handler.addFilter(_keep)
    return handler


def configure_logging() -> None:
    """Emit JSON log lines on stdout, from structlog and the standard library alike."""
    logging.basicConfig(handlers=[json_handler(sys.stdout)], level=logging.INFO)
    structlog.configure(
        processors=[*_STAMP, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
