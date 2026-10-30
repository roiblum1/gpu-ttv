"""Shared read-only HTTP plumbing for cluster sources: error kinds, retries, TLS (AGENT.md §4)."""

from __future__ import annotations

import asyncio
import enum
import random
import ssl
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class SourceErrorKind(enum.StrEnum):
    """Why a source read failed (AGENT.md §4 "Mandatory connector behavior")."""

    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    STALE = "stale"
    INVALID = "invalid"
    CONFIRMED_ABSENT = "confirmed_absent"


class SourceError(Exception):
    """A source read failed; `kind` says how, `source` says which (never carries credentials)."""

    def __init__(self, source: str, kind: SourceErrorKind, message: str) -> None:
        """
        Build the error.

        Args:
            source (str): Source name, e.g. `kube:ocp4-prod-gpu-site1`.
            kind (SourceErrorKind): Failure kind.
            message (str): Human summary without secrets.
        """
        super().__init__(f"{source}: {kind}: {message}")
        self.source = source
        self.kind = kind


class UnsafeRequestError(RuntimeError):
    """A request outside a source's read-only allow-list was attempted; a bug, never retried."""


def kind_for_status(status: int) -> SourceErrorKind:
    """
    Map an HTTP status to a failure kind.

    Args:
        status (int): HTTP status code.

    Returns:
        SourceErrorKind: The kind.
    """
    if status in (401, 403):
        return SourceErrorKind.PERMISSION_DENIED
    if status == 404:
        return SourceErrorKind.CONFIRMED_ABSENT
    if status in _RETRYABLE_STATUS:
        return SourceErrorKind.TEMPORARILY_UNAVAILABLE
    return SourceErrorKind.INVALID


def tls_verify(ca_bundle: Path | None) -> ssl.SSLContext | bool:
    """
    TLS verification for a client: the given CA bundle, else the default trust store. Never off.

    Args:
        ca_bundle (Path | None): PEM bundle.

    Returns:
        ssl.SSLContext | bool: Value for `httpx.AsyncClient(verify=...)`.
    """
    return ssl.create_default_context(cafile=str(ca_bundle)) if ca_bundle else True


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    source: str,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Any:
    """
    GET a JSON document, retrying transient failures with exponential backoff and jitter.

    Retries connection errors, timeouts, 429 (honouring `Retry-After`) and 5xx. Every other
    failure raises at once.

    Args:
        client (httpx.AsyncClient): Client (its request hook enforces the allow-list).
        url (str): Absolute URL or a path relative to the client's base URL.
        source (str): Source name for errors and logs.
        params (Mapping[str, str] | None): Query parameters.
        headers (Mapping[str, str] | None): Extra headers (e.g. the bearer token).
        attempts (int): Total tries.
        base_delay (float): First backoff, in seconds.
        sleep (Callable[[float], Awaitable[Any]]): Sleep function (tests inject one).

    Returns:
        Any: The decoded JSON.

    Raises:
        SourceError: When the request fails for good or the body is not JSON.
    """
    last: SourceError | None = None
    for attempt in range(attempts):
        retry_after: float | None = None
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            last = SourceError(source, SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"timeout: {exc}")
        except httpx.TransportError as exc:
            last = SourceError(source, SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"transport: {exc}")
        else:
            if response.is_success:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SourceError(source, SourceErrorKind.INVALID, "body is not JSON") from exc
            kind = kind_for_status(response.status_code)
            detail = _error_detail(response)
            last = SourceError(
                source, kind, f"HTTP {response.status_code} for {response.url.path}{detail}"
            )
            if response.status_code not in _RETRYABLE_STATUS:
                raise last
            retry_after = _retry_after(response)
        if attempt + 1 < attempts:
            delay = retry_after if retry_after is not None else base_delay * 2**attempt
            log.debug("source.retry", source=source, attempt=attempt + 1, delay=round(delay, 2))
            await sleep(delay * (1 + random.random() * 0.2))  # noqa: S311 — jitter, not crypto
    assert last is not None
    raise last


def _error_detail(response: httpx.Response) -> str:
    """
    The `error` (Prometheus) or `message` (Kubernetes Status) field of a JSON error body.

    Args:
        response (httpx.Response): A failed response.

    Returns:
        str: `": <text>"` truncated to 200 characters, or "" when there is none.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    text = body.get("error") or body.get("message") if isinstance(body, dict) else None
    return f": {str(text)[:200]}" if text else ""


def _retry_after(response: httpx.Response) -> float | None:
    """
    Read a numeric `Retry-After` header, capped at 30 s.

    Args:
        response (httpx.Response): The response.

    Returns:
        float | None: Seconds to wait, if given.
    """
    value = response.headers.get("retry-after", "")
    try:
        return min(float(value), 30.0)
    except ValueError:
        return None
