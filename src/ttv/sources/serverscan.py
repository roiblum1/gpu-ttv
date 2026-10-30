"""
Read-only async client for server-scan's REST API.

Lists `GET /api/v1/servers/rows` every scan and fills a detail cache from
`GET /api/v1/servers/{id}`; endpoint facts are in docs/discovery.md, "server-scan" §1.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
import structlog

from ttv.models import ServerScanServer
from ttv.sources.http import UnsafeRequestError

log = structlog.get_logger(__name__)

ROWS_PATH = "/api/v1/servers/rows"
_SERVERS_PREFIX = "/api/v1/servers/"
_SERVER_ID = re.compile(r"[A-Za-z0-9_.:-]+")
# `available` is a GET that writes through server-scan's ingest (discovery §1); `rows` is no id.
_RESERVED_IDS = frozenset({"available", "rows"})
_VOLATILE_ROW_FIELDS = frozenset({"last_seen_at", "stale"})
# Salts every row_hash: bump it when `_project_detail` keeps different fields.
_CACHE_FORMAT = "v1"
_GPUS_UNREAD = "hardware.gpus"


class ServerScanError(Exception):
    """The rows listing could not be read completely; the engine applies its grace policy."""


class _PayloadError(ValueError):
    """A server-scan payload does not have the expected shape."""


@dataclass(frozen=True)
class CachedDetail:
    """
    One server detail as a `DetailCache` stores it (table `serverscan_cache`).

    Attributes:
        id (str): server-scan `id`.
        row_hash (str): `row_hash` of the row the detail was fetched for.
        fetched_at (datetime): When it was fetched; timezone-aware.
        payload (dict[str, Any]): The detail trimmed to the fields this module reads.
    """

    id: str
    row_hash: str
    fetched_at: datetime
    payload: dict[str, Any]


class DetailCache(Protocol):
    """Storage for server details between scans."""

    async def get_many(self, ids: Sequence[str]) -> dict[str, CachedDetail]:
        """
        Return the cached entries for those of `ids` that have one.

        Args:
            ids (Sequence[str]): server-scan ids.

        Returns:
            dict[str, CachedDetail]: Entries keyed by id; absent ids are omitted.
        """
        ...

    async def put_many(self, entries: Sequence[CachedDetail]) -> None:
        """
        Insert or replace entries, keyed by `id`.

        Args:
            entries (Sequence[CachedDetail]): Entries to store.
        """
        ...


class InMemoryDetailCache:
    """A `DetailCache` held in process memory; lost on restart."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: dict[str, CachedDetail] = {}

    async def get_many(self, ids: Sequence[str]) -> dict[str, CachedDetail]:
        """
        Return the cached entries for those of `ids` that have one.

        Args:
            ids (Sequence[str]): server-scan ids.

        Returns:
            dict[str, CachedDetail]: Entries keyed by id; absent ids are omitted.
        """
        return {i: self._entries[i] for i in ids if i in self._entries}

    async def put_many(self, entries: Sequence[CachedDetail]) -> None:
        """
        Insert or replace entries, keyed by `id`.

        Args:
            entries (Sequence[CachedDetail]): Entries to store.
        """
        for entry in entries:
            self._entries[entry.id] = entry


@dataclass(frozen=True)
class ServerScanFetch:
    """
    The result of one `ServerScanSource.fetch`.

    Attributes:
        servers (tuple[ServerScanServer, ...]): One per row with a usable detail, in row order.
        pending_detail_ids (tuple[str, ...]): Listed ids not built yet (no usable detail).
        listing_complete (bool): True when the rows listing succeeded.
        generated_at (datetime | None): server-scan's `generated_at` for the listing.
        fetched_at (datetime): The scan time passed to `fetch`, in UTC.
        details_fetched (int): Detail GETs that succeeded this fetch.
        details_failed (int): Detail GETs that failed this fetch.
        not_modified (bool): True when rows answered 304 and the held listing was reused.
    """

    servers: tuple[ServerScanServer, ...]
    pending_detail_ids: tuple[str, ...]
    listing_complete: bool
    generated_at: datetime | None
    fetched_at: datetime
    details_fetched: int
    details_failed: int
    not_modified: bool


