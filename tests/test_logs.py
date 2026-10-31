"""Log lines: standard-library records render as JSON, without probe, scrape or request noise."""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import Any

from ttv.logs import json_handler


def emit(name: str, level: int, msg: str, *args: object, exc: bool = False) -> list[dict[str, Any]]:
    """
    Pass one record through the handler, without touching the global loggers.

    Args:
        name (str): Logger name the record claims.
        level (int): Its level.
        msg (str): Its %-style message.
        *args (object): The message arguments.
        exc (bool): Attach the exception being handled.

    Returns:
        list[dict[str, Any]]: The JSON objects written, one per line.
    """
    stream = io.StringIO()
    info = sys.exc_info() if exc else None
    json_handler(stream).handle(logging.LogRecord(name, level, __file__, 1, msg, args, info))
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_a_standard_library_line_is_one_json_object() -> None:
    """uvicorn's startup line comes out like structlog's lines: event, level, timestamp."""
    [line] = emit("uvicorn.error", logging.INFO, "Uvicorn running on %s", "http://0.0.0.0:8080")
    assert line["event"] == "Uvicorn running on http://0.0.0.0:8080"
    assert line["level"] == "info"
    assert line["logger"] == "uvicorn.error"
    assert line["timestamp"].endswith("Z")


def test_probe_and_scrape_access_lines_are_dropped_and_api_calls_kept() -> None:
    """Probes and scrapes would log every few seconds; the unauthenticated API calls are kept."""
    fmt = '%s - "%s %s HTTP/%s" %d'
    kept = []
    for path in (
        "/healthz",
        "/readyz",
        "/metrics/",
        "/metrics",
        "/decisions?limit=1",
        "/explain/a",
    ):
        kept += emit("uvicorn.access", logging.INFO, fmt, "10.0.0.1:5000", "GET", path, "1.1", 200)
    assert [k["event"] for k in kept] == [
        '10.0.0.1:5000 - "GET /decisions?limit=1 HTTP/1.1" 200',
        '10.0.0.1:5000 - "GET /explain/a HTTP/1.1" 200',
    ]


def test_httpx_request_lines_are_dropped_and_its_warnings_kept() -> None:
    """A scan makes up to a thousand GETs; one INFO line each would bury scan.done."""
    assert emit("httpx", logging.INFO, 'HTTP Request: GET %s "%s"', "https://x/y", "200 OK") == []
    [line] = emit("httpx", logging.WARNING, "retrying")
    assert line["level"] == "warning"


def test_an_exception_is_rendered_as_text() -> None:
    """A traceback becomes a string field, so the line stays valid JSON."""
    try:
        raise ValueError("boom")
    except ValueError:
        [line] = emit("uvicorn.error", logging.ERROR, "Exception in ASGI application", exc=True)
    assert "ValueError: boom" in line["exception"]
