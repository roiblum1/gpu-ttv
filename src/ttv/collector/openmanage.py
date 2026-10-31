"""
Read-only Dell OpenManage Enterprise reader for the profile-less GPU collector (ADR-0004, "Dell").

Lists every server device, joins each to the profile that targets it, and reads GPU evidence from
OME inventory, else from the server's iDRAC over Redfish. Session and paging follow server-scan's
`openmanage/client.py@defe172`; the device-driven listing is new (docs/discovery.md, "Dell").
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from ttv.collector.redfish_gpu import (
    RedfishPayloadError,
    idrac_origin,
    is_gpu_pcie_device,
    read_gpu_models,
)
from ttv.collector.redfish_gpu import check_request as check_redfish_request
from ttv.config import OpenManageConnection, read_secret
from ttv.identity import normalize_serial
from ttv.models import CollectorRead, ManagerServer
from ttv.sources.http import (
    SourceError,
    SourceErrorKind,
    UnsafeRequestError,
    get_json,
    kind_for_status,
    tls_verify,
)

log = structlog.get_logger(__name__)

KIND = "openmanage"
VENDOR = "dell"
SERVER_TYPE = 1000
LOGIN_PATH = "/api/SessionService/Sessions"
DEVICES_PATH = "/api/DeviceService/Devices"
SERVER_FILTER = "Type eq 1000"
DEVICES_URL = f"{DEVICES_PATH}?$filter=Type%20eq%20{SERVER_TYPE}"
# OME 3.3+ path first; OME 3.2 serves profiles under TemplateService (docs/discovery.md, "Dell").
PROFILE_PATHS = ("/api/ProfileService/Profiles", "/api/TemplateService/Profiles")
DEFAULT_INVENTORY_TYPES = ("serverDeviceCards",)
GPU_SOURCE_OME = "ome-inventory"
GPU_SOURCE_REDFISH = "redfish"
JOIN_TARGET_ID = "target_id"
JOIN_TARGET_NAME = "target_name"
JOIN_AMBIGUOUS = "ambiguous"

_SESSION_PATH = re.compile(r"^/api/SessionService/Sessions\('([^'/]+)'\)$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_INVENTORY_PATH = re.compile(
    r"^/api/DeviceService/Devices\((\d+)\)/InventoryDetails\('([A-Za-z]+)'\)$"
)
_INVENTORY_TYPE = re.compile(r"^[A-Za-z]+$")
_PAGE_PARAMS = frozenset({"$top", "$skip"})
_NO_ANSWER = frozenset({SourceErrorKind.INVALID, SourceErrorKind.CONFIRMED_ABSENT})
_MAX_PAGES = 10_000
_MAX_ERRORS = 50
_AUTH_BREAKER = 3
_REDFISH_ATTEMPTS = 2


def _check_page_query(params: httpx.QueryParams, required_filter: str | None) -> None:
    """
    Allow only `$top`, `$skip` (digits) and, where required, the exact `$filter`, each once.

    Args:
        params (httpx.QueryParams): The request's query parameters.
        required_filter (str | None): The `$filter` the list must carry; None allows none.

    Raises:
        UnsafeRequestError: A parameter is unknown, repeated, or has a value off the list.
    """
    keys = [key for key, _ in params.multi_items()]
    allowed = _PAGE_PARAMS | ({"$filter"} if required_filter else set())
    if len(keys) != len(set(keys)) or not set(keys) <= allowed:
        raise UnsafeRequestError(f"refused list query parameters {sorted(set(keys))}")
    if required_filter is not None and params.get("$filter") != required_filter:
        raise UnsafeRequestError("refused a device list without the server-type filter")
    if not all(params[key].isdigit() for key in _PAGE_PARAMS & set(keys)):
        raise UnsafeRequestError("refused a non-numeric $top or $skip")


def check_request(
    method: str,
    url: httpx.URL,
    base_url: httpx.URL,
    *,
    session_id: str | None,
    inventory_types: Sequence[str],
) -> None:
    """
    Refuse every OME request except login, our own logout and the read-only list/inventory GETs.

    Allowed: `POST /api/SessionService/Sessions`; `DELETE` of `Sessions('<our id>')`; GET of the
    server device list (`$filter=Type eq 1000`), the profile list, and one device's
    `InventoryDetails('<configured type>')`. Same origin only; no percent-encoded paths.

    Args:
        method (str): HTTP method.
        url (httpx.URL): Absolute request URL.
        base_url (httpx.URL): The OME base URL.
        session_id (str | None): Id of the session this client opened, if any.
        inventory_types (Sequence[str]): Inventory types the reader may read.

    Raises:
        UnsafeRequestError: The request is not on the allow-list.
    """
    if (url.scheme, url.host, url.port) != (base_url.scheme, base_url.host, base_url.port):
        raise UnsafeRequestError("refused a request to another origin")
    if url.userinfo or url.fragment:
        raise UnsafeRequestError("refused a URL with userinfo or a fragment")
    if url.raw_path.split(b"?", 1)[0].decode("ascii") != url.path:
        raise UnsafeRequestError("refused a percent-encoded path")
    base_path = base_url.path.rstrip("/")
    if not url.path.startswith(base_path + "/"):
        raise UnsafeRequestError("refused a path outside the OME root")
    path = url.path.removeprefix(base_path)
    if method == "POST":
        if path != LOGIN_PATH or url.query:
            raise UnsafeRequestError("refused a POST other than the session login")
        return
    if method == "DELETE":
        match = _SESSION_PATH.fullmatch(path)
        if not match or session_id is None or match.group(1) != session_id or url.query:
            raise UnsafeRequestError("refused a DELETE other than this client's own logout")
        return
    if method != "GET":
        raise UnsafeRequestError(f"refused {method}: OpenManage is read-only here")
    if path == DEVICES_PATH:
        _check_page_query(url.params, SERVER_FILTER)
        return
    if path in PROFILE_PATHS:
        _check_page_query(url.params, None)
        return
    match = _INVENTORY_PATH.fullmatch(path)
    if not match or match.group(2) not in inventory_types or url.query:
        raise UnsafeRequestError("refused a path outside the read-only allow-list")


def gpu_models_from_inventory(body: Any) -> tuple[str, ...] | None:
    """
    One model (`Description`) per GPU card in an `InventoryDetails` answer.

    Cards are screened with the same vendor + display-class rule as iDRAC PCIe devices.

    Args:
        body (Any): Decoded `InventoryDetails('<type>')` response.

    Returns:
        tuple[str, ...] | None: GPU models; None when the body has no `InventoryInfo` list.
    """
    info = body.get("InventoryInfo") if isinstance(body, dict) else None
    if not isinstance(info, list):
        return None
    cards = [card for card in info if isinstance(card, dict)]
    return tuple(str(card.get("Description") or "") for card in cards if is_gpu_pcie_device(card))


# Copied from server-scan backend/app/infrastructure/providers/openmanage/mapping.py@defe172.
def _opt_str(value: object) -> str | None:
    """
    Normalize an OME string field to a non-empty `str` or `None`.

    Args:
        value (object): A raw OME field value.

    Returns:
        str | None: The stripped string, or `None` when missing or blank.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# Copied from server-scan backend/app/infrastructure/providers/openmanage/mapping.py@defe172.