@dataclass(frozen=True)
class _Row:
    """One validated `ServerRow`, plus its `row_hash`."""

    id: str
    name: str
    vendor: str | None
    model: str | None
    site_id: str | None
    source_provider: str | None
    lifecycle_state: str | None
    cluster_name: str | None
    mce_name: str | None
    reported_name: str | None
    contested_with: str | None
    maintenance_enabled: bool
    maintenance_reason: str | None
    health: str | None
    reachable: bool | None
    last_seen_at: datetime | None
    stale: bool
    profile_template_name: str | None
    serial: str | None
    row_hash: str


@dataclass(frozen=True)
class _Detail:
    """The validated detail fields that rows lack."""

    id: str
    vendor: str
    serial_normalized: str
    system_uuid: str | None
    gpu_models: tuple[str, ...] | None
    claim_changed_at: datetime | None
    maintenance_enabled: bool
    maintenance_created_at: datetime | None
    reachable: bool | None
    unreachable_since: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class _Listing:
    """A validated rows listing and the ETag it came with."""

    etag: str | None
    generated_at: datetime | None
    rows: tuple[_Row, ...]


def is_safe_server_id(server_id: str) -> bool:
    """
    Tell whether an id may be put in a `GET /api/v1/servers/{id}` path.

    Args:
        server_id (str): Candidate id.

    Returns:
        bool: True for ids matching `^[A-Za-z0-9_.:-]+$` that are not dots or route names.
    """
    return (
        _SERVER_ID.fullmatch(server_id) is not None
        and server_id.strip(".") != ""
        and server_id.lower() not in _RESERVED_IDS
    )


def check_request(method: str, url: httpx.URL, base_url: httpx.URL) -> None:
    """
    Refuse every request except GET of the rows listing or of one server's detail.

    Args:
        method (str): HTTP method.
        url (httpx.URL): Absolute request URL.
        base_url (httpx.URL): server-scan's base URL.

    Raises:
        UnsafeRequestError: The request is not on the allow-list.
    """
    if method != "GET":
        raise UnsafeRequestError(f"refused {method}: server-scan is read-only")
    if (url.scheme, url.host, url.port) != (base_url.scheme, base_url.host, base_url.port):
        raise UnsafeRequestError("refused a request to another origin")
    if url.query:
        raise UnsafeRequestError("refused a request with a query string")
    base_path = base_url.path.rstrip("/")
    if url.path == base_path + ROWS_PATH:
        return
    prefix = base_path + _SERVERS_PREFIX
    if url.path.startswith(prefix) and is_safe_server_id(url.path.removeprefix(prefix)):
        return
    raise UnsafeRequestError("refused a path outside /servers/rows and /servers/{id}")


def row_hash(row: Mapping[str, Any]) -> str:
    """
    Hash a raw `ServerRow` for detail change detection, ignoring `last_seen_at` and `stale`.

    Args:
        row (Mapping[str, Any]): The row as decoded from JSON.

    Returns:
        str: Hex SHA-256 of the row's canonical JSON, salted with the cache format.
    """
    stable = {k: v for k, v in row.items() if k not in _VOLATILE_ROW_FIELDS}
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{_CACHE_FORMAT}:{canonical}".encode()).hexdigest()


def _req_obj(obj: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """
    Read a required JSON object.

    Args:
        obj (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        Mapping[str, Any]: The value.

    Raises:
        _PayloadError: The value is not an object.
    """
    value = obj.get(key)
    if not isinstance(value, dict):
        raise _PayloadError(f"{key}: expected an object")
    return value


def _opt_obj(obj: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """
    Read an optional JSON object; absent or null gives an empty mapping.

    Args:
        obj (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        Mapping[str, Any]: The value, or `{}`.

    Raises:
        _PayloadError: The value is neither an object nor null.
    """
    value = obj.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _PayloadError(f"{key}: expected an object or null")
    return value


def _req_str(obj: Mapping[str, Any], key: str) -> str:
    """
    Read a required string.

    Args:
        obj (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        str: The value.

    Raises:
        _PayloadError: The value is not a string.
    """
    value = obj.get(key)
    if not isinstance(value, str):
        raise _PayloadError(f"{key}: expected a string")
    return value


def _opt_str(obj: Mapping[str, Any], key: str) -> str | None:
    """
    Read an optional string.

    Args:
        obj (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        str | None: The value, or None when absent or null.

    Raises:
        _PayloadError: The value is neither a string nor null.
    """
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise _PayloadError(f"{key}: expected a string or null")
    return value


def _opt_bool(obj: Mapping[str, Any], key: str) -> bool | None:
    """
    Read an optional boolean.

    Args:
        obj (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        bool | None: The value, or None when absent or null.

    Raises:
        _PayloadError: The value is neither a boolean nor null.
    """
    value = obj.get(key)
    if value is not None and not isinstance(value, bool):
        raise _PayloadError(f"{key}: expected a boolean or null")
    return value


def _req_bool(obj: Mapping[str, Any], key: str) -> bool:
    """
    Read a required boolean.

    Args:
        obj (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        bool: The value.

    Raises:
        _PayloadError: The value is not a boolean.
    """
    value = _opt_bool(obj, key)
    if value is None:
        raise _PayloadError(f"{key}: expected a boolean")
    return value


def _opt_dt(obj: Mapping[str, Any], key: str) -> datetime | None:
    """
    Read an optional ISO-8601 timestamp as an aware UTC datetime; naive values are UTC.

    Args:
        obj (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        datetime | None: The value in UTC, or None when absent or null.

    Raises:
        _PayloadError: The value is not an ISO-8601 string.
    """
    value = _opt_str(obj, key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _PayloadError(f"{key}: expected an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_serial(serial: str | None) -> str:
    """
    Normalise a raw serial the way server-scan's `normalize_text` does (discovery §5).

    Args:
        serial (str | None): Raw serial.

    Returns:
        str: Lower-cased, whitespace-collapsed serial; `""` for None or empty.
    """
    return " ".join(serial.split()).lower() if serial else ""


def _parse_row(item: Any) -> _Row:
    """
    Validate one `ServerRow` item.

    Args:
        item (Any): One element of the listing's `items`.

    Returns:
        _Row: The row.

    Raises:
        _PayloadError: A field has the wrong type, or the id is unsafe.
    """
    if not isinstance(item, dict):
        raise _PayloadError("items[]: expected an object")
    server_id = _req_str(item, "id")
    if not is_safe_server_id(server_id):
        raise _PayloadError("id: not a safe server id")
    maintenance = _req_obj(item, "maintenance")
    return _Row(
        id=server_id,
        name=_req_str(item, "name"),
        vendor=_opt_str(item, "vendor"),
        model=_opt_str(item, "model"),
        site_id=_opt_str(item, "site_id"),
        source_provider=_opt_str(item, "source_provider"),
        lifecycle_state=_opt_str(item, "openshift_state"),
        cluster_name=_opt_str(item, "cluster_name"),
        mce_name=_opt_str(item, "mce_name"),
        reported_name=_opt_str(item, "openshift_reported_name"),
        contested_with=_opt_str(item, "contested_with"),
        maintenance_enabled=_req_bool(maintenance, "enabled"),
        maintenance_reason=_opt_str(maintenance, "reason"),
        health=_opt_str(item, "health"),
        reachable=_opt_bool(item, "reachable"),
        last_seen_at=_opt_dt(item, "last_seen_at"),
        stale=_req_bool(item, "stale"),
        profile_template_name=_opt_str(item, "profile_template_name"),
        serial=_opt_str(item, "serial"),
        row_hash=row_hash(item),
    )


def _parse_listing(body: Any, etag: str | None) -> _Listing:
    """
    Validate a `ServerRowsResponse` body.

    Args:
        body (Any): Decoded JSON.
        etag (str | None): The response's `ETag` header.

    Returns:
        _Listing: The listing.

    Raises:
        _PayloadError: The body is malformed or repeats an id.
    """
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise _PayloadError("body: expected an object with an items list")
    rows = tuple(_parse_row(item) for item in body["items"])
    if len({row.id for row in rows}) != len(rows):
        raise _PayloadError("items: duplicate server ids")
    return _Listing(etag=etag, generated_at=_opt_dt(body, "generated_at"), rows=rows)


def _project_detail(raw: Any) -> dict[str, Any]:
    """
    Trim a `ServerDetail` body to the fields rows lack, keeping the API's nesting.

    Args:
        raw (Any): Decoded detail JSON.

    Returns:
        dict[str, Any]: The cache payload; `_parse_detail` validates it.

    Raises:
        _PayloadError: The body is not an object.
    """
    if not isinstance(raw, dict):
        raise _PayloadError("detail: expected an object")

    def pick(key: str, *fields: str) -> Any:
        """
        Copy some fields of a nested object, or pass a non-object through for validation.

        Args:
            key (str): Nested object's field name.
            *fields (str): Fields to keep.

        Returns:
            Any: The trimmed object, or the raw value when it is not an object.
        """
        value = raw.get(key)
        return {f: value.get(f) for f in fields} if isinstance(value, dict) else value

    hardware = raw.get("hardware")
    gpus = hardware.get("gpus") if isinstance(hardware, dict) else None
    if isinstance(gpus, list):
        gpus = [{"model": g.get("model")} if isinstance(g, dict) else g for g in gpus]
    return {
        "id": raw.get("id"),
        "identity": pick("identity", "vendor", "serial_normalized", "system_uuid"),
        "hardware": {"gpus": gpus},
        "unread_fields": raw.get("unread_fields"),
        "openshift": pick("openshift", "claim_changed_at"),
        "maintenance": pick("maintenance", "enabled", "created_at"),
        "reachable": raw.get("reachable"),
        "unreachable_since": raw.get("unreachable_since"),
        "created_at": raw.get("created_at"),
    }


def _gpu_models(payload: Mapping[str, Any]) -> tuple[str, ...] | None:
    """
    Read one model per GPU; None when the inventory is unread (ADR-0002, "Assets with no GPUs").

    Args:
        payload (Mapping[str, Any]): A `_project_detail` payload.

    Returns:
        tuple[str, ...] | None: Models (`""` for a GPU without one), or None if unread.

    Raises:
        _PayloadError: `hardware.gpus` or `unread_fields` has the wrong type.
    """
    gpus = _opt_obj(payload, "hardware").get("gpus")
    unread = payload.get("unread_fields") or []
    if not isinstance(unread, list):
        raise _PayloadError("unread_fields: expected a list")
    if gpus is None:
        return None
    if not isinstance(gpus, list) or not all(isinstance(g, dict) for g in gpus):
        raise _PayloadError("hardware.gpus: expected a list of objects")
    # An empty list that server-scan could not read is its new-server default, not zero GPUs.
    if not gpus and _GPUS_UNREAD in unread:
        return None
    return tuple(_opt_str(g, "model") or "" for g in gpus)


def _parse_detail(payload: Mapping[str, Any]) -> _Detail:
    """
    Validate a cache payload built by `_project_detail`.

    Args:
        payload (Mapping[str, Any]): The payload.

    Returns:
        _Detail: The detail fields.

    Raises:
        _PayloadError: A field has the wrong type.
    """
    identity = _req_obj(payload, "identity")
    maintenance = _opt_obj(payload, "maintenance")
    return _Detail(
        id=_req_str(payload, "id"),
        vendor=_req_str(identity, "vendor"),
        serial_normalized=_opt_str(identity, "serial_normalized") or "",
        system_uuid=_opt_str(identity, "system_uuid"),
        gpu_models=_gpu_models(payload),
        claim_changed_at=_opt_dt(_opt_obj(payload, "openshift"), "claim_changed_at"),
        maintenance_enabled=_opt_bool(maintenance, "enabled") or False,
        maintenance_created_at=_opt_dt(maintenance, "created_at"),
        reachable=_opt_bool(payload, "reachable"),
        unreachable_since=_opt_dt(payload, "unreachable_since"),
        created_at=_opt_dt(payload, "created_at"),
    )


def _identity_matches(row: _Row, detail: _Detail) -> bool:
    """
    Tell whether a cached detail belongs to the row's current identity.

    Args:
        row (_Row): The current row.
        detail (_Detail): A cached detail, possibly fetched for an older row.

    Returns:
        bool: True when vendor and normalised serial agree.
    """
    if row.vendor is not None and row.vendor != detail.vendor:
        return False
    return _normalize_serial(row.serial) == detail.serial_normalized


def _build_server(row: _Row, detail: _Detail) -> ServerScanServer:
    """
    Merge a row (fast-changing fields) with its detail (fields rows lack).

    Args:
        row (_Row): The current row.
        detail (_Detail): A detail whose identity matches the row.

    Returns:
        ServerScanServer: The engine's view of the server.
    """
    in_maintenance = row.maintenance_enabled and detail.maintenance_enabled
    unreachable = row.reachable is False and detail.reachable is False
    return ServerScanServer(
        id=row.id,
        name=row.name,
        vendor=row.vendor or detail.vendor,
        serial_normalized=detail.serial_normalized,
        system_uuid=detail.system_uuid,
        model=row.model,
        site_id=row.site_id,
        source_provider=row.source_provider or "",
        gpu_models=detail.gpu_models,
        profile_template_name=row.profile_template_name,
        lifecycle_state=row.lifecycle_state,
        cluster_name=row.cluster_name,
        mce_name=row.mce_name,
        reported_name=row.reported_name,
        contested_with=row.contested_with,
        claim_changed_at=detail.claim_changed_at,
        maintenance_enabled=row.maintenance_enabled,
        maintenance_reason=row.maintenance_reason,
        maintenance_since=detail.maintenance_created_at if in_maintenance else None,
        health_overall=row.health,
        reachable=row.reachable,
        unreachable_since=detail.unreachable_since if unreachable else None,
        last_seen_at=row.last_seen_at,
        created_at=detail.created_at,
        stale=row.stale,
    )


class ServerScanSource:
    """
    Async, GET-only client for server-scan's rows listing and server details.

    Every request passes `check_request`; `fetch` must not run concurrently with itself.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None,
        ca_bundle: str | None,
        timeout_seconds: float,
        detail_ttl: timedelta,
        detail_max_per_scan: int,
        detail_concurrency: int,
        cache: DetailCache,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Create the client; TLS verification is always on.

        Args:
            base_url (str): server-scan root, e.g. `https://server-scan.example`.
            token (str | None): AUDITOR bearer token, or None when auth is off.
            ca_bundle (str | None): PEM bundle to trust instead of the system store.
            timeout_seconds (float): Per-request timeout.
            detail_ttl (timedelta): Refetch a detail older than this even if its row is unchanged.
            detail_max_per_scan (int): Most detail GETs per `fetch`.
            detail_concurrency (int): Most detail GETs in flight.
            cache (DetailCache): Detail storage between scans.
            transport (httpx.AsyncBaseTransport | None): Test transport; None for the network.

        Raises:
            ValueError: A setting is out of range or the URL is not http(s).
        """
        self._base_url = httpx.URL(base_url.rstrip("/"))
        if self._base_url.scheme not in {"http", "https"} or not self._base_url.host:
            raise ValueError("base_url must be an absolute http(s) URL")
        if detail_max_per_scan < 1 or detail_concurrency < 1 or detail_ttl <= timedelta(0):
            raise ValueError("detail budget, concurrency and TTL must be positive")
        self._detail_ttl = detail_ttl
        self._detail_max = detail_max_per_scan
        self._detail_concurrency = detail_concurrency
        self._cache = cache
        self._listing: _Listing | None = None
        self._lock = asyncio.Lock()
        headers = {"Accept": "application/json", "User-Agent": "gpu-ttv"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            verify=verify,
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [self._guard]},
        )

    async def __aenter__(self) -> ServerScanSource:
        """
        Enter an `async with` block.

        Returns:
            ServerScanSource: This client.
        """
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """
        Close the client on leaving an `async with` block.

        Args:
            *exc_info (object): Exception details, unused.
        """
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._client.aclose()

    async def _guard(self, request: httpx.Request) -> None:
        """
        Refuse unsafe requests before they reach any transport (httpx request hook).

        Args:
            request (httpx.Request): The outgoing request.

        Raises:
            UnsafeRequestError: The request is not on the allow-list.
        """
        check_request(request.method, request.url, self._base_url)

    async def fetch(self, now: datetime) -> ServerScanFetch:
        """
        List every server and build those whose detail is cached or fetched this scan.

        Args:
            now (datetime): Scan time, timezone-aware; stamps new cache entries.

        Returns:
            ServerScanFetch: Servers, ids still waiting for a detail, and counters.

        Raises:
            ServerScanError: The rows listing failed, timed out or was malformed.
            ValueError: `now` is naive.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        async with self._lock:
            started = time.monotonic()
            listing, not_modified = await self._fetch_rows()
            entries = await self._cache.get_many([row.id for row in listing.rows])
            details = {i: d for i, e in entries.items() if (d := _parse_cached(e)) is not None}
            queue = self._plan(listing.rows, entries, details, now)
            fetched = await self._fetch_details(queue, now)
            if fetched:
                await self._cache.put_many([entry for entry, _ in fetched])
            details.update({entry.id: detail for entry, detail in fetched})
            servers: list[ServerScanServer] = []
            pending: list[str] = []
            for row in listing.rows:
                detail = details.get(row.id)
                if detail is not None and _identity_matches(row, detail):
                    servers.append(_build_server(row, detail))
                else:
                    pending.append(row.id)
            result = ServerScanFetch(
                servers=tuple(servers),
                pending_detail_ids=tuple(pending),
                listing_complete=True,
                generated_at=listing.generated_at,
                fetched_at=now,
                details_fetched=len(fetched),
                details_failed=len(queue) - len(fetched),
                not_modified=not_modified,
            )
            log.info(
                "serverscan_fetch",
                rows=len(listing.rows),
                servers=len(result.servers),
                pending=len(result.pending_detail_ids),
                details_planned=len(queue),
                details_fetched=result.details_fetched,
                details_failed=result.details_failed,
                not_modified=not_modified,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return result

    async def _fetch_rows(self) -> tuple[_Listing, bool]:
        """
        GET the rows listing, conditionally on the held ETag.

        Returns:
            tuple[_Listing, bool]: The listing, and True when it is the held one (304).

        Raises:
            ServerScanError: Transport error, unexpected status or invalid body.
        """
        headers = {}
        if self._listing is not None and self._listing.etag:
            headers["If-None-Match"] = self._listing.etag
        try:
            response = await self._client.get(ROWS_PATH, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("serverscan_rows_failed", error=type(exc).__name__)
            raise ServerScanError(f"rows request failed: {type(exc).__name__}") from exc
        if response.status_code == 304 and self._listing is not None:
            return self._listing, True
        if response.status_code != 200:
            log.warning("serverscan_rows_failed", status=response.status_code)
            raise ServerScanError(f"rows answered HTTP {response.status_code}")
        try:
            listing = _parse_listing(response.json(), response.headers.get("etag"))
        except ValueError as exc:
            log.warning("serverscan_rows_failed", error="invalid payload")
            raise ServerScanError(f"rows payload invalid: {exc}") from exc
        self._listing = listing
        return listing, False

    def _plan(
        self,
        rows: Sequence[_Row],
        entries: Mapping[str, CachedDetail],
        details: Mapping[str, _Detail],
        now: datetime,
    ) -> list[_Row]:
        """
        Choose which details to GET: missing, then changed, then expired; oldest first.

        Args:
            rows (Sequence[_Row]): Current listing.
            entries (Mapping[str, CachedDetail]): Cache entries by id.
            details (Mapping[str, _Detail]): Entries whose payload parsed.
            now (datetime): Scan time.

        Returns:
            list[_Row]: At most `detail_max_per_scan` rows.
        """
        missing: list[_Row] = []
        changed: list[tuple[datetime, _Row]] = []
        expired: list[tuple[datetime, _Row]] = []
        for row in rows:
            entry = entries.get(row.id)
            if entry is None or row.id not in details:
                missing.append(row)
            elif entry.row_hash != row.row_hash:
                changed.append((entry.fetched_at, row))
            elif now - entry.fetched_at > self._detail_ttl:
                expired.append((entry.fetched_at, row))
        changed.sort(key=lambda pair: pair[0])
        expired.sort(key=lambda pair: pair[0])
        ordered = missing + [row for _, row in changed] + [row for _, row in expired]
        return ordered[: self._detail_max]

    async def _fetch_details(
        self, rows: Sequence[_Row], now: datetime
    ) -> list[tuple[CachedDetail, _Detail]]:
        """
        GET details with bounded concurrency; failures are dropped, not raised.

        Args:
            rows (Sequence[_Row]): Rows whose detail to fetch.
            now (datetime): Scan time, stamped on new entries.

        Returns:
            list[tuple[CachedDetail, _Detail]]: The successful fetches.
        """
        semaphore = asyncio.Semaphore(self._detail_concurrency)

        async def bounded(row: _Row) -> tuple[CachedDetail, _Detail] | None:
            """
            Fetch one detail once a concurrency slot is free.

            Args:
                row (_Row): The row.

            Returns:
                tuple[CachedDetail, _Detail] | None: The entry, or None on failure.
            """
            async with semaphore:
                return await self._fetch_detail(row, now)

        results = await asyncio.gather(*(bounded(row) for row in rows))
        return [result for result in results if result is not None]

    async def _fetch_detail(self, row: _Row, now: datetime) -> tuple[CachedDetail, _Detail] | None:
        """
        GET one server's detail.

        Args:
            row (_Row): The row; its hash is stored with the entry.
            now (datetime): Scan time.

        Returns:
            tuple[CachedDetail, _Detail] | None: The entry, or None on any failure.
        """
        try:
            response = await self._client.get(_SERVERS_PREFIX + row.id)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = _project_detail(response.json())
            detail = _parse_detail(payload)
        except ValueError:
            return None
        if detail.id != row.id:
            return None
        entry = CachedDetail(id=row.id, row_hash=row.row_hash, fetched_at=now, payload=payload)
        return entry, detail


def _parse_cached(entry: CachedDetail) -> _Detail | None:
    """
    Validate a cache entry's payload.

    Args:
        entry (CachedDetail): The entry.

    Returns:
        _Detail | None: The detail, or None when the payload is unusable (refetched).
    """
    try:
        detail = _parse_detail(entry.payload)
    except ValueError:
        return None
    return detail if detail.id == entry.id else None