def _network_address(device: Mapping[str, Any]) -> str | None:
    """
    Read a device's real network address, immune to OME's naming policy.

    Args:
        device (Mapping[str, Any]): One `/DeviceService/Devices` entry.

    Returns:
        str | None: `DeviceManagement[0].NetworkAddress`, or None when there is none.
    """
    management = device.get("DeviceManagement")
    if not isinstance(management, list) or not management:
        return None
    first = management[0]
    if not isinstance(first, dict):
        return None
    return _opt_str(first.get("NetworkAddress"))


def _as_int(value: object) -> int | None:
    """
    An OME integer id or state, accepting digit strings.

    Args:
        value (object): Raw value.

    Returns:
        int | None: The integer, or None when absent or not an integer.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


@dataclass(frozen=True)
class _Device:
    """
    The fields of one OME server device the reader uses.

    Attributes:
        id (int): `Id`.
        service_tag (str | None): `DeviceServiceTag`.
        name (str | None): `DeviceName`.
        model (str | None): `Model`.
        address (str | None): `DeviceManagement[0].NetworkAddress` (the iDRAC).
        last_inventory (str | None): `LastInventoryTime`, verbatim.
    """

    id: int
    service_tag: str | None
    name: str | None
    model: str | None
    address: str | None
    last_inventory: str | None


@dataclass(frozen=True)
class _Profile:
    """
    The fields of one OME profile the reader uses.

    Attributes:
        id (int | None): `Id`.
        name (str): `ProfileName` ("" when blank).
        state (int | None): `ProfileState` (evidence only).
        target_id (int | None): `TargetId`; 0 and null mean no target.
        target_name (str | None): `TargetName`.
    """

    id: int | None
    name: str
    state: int | None
    target_id: int | None
    target_name: str | None


@dataclass(frozen=True)
class _Binding:
    """
    A profile that targets a device, and how the join found it.

    Attributes:
        profile (_Profile): The profile.
        method (str): `target_id` or `target_name`.
    """

    profile: _Profile
    method: str


@dataclass
class _Join:
    """
    Profiles joined to devices, with what could not be joined.

    Attributes:
        bindings (dict[int, list[_Binding]]): Device id → profiles that target it, best first.
        untargeted (int): Profiles that name no target (unassigned).
        unmatched (int): Profiles whose target is not a listed server device.
        ambiguous (int): Profiles whose `TargetName` names several devices.
        ambiguous_devices (set[int]): Devices one of those profiles may target.
    """

    bindings: dict[int, list[_Binding]] = field(default_factory=dict)
    untargeted: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    ambiguous_devices: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class _Gpu:
    """
    GPU evidence for one device.

    Attributes:
        models (tuple[str, ...] | None): GPU models; () none; None unread.
        source (str | None): `ome-inventory` or `redfish`.
        rule (str | None): Inventory type, or the Redfish rule (`processors`, `pcie`).
        note (str | None): Why the GPUs are unread, when they are.
        failed (bool): A read was attempted and failed.
        read_at (datetime | None): When the evidence was read.
        cached (bool): Served from the per-device cache.
    """

    models: tuple[str, ...] | None
    source: str | None = None
    rule: str | None = None
    note: str | None = None
    failed: bool = False
    read_at: datetime | None = None
    cached: bool = False


@dataclass(frozen=True)
class _CacheEntry:
    """
    A GPU answer and the `LastInventoryTime` it was read under.

    Attributes:
        last_inventory (str | None): `LastInventoryTime` at read time.
        gpu (_Gpu): The answer.
    """

    last_inventory: str | None
    gpu: _Gpu


@dataclass
class _ReadState:
    """
    Per-read state shared by the GPU tasks.

    Attributes:
        now (datetime): Read time.
        ome_headers (dict[str, str]): The OME session header.
        semaphore (asyncio.Semaphore): Bounds concurrent per-device GPU reads.
        redfish_headers (dict[str, str] | None): Basic auth header; None disables Redfish.
        redfish_note (str | None): Why Redfish is unavailable.
        redfish_failed (bool): Redfish is configured but its password could not be read.
        redfish_rejections (int): iDRACs that refused the account in this read.
        redfish_successes (int): iDRACs read in this read.
        errors (list[str]): Error summaries without secrets.
    """

    now: datetime
    ome_headers: dict[str, str]
    semaphore: asyncio.Semaphore
    redfish_headers: dict[str, str] | None = None
    redfish_note: str | None = None
    redfish_failed: bool = False
    redfish_rejections: int = 0
    redfish_successes: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_device(item: Mapping[str, Any]) -> _Device | None:
    """
    Read one server device; None when it has no integer `Id`.

    Args:
        item (Mapping[str, Any]): One `/DeviceService/Devices` entry.

    Returns:
        _Device | None: The device.
    """
    device_id = _as_int(item.get("Id"))
    if device_id is None:
        return None
    return _Device(
        id=device_id,
        service_tag=_opt_str(item.get("DeviceServiceTag")),
        name=_opt_str(item.get("DeviceName")),
        model=_opt_str(item.get("Model")),
        address=_network_address(item),
        last_inventory=_opt_str(item.get("LastInventoryTime")),
    )


def _parse_profile(item: Mapping[str, Any]) -> _Profile:
    """
    Read one profile.

    Args:
        item (Mapping[str, Any]): One `/ProfileService/Profiles` entry.

    Returns:
        _Profile: The profile.
    """
    target_id = _as_int(item.get("TargetId"))
    return _Profile(
        id=_as_int(item.get("Id")),
        name=_opt_str(item.get("ProfileName")) or "",
        state=_as_int(item.get("ProfileState")),
        target_id=target_id if target_id and target_id > 0 else None,
        target_name=_opt_str(item.get("TargetName")),
    )


def join_profiles(devices: Sequence[_Device], profiles: Sequence[_Profile]) -> _Join:
    """
    Bind each profile to the device it targets: `TargetId == Id`, else `TargetName == DeviceName`.

    A profile binds in any `ProfileState`; a profile with a `TargetId` never falls back to the
    name, and a name that matches several devices binds none (ADR-0004, "Dell").

    Args:
        devices (Sequence[_Device]): Listed server devices.
        profiles (Sequence[_Profile]): Every profile.

    Returns:
        _Join: Bindings per device, best first, and the counts of what did not bind.
    """
    ids = {device.id for device in devices}
    by_name: dict[str, list[int]] = defaultdict(list)
    for device in devices:
        if device.name:
            by_name[device.name].append(device.id)
    join = _Join()
    for profile in profiles:
        if profile.target_id is not None:
            if profile.target_id in ids:
                join.bindings.setdefault(profile.target_id, []).append(
                    _Binding(profile, JOIN_TARGET_ID)
                )
            else:
                join.unmatched += 1
        elif profile.target_name:
            matches = by_name.get(profile.target_name, [])
            if len(matches) == 1:
                join.bindings.setdefault(matches[0], []).append(_Binding(profile, JOIN_TARGET_NAME))
            elif matches:
                join.ambiguous += 1
                join.ambiguous_devices.update(matches)
            else:
                join.unmatched += 1
        else:
            join.untargeted += 1
    for bound in join.bindings.values():
        bound.sort(key=lambda b: (b.method != JOIN_TARGET_ID, b.profile.id is None, b.profile.id))
    return join


def _inventory_path(device_id: int, inventory_type: str) -> str:
    """
    The `InventoryDetails` path of one device and inventory type.

    Args:
        device_id (int): OME device `Id`.
        inventory_type (str): e.g. `serverDeviceCards`.

    Returns:
        str: The path.
    """
    return f"{DEVICES_PATH}({device_id})/InventoryDetails('{inventory_type}')"


class OpenManageSource:
    """
    Async reader of one OpenManage Enterprise; implements `CollectorSource`, read-only.

    Every OME request passes `check_request` and every iDRAC request `redfish_gpu.check_request`.
    GPU answers are cached per device until its `LastInventoryTime` changes or `gpu_cache_ttl`
    passes, so an unchanged fleet costs one device and one profile listing per cycle.
    """

    def __init__(
        self,
        conn: OpenManageConnection,
        *,
        timeout_seconds: float,
        inventory_types: Sequence[str] = DEFAULT_INVENTORY_TYPES,
        empty_inventory_means_no_gpu: bool = False,
        pcie_fallback: bool = True,
        gpu_concurrency: int = 8,
        gpu_cache_ttl: timedelta = timedelta(hours=24),
        request_timeout_seconds: float = 30.0,
        max_members: int = 128,
        retry_delay: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Create the clients; nothing is sent until `read`. TLS verification is always on.

        Args:
            conn (OpenManageConnection): OME and optional iDRAC connection.
            timeout_seconds (float): Deadline for one whole read (`collector_timeout_seconds`).
            inventory_types (Sequence[str]): OME inventory types searched for GPU cards, in order
                (a validation gate: `serverDeviceCards` until probe B3/B4 says otherwise).
            empty_inventory_means_no_gpu (bool): Trust an OME inventory that lists no GPU as
                "no GPUs"; off until the probe shows OME lists every GPU there.
            pcie_fallback (bool): Screen iDRAC PCIe devices when `Processors` lists no GPU.
            gpu_concurrency (int): Devices whose GPUs are read at once.
            gpu_cache_ttl (timedelta): Longest a cached GPU answer is reused.
            request_timeout_seconds (float): Timeout of each HTTP request.
            max_members (int): Most members read from one Redfish collection or PCIe list.
            retry_delay (float): First retry backoff, in seconds (tests pass 0).
            transport (httpx.AsyncBaseTransport | None): Test transport; None for the network.

        Raises:
            ValueError: A setting is out of range, the URL is not https, or an iDRAC user has
                no password file.
        """
        if timeout_seconds <= 0 or request_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if gpu_concurrency < 1 or max_members < 1:
            raise ValueError("gpu_concurrency and max_members must be at least 1")
        types = tuple(inventory_types)
        if not types or not all(_INVENTORY_TYPE.fullmatch(t) for t in types):
            raise ValueError(f"{conn.name}: inventory types must be non-empty letters only")
        if conn.idrac_username and conn.idrac_password_file is None:
            raise ValueError(f"{conn.name}: idrac_username needs idrac_password_file")
        self._conn = conn
        self.name = f"{KIND}:{conn.name}"
        self._base_url = httpx.URL(conn.url.rstrip("/"))
        if self._base_url.scheme != "https" or not self._base_url.host:
            raise ValueError(f"{conn.name}: url must be an absolute https URL")
        self._prefix = self._base_url.path.rstrip("/")
        self._inventory_types = types
        self._empty_means_none = empty_inventory_means_no_gpu
        self._pcie_fallback = pcie_fallback
        self._concurrency = gpu_concurrency
        self._cache_ttl = gpu_cache_ttl
        self._timeout = timeout_seconds
        self._max_members = max_members
        self._retry_delay = retry_delay
        self._token: str | None = None
        self._session_id: str | None = None
        self._idrac_hosts: frozenset[str] = frozenset()
        self._cache: dict[int, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        timeout = httpx.Timeout(min(request_timeout_seconds, timeout_seconds))
        headers = {"Accept": "application/json", "User-Agent": "gpu-ttv"}
        self._ome = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            verify=tls_verify(conn.ca_bundle),
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [self._guard_ome]},
        )
        self._idrac = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            verify=tls_verify(conn.idrac_ca_bundle),
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [self._guard_idrac]},
        )

    async def __aenter__(self) -> OpenManageSource:
        """
        Enter an `async with` block.

        Returns:
            OpenManageSource: This reader.
        """
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """
        Log out and close on leaving an `async with` block.

        Args:
            *exc_info (object): Exception details, unused.
        """
        await self.aclose()

    async def aclose(self) -> None:
        """Log out of an open OME session and close both connection pools."""
        await self._logout()
        await self._ome.aclose()
        await self._idrac.aclose()

    async def _guard_ome(self, request: httpx.Request) -> None:
        """
        Refuse unsafe OME requests before they reach any transport (httpx request hook).

        Args:
            request (httpx.Request): The outgoing request.
        """
        check_request(
            request.method,
            request.url,
            self._base_url,
            session_id=self._session_id,
            inventory_types=self._inventory_types,
        )

    async def _guard_idrac(self, request: httpx.Request) -> None:
        """
        Refuse unsafe iDRAC requests before they reach any transport (httpx request hook).

        Args:
            request (httpx.Request): The outgoing request.
        """
        check_redfish_request(request.method, request.url, self._idrac_hosts)

    async def read(self, now: datetime) -> CollectorRead:
        """
        Read every server device with its bound profile and GPU evidence; log out at the end.

        Args:
            now (datetime): Read time, timezone-aware.

        Returns:
            CollectorRead: `complete=False` when a GPU read failed or a profile join was
                ambiguous; those servers keep `gpu_models=None` or their raw evidence.

        Raises:
            SourceError: Login failed, or the device or profile listing could not be read in
                full within the deadline.
            ValueError: `now` is naive.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        async with self._lock:
            started = time.monotonic()
            try:
                result = await self._read(now)
            finally:
                await self._logout()
            elapsed = time.monotonic() - started
        result = replace(result, finished_at=now + timedelta(seconds=elapsed))
        log.info(
            "openmanage_read",
            manager=self.name,
            complete=result.complete,
            errors=len(result.errors),
            duration_s=round(elapsed, 3),
            **result.coverage,
        )
        return result

    async def _read(self, now: datetime) -> CollectorRead:
        """
        Log in, list devices and profiles, join them, then read GPU evidence within the deadline.

        Args:
            now (datetime): Read time in UTC.

        Returns:
            CollectorRead: The read (`finished_at` is set by the caller).

        Raises:
            SourceError: Login or a listing failed, or the listing missed the deadline.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        password = self._secret()
        try:
            async with asyncio.timeout_at(deadline):
                ome_headers = await self._login(password)
                raw_devices = await self._get_all(DEVICES_URL, ome_headers)
                raw_profiles, profile_path = await self._profiles(ome_headers)
        except TimeoutError as exc:
            raise SourceError(
                self.name,
                SourceErrorKind.TEMPORARILY_UNAVAILABLE,
                f"listing exceeded the {self._timeout:g}s deadline",
            ) from exc
        state = _ReadState(now, ome_headers, asyncio.Semaphore(self._concurrency))
        devices, skipped, malformed = self._devices(raw_devices, state)
        profiles = [_parse_profile(p) for p in raw_profiles if isinstance(p, dict)]
        bad_profiles = len(raw_profiles) - len(profiles)
        if bad_profiles:
            state.errors.append(f"{bad_profiles} profile entries are not objects")
        join = join_profiles(devices, profiles)
        if join.ambiguous:
            state.errors.append(
                f"{join.ambiguous} profiles name a TargetName shared by several devices"
            )
        self._idrac_hosts = frozenset(
            origin.host for d in devices if d.address and (origin := idrac_origin(d.address))
        )
        self._redfish_auth(state)
        gpus = await self._read_gpus(devices, state, deadline)
        listed = {device.id for device in devices}
        self._cache = {k: v for k, v in self._cache.items() if k in listed}
        servers = tuple(self._server(d, join, gpus[d.id]) for d in devices)
        coverage = {
            "devices_listed": len(raw_devices),
            "servers": len(servers),
            "non_server_devices": skipped,
            "devices_malformed": malformed,
            "profiles": len(raw_profiles),
            "profiles_malformed": bad_profiles,
            "profiles_bound": sum(len(b) for b in join.bindings.values()),
            "profiles_untargeted": join.untargeted,
            "profiles_unmatched": join.unmatched,
            "profiles_ambiguous": join.ambiguous,
            "profile_path": profile_path,
            "servers_with_profile": sum(1 for s in servers if s.profile_name is not None),
            "servers_with_several_profiles": sum(1 for b in join.bindings.values() if len(b) > 1),
            "servers_without_serial": sum(1 for s in servers if not s.serial_normalized),
            "servers_with_gpus": sum(1 for s in servers if s.gpu_models),
            "gpu_ome_inventory": sum(1 for g in gpus.values() if g.source == GPU_SOURCE_OME),
            "gpu_redfish": sum(
                1 for g in gpus.values() if g.source == GPU_SOURCE_REDFISH and g.models is not None
            ),
            "gpu_from_cache": sum(1 for g in gpus.values() if g.cached),
            "gpu_unread": sum(1 for g in gpus.values() if g.models is None),
            "gpu_read_failed": sum(1 for g in gpus.values() if g.failed),
        }
        errors = state.errors[:_MAX_ERRORS]
        if len(state.errors) > _MAX_ERRORS:
            errors.append(f"... and {len(state.errors) - _MAX_ERRORS} more errors")
        return CollectorRead(
            manager=self.name,
            kind=KIND,
            started_at=now,
            finished_at=now,
            complete=not (
                coverage["gpu_read_failed"] or join.ambiguous or malformed or bad_profiles
            ),
            servers=servers,
            errors=tuple(errors),
            coverage=coverage,
        )

    def _secret(self) -> str:
        """
        Read the OME password from its file (re-read on every read, so rotation works).

        Returns:
            str: The password.

        Raises:
            SourceError: The file is missing or empty.
        """
        try:
            return read_secret(self._conn.password_file)
        except (OSError, ValueError) as exc:
            raise SourceError(
                self.name, SourceErrorKind.PERMISSION_DENIED, "cannot read the OME password file"
            ) from exc

    def _redfish_auth(self, state: _ReadState) -> None:
        """
        Prepare the iDRAC Basic auth header, or record why the Redfish fallback is unavailable.

        Args:
            state (_ReadState): The read; receives the header or the reason.
        """
        user, password_file = self._conn.idrac_username, self._conn.idrac_password_file
        if not user or password_file is None:
            state.redfish_note = "the Redfish fallback is not configured"
            return
        try:
            password = read_secret(password_file)
        except (OSError, ValueError):
            state.redfish_note = "cannot read the iDRAC password file"
            state.redfish_failed = True
            state.errors.append(state.redfish_note)
            return
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        state.redfish_headers = {"Authorization": f"Basic {token}"}

    # Adapted from server-scan backend/app/infrastructure/providers/openmanage/client.py@defe172.
    async def _login(self, password: str) -> dict[str, str]:
        """
        Open an API session and return the `X-Auth-Token` header for the read's requests.

        Args:
            password (str): The OME password.

        Returns:
            dict[str, str]: The session header.

        Raises:
            SourceError: PERMISSION_DENIED when OME rejects the account; otherwise by status.
        """
        payload = {"UserName": self._conn.username, "Password": password, "SessionType": "API"}
        try:
            response = await self._ome.post(LOGIN_PATH, json=payload)
        except httpx.TimeoutException as exc:
            raise SourceError(
                self.name, SourceErrorKind.TEMPORARILY_UNAVAILABLE, "login timed out"
            ) from exc
        except httpx.TransportError as exc:
            raise SourceError(
                self.name, SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"login transport: {exc}"
            ) from exc
        status = response.status_code
        if status in (400, 401, 403):
            raise SourceError(
                self.name, SourceErrorKind.PERMISSION_DENIED, f"login rejected (HTTP {status})"
            )
        if not response.is_success:
            raise SourceError(self.name, kind_for_status(status), f"login failed (HTTP {status})")
        token = response.headers.get("X-Auth-Token")
        if not token:
            raise SourceError(self.name, SourceErrorKind.INVALID, "login returned no X-Auth-Token")
        self._token = token
        try:
            body = response.json()
        except ValueError:
            body = None
        session_id = str(body.get("Id") or "") if isinstance(body, dict) else ""
        self._session_id = session_id if _SESSION_ID.fullmatch(session_id) else None
        if self._session_id is None:
            log.warning("openmanage.session_id_unusable", manager=self.name)
        return {"X-Auth-Token": token}

    # Adapted from server-scan backend/app/infrastructure/providers/openmanage/client.py@defe172.
    async def _logout(self) -> None:
        """Delete this client's OME session, best-effort; a failure is logged, never raised."""
        token, session_id = self._token, self._session_id
        if token is None:
            return
        try:
            if session_id is not None:
                response = await self._ome.delete(
                    f"{LOGIN_PATH}('{session_id}')", headers={"X-Auth-Token": token}
                )
                if not response.is_success:
                    log.warning(
                        "openmanage.logout_failed", manager=self.name, status=response.status_code
                    )
        except httpx.HTTPError as exc:
            log.warning("openmanage.logout_failed", manager=self.name, error=type(exc).__name__)
        finally:
            self._token = None
            self._session_id = None

    # Adapted from server-scan backend/app/infrastructure/providers/openmanage/client.py@defe172.
    async def _get_all(self, first: str, headers: Mapping[str, str]) -> list[Any]:
        """
        Fetch every item of a paged OME collection by following `@odata.nextLink`.

        Args:
            first (str): First page, relative to the OME root (may carry a query).
            headers (Mapping[str, str]): The session header.

        Returns:
            list[Any]: Every `value` item across all pages, in order.

        Raises:
            SourceError: A page failed, is malformed, or the links loop or run away.
        """
        path = httpx.URL(first).path
        url, seen, items = first, {first}, []
        for _ in range(_MAX_PAGES):
            body = await get_json(
                self._ome, url, source=self.name, headers=headers, base_delay=self._retry_delay
            )
            page = body.get("value") if isinstance(body, dict) else None
            if not isinstance(page, list):
                raise SourceError(self.name, SourceErrorKind.INVALID, f"{path}: no value array")
            items.extend(page)
            link = body.get("@odata.nextLink")
            if not link:
                return items
            url = self._next_page(link, path)
            if url in seen:
                raise SourceError(self.name, SourceErrorKind.INVALID, f"{path}: nextLink repeats")
            seen.add(url)
        raise SourceError(self.name, SourceErrorKind.INVALID, f"{path}: over {_MAX_PAGES} pages")

    def _next_page(self, link: object, path: str) -> str:
        """
        Resolve an `@odata.nextLink` on our own origin and check it is the same list.

        Only the link's path and query are used, so an OME that names itself by another host
        name still pages on the configured origin.

        Args:
            link (object): The link as received.
            path (str): Path of the list being paged.

        Returns:
            str: Absolute URL of the next page.

        Raises:
            SourceError: The link is malformed, another path, or outside the allow-list.
        """
        try:
            raw = httpx.URL(str(link)).raw_path.decode("ascii")
            target = self._base_url.join(raw)
            if target.path.removeprefix(self._prefix) != path:
                raise UnsafeRequestError("nextLink names another path")
            check_request(
                "GET",
                target,
                self._base_url,
                session_id=self._session_id,
                inventory_types=self._inventory_types,
            )
        except (httpx.InvalidURL, UnicodeDecodeError, UnsafeRequestError) as exc:
            raise SourceError(
                self.name, SourceErrorKind.INVALID, f"{path}: unusable @odata.nextLink"
            ) from exc
        return str(target)

    async def _profiles(self, headers: Mapping[str, str]) -> tuple[list[Any], str]:
        """
        Read every profile, from the OME 3.2 path when the current one is absent (404).

        Args:
            headers (Mapping[str, str]): The session header.

        Returns:
            tuple[list[Any], str]: The profile items, and the path they came from.

        Raises:
            SourceError: The profile list could not be read in full.
        """
        current, legacy = PROFILE_PATHS
        try:
            return await self._get_all(current, headers), current
        except SourceError as exc:
            if exc.kind is not SourceErrorKind.CONFIRMED_ABSENT:
                raise
        return await self._get_all(legacy, headers), legacy

    def _devices(
        self, raw_devices: Sequence[Any], state: _ReadState
    ) -> tuple[list[_Device], int, int]:
        """
        Keep the server devices (`Type == 1000`) of the listing, counting what was dropped.

        Args:
            raw_devices (Sequence[Any]): Device list items.
            state (_ReadState): Receives an error for malformed entries.

        Returns:
            tuple[list[_Device], int, int]: Server devices by id, other device types skipped,
                and malformed entries (not an object, or no integer `Id`).
        """
        devices: dict[int, _Device] = {}
        skipped = malformed = 0
        for item in raw_devices:
            if not isinstance(item, dict):
                malformed += 1
                continue
            if _as_int(item.get("Type")) != SERVER_TYPE:
                skipped += 1
                continue
            device = _parse_device(item)
            if device is None:
                malformed += 1
            else:
                devices[device.id] = device
        if malformed:
            state.errors.append(f"{malformed} device entries have no integer Id")
        return sorted(devices.values(), key=lambda d: d.id), skipped, malformed

    async def _read_gpus(
        self, devices: Sequence[_Device], state: _ReadState, deadline: float
    ) -> dict[int, _Gpu]:
        """
        Read every device's GPU evidence concurrently; what misses the deadline stays unread.

        Args:
            devices (Sequence[_Device]): Server devices.
            state (_ReadState): The read.
            deadline (float): Event-loop time the whole read must end by.

        Returns:
            dict[int, _Gpu]: Evidence per device id.
        """
        tasks = {d.id: asyncio.create_task(self._gpu_for(d, state)) for d in devices}
        if not tasks:
            return {}
        pending: set[asyncio.Task[_Gpu]] = set()
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _, pending = await asyncio.wait(tasks.values(), timeout=remaining)
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        if pending:
            state.errors.append(f"{len(pending)} GPU reads did not finish before the deadline")
        late = _Gpu(None, note="the GPU read did not finish before the deadline", failed=True)
        return {i: late if t in pending else t.result() for i, t in tasks.items()}

    async def _gpu_for(self, device: _Device, state: _ReadState) -> _Gpu:
        """
        One device's GPU evidence: the cache while `LastInventoryTime` is unchanged, else a read.

        Args:
            device (_Device): The device.
            state (_ReadState): The read.

        Returns:
            _Gpu: The evidence.
        """
        entry = self._cache.get(device.id)
        if (
            entry is not None
            and entry.last_inventory == device.last_inventory
            and entry.gpu.read_at is not None
            and state.now - entry.gpu.read_at < self._cache_ttl
        ):
            return replace(entry.gpu, cached=True)
        async with state.semaphore:
            gpu = await self._read_gpu(device, state)
        if gpu.models is not None:
            self._cache[device.id] = _CacheEntry(device.last_inventory, gpu)
        return gpu

    async def _read_gpu(self, device: _Device, state: _ReadState) -> _Gpu:
        """
        Read GPUs from OME inventory; fall back to Redfish when OME lists none or fails.

        Args:
            device (_Device): The device.
            state (_ReadState): The read.

        Returns:
            _Gpu: The evidence.
        """
        ome_failed, answered = False, True
        for inventory_type in self._inventory_types:
            try:
                body = await get_json(
                    self._ome,
                    _inventory_path(device.id, inventory_type),
                    source=self.name,
                    headers=state.ome_headers,
                    base_delay=self._retry_delay,
                )
            except SourceError as exc:
                answered = False
                if exc.kind not in _NO_ANSWER:
                    ome_failed = True
                    state.errors.append(f"device {device.id}: OME {inventory_type}: {exc}")
                continue
            models = gpu_models_from_inventory(body)
            if models is None:
                answered = False
            elif models:
                return _Gpu(models, GPU_SOURCE_OME, inventory_type, read_at=state.now)
        if answered and self._empty_means_none:
            return _Gpu((), GPU_SOURCE_OME, ",".join(self._inventory_types), read_at=state.now)
        return await self._redfish_gpu(device, state, ome_failed)

    async def _redfish_gpu(self, device: _Device, state: _ReadState, ome_failed: bool) -> _Gpu:
        """
        Read GPUs from the device's iDRAC with server-scan's Processors-then-PCIe rule.

        Args:
            device (_Device): The device.
            state (_ReadState): The read.
            ome_failed (bool): The OME inventory read failed (not merely silent).

        Returns:
            _Gpu: The evidence; unread when Redfish is unavailable or fails.
        """
        if state.redfish_headers is None:
            note = f"no GPU in OME inventory; {state.redfish_note}"
            return _Gpu(None, note=note, failed=ome_failed or state.redfish_failed)
        origin = idrac_origin(device.address) if device.address else None
        if origin is None:
            return _Gpu(None, note="OME lists no usable iDRAC address", failed=ome_failed)
        if state.redfish_rejections >= _AUTH_BREAKER and not state.redfish_successes:
            note = "skipped: the iDRAC account was refused by every iDRAC tried"
            return _Gpu(None, GPU_SOURCE_REDFISH, note=note, failed=True)
        try:
            found = await read_gpu_models(
                self._idrac,
                origin,
                source=f"{self.name}/idrac",
                headers=state.redfish_headers,
                pcie_fallback=self._pcie_fallback,
                max_members=self._max_members,
                attempts=_REDFISH_ATTEMPTS,
                base_delay=self._retry_delay,
            )
        except SourceError as exc:
            if exc.kind is SourceErrorKind.PERMISSION_DENIED:
                state.redfish_rejections += 1
            state.errors.append(f"device {device.id}: Redfish: {exc}")
            return _Gpu(None, GPU_SOURCE_REDFISH, note=f"Redfish: {exc.kind}", failed=True)
        except RedfishPayloadError as exc:
            state.errors.append(f"device {device.id}: Redfish: {exc}")
            return _Gpu(None, GPU_SOURCE_REDFISH, note=str(exc)[:200], failed=True)
        state.redfish_successes += 1
        return _Gpu(found.models, GPU_SOURCE_REDFISH, found.rule, found.note, read_at=state.now)

    def _server(self, device: _Device, join: _Join, gpu: _Gpu) -> ManagerServer:
        """
        Build the collector record of one device.

        Args:
            device (_Device): The device.
            join (_Join): Profile bindings.
            gpu (_Gpu): Its GPU evidence.

        Returns:
            ManagerServer: The record.
        """
        bound = join.bindings.get(device.id, [])
        evidence: dict[str, Any] = {
            "device_id": device.id,
            "last_inventory_time": device.last_inventory,
        }
        if bound:
            evidence |= {
                "profile_id": bound[0].profile.id,
                "profile_state": bound[0].profile.state,
                "profile_join": bound[0].method,
            }
            if len(bound) > 1:
                evidence["other_profile_ids"] = [b.profile.id for b in bound[1:]]
        elif device.id in join.ambiguous_devices:
            evidence["profile_join"] = JOIN_AMBIGUOUS
        evidence["gpu_source"] = gpu.source
        extra = {
            "gpu_rule": gpu.rule,
            "gpu_note": gpu.note,
            "gpu_read_at": gpu.read_at.isoformat() if gpu.read_at else None,
        }
        evidence |= {key: value for key, value in extra.items() if value is not None}
        if gpu.cached:
            evidence["gpu_cached"] = True
        return ManagerServer(
            vendor=VENDOR,
            serial_normalized=normalize_serial(device.service_tag),
            manager=self.name,
            source_id=str(device.id),
            name=device.name,
            model=device.model,
            profile_name=bound[0].profile.name if bound else None,
            gpu_models=gpu.models,
            evidence=evidence,
        )
