"""
Read-only async client for one target cluster's kube API (AGENT.md §20.3, §4).

Each fetch lists nodes and GPU-holding pods and walks pod `ownerReferences` to the top owner;
documented facts are in docs/discovery.md, "Target GPU clusters" (rows 1-16, 50-55, D5-D7).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from ttv.config import ClusterConnection
from ttv.models import ClusterNode, ClusterPod, KubeSnapshot, OwnerRef
from ttv.sources.http import (
    SourceError,
    SourceErrorKind,
    UnsafeRequestError,
    get_json,
    tls_verify,
)

log = structlog.get_logger(__name__)

NODES_PATH = "/api/v1/nodes"
PODS_PATH = "/api/v1/pods"
CLUSTER_VERSION_PATH = "/apis/config.openshift.io/v1/clusterversions/version"
APIS_PATH = "/apis"
CORE_V1_PATH = "/api/v1"
PAGE_LIMIT = 500
ACTIVE_PODS_SELECTOR = "status.phase!=Succeeded,status.phase!=Failed"
MAX_OWNER_DEPTH = 8
FRACTION_ANNOTATIONS = ("gpu-fraction", "gpu-memory")
GPU_GROUP_LABEL = "runai-gpu-group"

_REQUEST_TIMEOUT_CAP = 30.0
_FIXED_PATHS = frozenset({"/api", CORE_V1_PATH, APIS_PATH, CLUSTER_VERSION_PATH})
_LIST_PATHS = frozenset({NODES_PATH, PODS_PATH})
_LIST_PARAMS = frozenset({"limit", "continue", "fieldSelector"})
_FINISHED_PHASES = frozenset({"Succeeded", "Failed"})
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_VERSION = re.compile(r"v[1-9][0-9]*(?:(?:alpha|beta)[1-9][0-9]*)?")
_WHOLE = re.compile(r"[0-9]{1,9}")
# Never read as objects: bodies with credentials, and the legacy `watch`/`proxy` prefixes.
_REFUSED_PLURALS = frozenset(
    {
        "secrets",
        "oauthaccesstokens",
        "oauthauthorizetokens",
        "useroauthaccesstokens",
        "oauthclients",
        "watch",
        "proxy",
    }
)


class _PayloadError(ValueError):
    """A kube API payload does not have the expected shape."""


class _ListExpiredError(Exception):
    """The API answered 410 Gone to a `continue` request; the listing must restart."""


class _LookupFailedError(Exception):
    """An owner could not be looked up; the walk stops and the chain is incomplete."""

    def __init__(self, reason: str) -> None:
        """
        Build the error.

        Args:
            reason (str): Short reason code for logs, e.g. `permission_denied`.
        """
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Ref:
    """One `ownerReferences` entry, with `apiVersion` split into group and version."""

    group: str
    version: str
    kind: str
    name: str
    uid: str | None

    @property
    def owner(self) -> OwnerRef:
        """
        The public form of this reference.

        Returns:
            OwnerRef: Group, kind and name.
        """
        return OwnerRef(group=self.group, kind=self.kind, name=self.name)


@dataclass(frozen=True)
class _Resource:
    """A kind as a discovery document lists it."""

    plural: str
    namespaced: bool


@dataclass(frozen=True)
class _PodDraft:
    """A kept pod before its owner walk, and its controller reference."""

    pod: ClusterPod
    owner: _Ref | None


@dataclass
class _OwnerWalk:
    """Per-fetch owner-walk state: shared lookups, the concurrency bound and counters."""

    headers: Mapping[str, str]
    versions: Mapping[str, tuple[str, ...]]
    semaphore: asyncio.Semaphore
    lookups: dict[tuple[str, str, str, str], asyncio.Task[tuple[_Ref | None, str | None]]] = field(
        default_factory=dict
    )
    refreshed: set[tuple[str, str]] = field(default_factory=set)
    owner_gets: int = 0
    discovery_gets: int = 0
    failures: Counter[str] = field(default_factory=Counter)


def _is_name(value: str) -> bool:
    """
    Tell whether a path segment is a DNS-subdomain-style kube name.

    Args:
        value (str): Candidate segment.

    Returns:
        bool: True for lowercase alphanumerics, `-` and `.`, starting and ending alphanumeric.
    """
    return _NAME.fullmatch(value) is not None


def _is_version(value: str) -> bool:
    """
    Tell whether a path segment is a kube API version such as `v1` or `v2alpha1`.

    Args:
        value (str): Candidate segment.

    Returns:
        bool: True for `v<n>`, `v<n>alpha<m>` and `v<n>beta<m>`.
    """
    return _VERSION.fullmatch(value) is not None


def _check_list_query(params: httpx.QueryParams) -> None:
    """
    Allow only `limit`, `continue` and `fieldSelector`, each at most once, on a list path.

    Args:
        params (httpx.QueryParams): The request's query parameters.

    Raises:
        UnsafeRequestError: A parameter is not allowed, repeated, or `limit` is not a number.
    """
    keys = [key for key, _ in params.multi_items()]
    if len(keys) != len(set(keys)) or not set(keys) <= _LIST_PARAMS:
        raise UnsafeRequestError(f"refused list query parameters {sorted(set(keys))}")
    limit = params.get("limit")
    if limit is not None and not limit.isdigit():
        raise UnsafeRequestError("refused a non-numeric limit")


def _is_allowed_path(path: str) -> bool:
    """
    Tell whether a query-less path is a discovery document or a single-object GET.

    Args:
        path (str): Path relative to the API root, e.g. `/apis/apps/v1`.

    Returns:
        bool: True for the fixed paths, `/apis/<group>/<version>`, and one namespaced or
            cluster-scoped object (`.../<plural>/<name>`, no subresource, not a refused plural).
    """
    if path in _FIXED_PATHS:
        return True
    match path.split("/")[1:]:
        case ["apis", group, version]:
            return _is_name(group) and _is_version(version)
        case ["api", "v1", "namespaces", namespace, plural, name]:
            names = (namespace, plural, name)
        case ["apis", group, version, "namespaces", namespace, plural, name] if _is_version(
            version
        ):
            names = (group, namespace, plural, name)
        case ["api", "v1", plural, name] if plural != "namespaces":
            names = (plural, name)
        case ["apis", group, version, plural, name] if plural != "namespaces" and _is_version(
            version
        ):
            names = (group, plural, name)
        case _:
            return False
    return all(_is_name(s) for s in names) and plural not in _REFUSED_PLURALS


def check_request(method: str, url: httpx.URL, base_url: httpx.URL) -> None:
    """
    Refuse every request except the reads this source needs (GET, same origin, allow-listed).

    Allowed: discovery (`/api`, `/api/v1`, `/apis`, `/apis/<g>/<v>`), the cluster version, the
    node and pod lists (`limit`, `continue`, `fieldSelector` only) and single namespaced or
    cluster-scoped objects with no subresource (owner lookups). Secrets and OAuth token/client
    objects, watches and percent-encoded paths are refused.

    Args:
        method (str): HTTP method.
        url (httpx.URL): Absolute request URL.
        base_url (httpx.URL): The cluster's API URL.

    Raises:
        UnsafeRequestError: The request is not on the allow-list.
    """
    if method != "GET":
        raise UnsafeRequestError(f"refused {method}: the kube API is read-only here")
    if (url.scheme, url.host, url.port) != (base_url.scheme, base_url.host, base_url.port):
        raise UnsafeRequestError("refused a request to another origin")
    if url.userinfo or url.fragment:
        raise UnsafeRequestError("refused a URL with userinfo or a fragment")
    if url.raw_path.split(b"?", 1)[0].decode("ascii") != url.path:
        raise UnsafeRequestError("refused a percent-encoded path")
    base_path = base_url.path.rstrip("/")
    if not url.path.startswith(base_path + "/"):
        raise UnsafeRequestError("refused a path outside the API root")
    path = url.path.removeprefix(base_path)
    if path in _LIST_PATHS:
        _check_list_query(url.params)
        return
    if url.query:
        raise UnsafeRequestError("refused a query string outside the node and pod lists")
    if not _is_allowed_path(path):
        raise UnsafeRequestError("refused a path outside the read-only allow-list")


def _obj(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """
    Read an optional JSON object; absent or null gives an empty mapping.

    Args:
        parent (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        Mapping[str, Any]: The value, or `{}`.

    Raises:
        _PayloadError: The value is neither an object nor null.
    """
    value = parent.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _PayloadError(f"{key}: expected an object")
    return value


def _objs(parent: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """
    Read an optional list of JSON objects; absent or null gives an empty list.

    Args:
        parent (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        list[Mapping[str, Any]]: The objects.

    Raises:
        _PayloadError: The value is not a list of objects or null.
    """
    value = parent.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise _PayloadError(f"{key}: expected a list of objects")
    return value


def _str(parent: Mapping[str, Any], key: str) -> str | None:
    """
    Read an optional string.

    Args:
        parent (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        str | None: The value, or None when absent or null.

    Raises:
        _PayloadError: The value is neither a string nor null.
    """
    value = parent.get(key)
    if value is not None and not isinstance(value, str):
        raise _PayloadError(f"{key}: expected a string")
    return value


def _req_str(parent: Mapping[str, Any], key: str) -> str:
    """
    Read a required, non-empty string.

    Args:
        parent (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        str: The value.

    Raises:
        _PayloadError: The value is missing, empty or not a string.
    """
    value = _str(parent, key)
    if not value:
        raise _PayloadError(f"{key}: expected a non-empty string")
    return value


def _time(parent: Mapping[str, Any], key: str) -> datetime | None:
    """
    Read an optional RFC 3339 timestamp as an aware UTC datetime.

    Args:
        parent (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        datetime | None: The value in UTC, or None when absent or null.

    Raises:
        _PayloadError: The value is not a timestamp.
    """
    value = _str(parent, key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _PayloadError(f"{key}: expected an RFC 3339 timestamp") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _string_map(parent: Mapping[str, Any], key: str) -> dict[str, str]:
    """
    Read an optional string-to-string map such as labels or annotations.

    Args:
        parent (Mapping[str, Any]): Container.
        key (str): Field name.

    Returns:
        dict[str, str]: A copy of the map.

    Raises:
        _PayloadError: A value is not a string.
    """
    value = _obj(parent, key)
    if not all(isinstance(v, str) for v in value.values()):
        raise _PayloadError(f"{key}: expected string values")
    return dict(value)


def _whole(quantity: Any) -> int:
    """
    Count whole units of an extended-resource quantity (request == limit, integers only).

    Args:
        quantity (Any): The quantity string, e.g. `"8"`; anything else counts 0.

    Returns:
        int: The integer, or 0 when absent or not a plain integer string.
    """
    if isinstance(quantity, str) and _WHOLE.fullmatch(quantity):
        return int(quantity)
    return 0


def _ready(status: Mapping[str, Any]) -> tuple[bool | None, datetime | None]:
    """
    Read the `Ready` condition.

    Args:
        status (Mapping[str, Any]): A node's or pod's `status`.

    Returns:
        tuple[bool | None, datetime | None]: Whether it is `True` (None when absent), and its
            `lastTransitionTime`.

    Raises:
        _PayloadError: The conditions are malformed.
    """
    for condition in _objs(status, "conditions"):
        if condition.get("type") == "Ready":
            return condition.get("status") == "True", _time(condition, "lastTransitionTime")
    return None, None


def _controller_ref(meta: Mapping[str, Any]) -> _Ref | None:
    """
    Pick the controller owner reference (`controller: true`), else the first one.

    Args:
        meta (Mapping[str, Any]): An object's `metadata`.

    Returns:
        _Ref | None: The reference, or None for an object without owners.

    Raises:
        _PayloadError: The reference is malformed.
    """
    refs = _objs(meta, "ownerReferences")
    if not refs:
        return None
    ref = next((r for r in refs if r.get("controller") is True), refs[0])
    group, _, version = _req_str(ref, "apiVersion").rpartition("/")
    return _Ref(group, version, _req_str(ref, "kind"), _req_str(ref, "name"), _str(ref, "uid"))


def _parse_node(item: Mapping[str, Any], resource: str) -> ClusterNode:
    """
    Build a node from one `NodeList` item.

    Args:
        item (Mapping[str, Any]): The node object.
        resource (str): Extended resource of whole GPUs, e.g. `nvidia.com/gpu`.

    Returns:
        ClusterNode: The node.

    Raises:
        _PayloadError: A field has the wrong type.
    """
    meta = _obj(item, "metadata")
    status = _obj(item, "status")
    unschedulable = _obj(item, "spec").get("unschedulable")
    if unschedulable is not None and not isinstance(unschedulable, bool):
        raise _PayloadError("spec.unschedulable: expected a boolean")
    ready, changed_at = _ready(status)
    return ClusterNode(
        name=_req_str(meta, "name"),
        uid=_req_str(meta, "uid"),
        ready=ready,
        ready_changed_at=changed_at,
        unschedulable=unschedulable is True,
        allocatable_gpus=_whole(_obj(status, "allocatable").get(resource)),
        labels=_string_map(meta, "labels"),
    )


def _pod_gpus(spec: Mapping[str, Any], resource: str) -> int:
    """
    Whole GPUs a pod holds, as Kubernetes computes a pod's effective request.

    That is max(sum of app containers + sum of sidecars, max over regular init containers of
    (init + sidecars declared before it)); sidecars are init containers with
    `restartPolicy: Always`.

    Args:
        spec (Mapping[str, Any]): The pod's `spec`.
        resource (str): Extended resource of whole GPUs.

    Returns:
        int: GPUs from each container's requests, falling back to its limits.

    Raises:
        _PayloadError: The containers are malformed.
    """

    def container_gpus(container: Mapping[str, Any]) -> int:
        """
        GPUs of one container.

        Args:
            container (Mapping[str, Any]): The container spec.

        Returns:
            int: The request, else the limit, else 0.
        """
        resources = _obj(container, "resources")
        requests = _obj(resources, "requests")
        if resource in requests:
            return _whole(requests[resource])
        return _whole(_obj(resources, "limits").get(resource))

    sidecars = 0
    init_peak = 0
    for init in _objs(spec, "initContainers"):
        if init.get("restartPolicy") == "Always":
            sidecars += container_gpus(init)
        else:
            init_peak = max(init_peak, container_gpus(init) + sidecars)
    main = sum(container_gpus(c) for c in _objs(spec, "containers")) + sidecars
    return max(main, init_peak)


def _gpu_group(labels: Mapping[str, str]) -> str | None:
    """
    Read the Run:ai GPU group from `runai-gpu-group=<id>` or a `runai-gpu-group/<id>` key.

    Args:
        labels (Mapping[str, str]): Pod labels.

    Returns:
        str | None: The group id, or None.
    """
    if value := labels.get(GPU_GROUP_LABEL):
        return value
    prefix = GPU_GROUP_LABEL + "/"
    suffixes = sorted(k.removeprefix(prefix) for k in labels if k.startswith(prefix))
    return suffixes[0] if suffixes and suffixes[0] else None


def _parse_pod(item: Mapping[str, Any], resource: str) -> _PodDraft | None:
    """
    Keep a pod bound to a node that holds whole GPUs or carries a Run:ai fraction annotation.

    Args:
        item (Mapping[str, Any]): The pod object.
        resource (str): Extended resource of whole GPUs.

    Returns:
        _PodDraft | None: The pod and its controller reference, or None when not kept.

    Raises:
        _PayloadError: A field the pod needs has the wrong type.
    """
    meta = _obj(item, "metadata")
    spec = _obj(item, "spec")
    status = _obj(item, "status")
    phase = _str(status, "phase") or "Unknown"
    node_name = _str(spec, "nodeName")
    if phase in _FINISHED_PHASES or not node_name:
        return None
    annotations = _obj(meta, "annotations")
    fractional = any(a in annotations for a in FRACTION_ANNOTATIONS)
    gpus = _pod_gpus(spec, resource)
    if gpus == 0 and not fractional:
        return None
    labels = _string_map(meta, "labels")
    statuses = [*_objs(status, "initContainerStatuses"), *_objs(status, "containerStatuses")]
    restarts = [s.get("restartCount", 0) for s in statuses]
    if not all(isinstance(r, int) and not isinstance(r, bool) for r in restarts):
        raise _PayloadError("restartCount: expected an integer")
    waiting = (_str(_obj(_obj(s, "state"), "waiting"), "reason") for s in statuses)
    ready, _ = _ready(status)
    pod = ClusterPod(
        namespace=_req_str(meta, "namespace"),
        name=_req_str(meta, "name"),
        uid=_req_str(meta, "uid"),
        node_name=node_name,
        phase=phase,
        ready=ready is True,
        started_at=_time(status, "startTime"),
        gpus=gpus,
        fractional=fractional,
        gpu_group=_gpu_group(labels),
        labels=labels,
        images=tuple(_str(c, "image") or "" for c in _objs(spec, "containers")),
        waiting_reasons=tuple(reason for reason in waiting if reason),
        restarts=sum(restarts),
    )
    return _PodDraft(pod=pod, owner=_controller_ref(meta))


def _parse_kinds(body: Any) -> dict[str, _Resource]:
    """
    Map kinds to resources from an `APIResourceList` discovery document.

    Args:
        body (Any): Decoded `/api/v1` or `/apis/<group>/<version>` document.

    Returns:
        dict[str, _Resource]: Kind → plural and scope; subresources are skipped.

    Raises:
        _PayloadError: The document is malformed.
    """
    if not isinstance(body, dict):
        raise _PayloadError("discovery: expected an object")
    kinds: dict[str, _Resource] = {}
    for resource in _objs(body, "resources"):
        plural = _req_str(resource, "name")
        if "/" not in plural:
            kinds.setdefault(
                _req_str(resource, "kind"), _Resource(plural, resource.get("namespaced") is True)
            )
    return kinds


def _parse_groups(body: Any) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    """
    Read group names and served versions from the `/apis` `APIGroupList`.

    Args:
        body (Any): Decoded `/apis` document.

    Returns:
        tuple[frozenset[str], dict[str, tuple[str, ...]]]: Group names, and each group's
            versions with the preferred one first.

    Raises:
        _PayloadError: The document is malformed.
    """
    if not isinstance(body, dict):
        raise _PayloadError("/apis: expected an object")
    versions: dict[str, tuple[str, ...]] = {}
    for group in _objs(body, "groups"):
        preferred = _str(_obj(group, "preferredVersion"), "version")
        served = [v for g in _objs(group, "versions") if (v := _str(g, "version"))]
        ordered = [preferred] if preferred else []
        versions[_req_str(group, "name")] = tuple(ordered + [v for v in served if v != preferred])
    return frozenset(versions), versions


class KubeSource:
    """
    Async, GET-only reader of one target cluster's kube API.

    Every request passes `check_request`; `fetch` must not run concurrently with itself.
    """

    def __init__(
        self,
        conn: ClusterConnection,
        *,
        timeout_seconds: float,
        owner_concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Create the client; TLS verification is always on.

        Args:
            conn (ClusterConnection): The cluster's API URL, token file, CA and GPU resource.
            timeout_seconds (float): Deadline for one whole `fetch`.
            owner_concurrency (int): Most owner and discovery GETs in flight.
            transport (httpx.AsyncBaseTransport | None): Test transport; None for the network.

        Raises:
            ValueError: The deadline or concurrency is not positive, or the URL is not https.
        """
        if timeout_seconds <= 0 or owner_concurrency < 1:
            raise ValueError("timeout_seconds and owner_concurrency must be positive")
        self._conn = conn
        self._source = f"kube:{conn.name}"
        self._base_url = httpx.URL(conn.api_url.rstrip("/"))
        if self._base_url.scheme != "https" or not self._base_url.host:
            raise ValueError("api_url must be an absolute https URL")
        self._timeout = timeout_seconds
        self._owner_concurrency = owner_concurrency
        self._kinds: dict[tuple[str, str], dict[str, _Resource]] = {}
        self._discovery_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Accept": "application/json", "User-Agent": "gpu-ttv"},
            timeout=httpx.Timeout(min(timeout_seconds, _REQUEST_TIMEOUT_CAP)),
            verify=tls_verify(conn.ca_bundle),
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [self._guard], "response": [self._on_response]},
        )

    async def __aenter__(self) -> KubeSource:
        """
        Enter an `async with` block.

        Returns:
            KubeSource: This client.
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

    async def _on_response(self, response: httpx.Response) -> None:
        """
        Turn 410 Gone on a list continuation into a restart signal (httpx response hook).

        Args:
            response (httpx.Response): The response, body not yet read.

        Raises:
            _ListExpiredError: The `continue` token expired.
        """
        if response.status_code == 410 and "continue" in response.request.url.params:
            raise _ListExpiredError

    def _error(self, kind: SourceErrorKind, message: str) -> SourceError:
        """
        Build a `SourceError` for this cluster.

        Args:
            kind (SourceErrorKind): Failure kind.
            message (str): Summary without secrets.

        Returns:
            SourceError: The error, to be raised.
        """
        return SourceError(self._source, kind, message)

    def _auth_headers(self) -> dict[str, str]:
        """
        Read the bearer token now (so rotation works) and build the auth header.

        Returns:
            dict[str, str]: The `Authorization` header.

        Raises:
            SourceError: PERMISSION_DENIED when the token file is unreadable, empty or malformed.
        """
        try:
            token = self._conn.token()
        except (OSError, ValueError) as exc:
            raise self._error(
                SourceErrorKind.PERMISSION_DENIED, f"token file unreadable ({type(exc).__name__})"
            ) from None
        if not (token.isascii() and token.isprintable()) or " " in token:
            raise self._error(
                SourceErrorKind.PERMISSION_DENIED, "token file holds a malformed token"
            )
        return {"Authorization": f"Bearer {token}"}

    async def fetch(self, now: datetime) -> KubeSnapshot:
        """
        Read the cluster id, API groups, every node and the GPU-holding pods with their owners.

        Args:
            now (datetime): Scan time, timezone-aware.

        Returns:
            KubeSnapshot: A complete read; owner chains may be marked incomplete.

        Raises:
            SourceError: The cluster id, API groups, node or pod listing failed, was invalid or
                missed the deadline; never a partial node or pod list.
            ValueError: `now` is naive.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        async with self._lock:
            started = time.monotonic()
            deadline = asyncio.get_running_loop().time() + self._timeout
            headers = self._auth_headers()
            gpu = self._conn.gpu_resource
            try:
                async with asyncio.timeout_at(deadline):
                    cluster_id = await self._cluster_id(headers)
                    groups, versions = await self._api_groups(headers)
                    nodes = await self._list(
                        NODES_PATH, headers, None, functools.partial(_parse_node, resource=gpu)
                    )
                    drafts = await self._list(
                        PODS_PATH,
                        headers,
                        ACTIVE_PODS_SELECTOR,
                        functools.partial(_parse_pod, resource=gpu),
                    )
            except TimeoutError:
                raise self._error(
                    SourceErrorKind.TEMPORARILY_UNAVAILABLE,
                    f"deadline of {self._timeout:g}s exceeded",
                ) from None
            except _PayloadError as exc:
                raise self._error(SourceErrorKind.INVALID, f"payload: {exc}") from None
            except httpx.HTTPError as exc:
                raise self._error(
                    SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"http: {type(exc).__name__}"
                ) from None
            pods, walk = await self._resolve_owners(drafts, headers, versions, deadline)
            elapsed = time.monotonic() - started
            snapshot = KubeSnapshot(
                cluster=self._conn.name,
                cluster_id=cluster_id,
                fetched_at=now.astimezone(UTC) + timedelta(seconds=elapsed),
                nodes=tuple(nodes),
                pods=tuple(pods),
                api_groups=groups,
            )
            log.info(
                "kube_fetch",
                cluster=self._conn.name,
                cluster_id=cluster_id,
                nodes=len(snapshot.nodes),
                pods=len(snapshot.pods),
                owner_gets=walk.owner_gets,
                discovery_gets=walk.discovery_gets,
                owners_incomplete=sum(not p.owners_complete for p in snapshot.pods),
                owner_failures=dict(walk.failures),
                elapsed_ms=round(elapsed * 1000),
            )
            return snapshot

    async def _get(
        self,
        path: str,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        *,
        absent_is_invalid: bool = False,
    ) -> Any:
        """
        GET a JSON document through the shared retrying helper.

        Args:
            path (str): Path relative to the API URL.
            headers (Mapping[str, str]): The auth header.
            params (Mapping[str, str] | None): Query parameters.
            absent_is_invalid (bool): Report 404 as INVALID (a list or discovery path that
                must exist), not CONFIRMED_ABSENT.

        Returns:
            Any: The decoded JSON.

        Raises:
            SourceError: The request failed for good.
        """
        try:
            return await get_json(
                self._client, path, source=self._source, params=params, headers=headers
            )
        except SourceError as exc:
            if absent_is_invalid and exc.kind is SourceErrorKind.CONFIRMED_ABSENT:
                raise self._error(SourceErrorKind.INVALID, f"{path} not found") from None
            raise

    async def _cluster_id(self, headers: Mapping[str, str]) -> str | None:
        """
        Read `clusterversion/version` `spec.clusterID` and check it against the configured one.

        Args:
            headers (Mapping[str, str]): The auth header.

        Returns:
            str | None: The cluster id, or None when the API is not OpenShift (404).

        Raises:
            SourceError: INVALID when a configured id is absent or differs, so a wrong URL never
                charges another cluster's nodes; other kinds when the read fails.
            _PayloadError: The document is malformed.
        """
        try:
            body = await self._get(CLUSTER_VERSION_PATH, headers)
        except SourceError as exc:
            if exc.kind is not SourceErrorKind.CONFIRMED_ABSENT:
                raise
            cluster_id = None
        else:
            if not isinstance(body, dict):
                raise _PayloadError("clusterversion: expected an object")
            cluster_id = _str(_obj(body, "spec"), "clusterID")
        expected = self._conn.cluster_id
        if expected is not None and cluster_id != expected:
            raise self._error(
                SourceErrorKind.INVALID,
                f"cluster id {cluster_id or 'absent'} does not match the configured {expected}",
            )
        return cluster_id

    async def _api_groups(
        self, headers: Mapping[str, str]
    ) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
        """
        List the API groups the cluster serves (Run:ai presence, D5; owner kind versions).

        Args:
            headers (Mapping[str, str]): The auth header.

        Returns:
            tuple[frozenset[str], dict[str, tuple[str, ...]]]: Group names, and versions per
                group with the preferred one first.

        Raises:
            SourceError: The read failed.
            _PayloadError: The document is malformed.
        """
        return _parse_groups(await self._get(APIS_PATH, headers, absent_is_invalid=True))

    async def _list[T](
        self,
        path: str,
        headers: Mapping[str, str],
        selector: str | None,
        parse: Callable[[Mapping[str, Any]], T | None],
    ) -> list[T]:
        """
        List every page of a collection, restarting once if a `continue` token expires.

        Args:
            path (str): `NODES_PATH` or `PODS_PATH`.
            headers (Mapping[str, str]): The auth header.
            selector (str | None): `fieldSelector`, if any.
            parse (Callable[[Mapping[str, Any]], T | None]): Item parser; None drops the item.

        Returns:
            list[T]: The parsed items of one consistent listing.

        Raises:
            SourceError: A page failed, or the token expired twice.
            _PayloadError: A page or item is malformed.
        """
        for attempt in (1, 2):
            try:
                return await self._list_once(path, headers, selector, parse)
            except _ListExpiredError:
                log.warning(
                    "kube_list_expired", cluster=self._conn.name, path=path, attempt=attempt
                )
        raise self._error(
            SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"{path}: continue token expired twice"
        )

    async def _list_once[T](
        self,
        path: str,
        headers: Mapping[str, str],
        selector: str | None,
        parse: Callable[[Mapping[str, Any]], T | None],
    ) -> list[T]:
        """
        Follow `metadata.continue` from the first page to the last.

        Args:
            path (str): Collection path.
            headers (Mapping[str, str]): The auth header.
            selector (str | None): `fieldSelector`, if any.
            parse (Callable[[Mapping[str, Any]], T | None]): Item parser; None drops the item.

        Returns:
            list[T]: The parsed items.

        Raises:
            SourceError: A page failed.
            _PayloadError: A page or item is malformed, or the token did not advance.
            _ListExpiredError: The API answered 410 Gone to a continuation.
        """
        items: list[T] = []
        token: str | None = None
        while True:
            params = {"limit": str(PAGE_LIMIT)}
            if selector:
                params["fieldSelector"] = selector
            if token:
                params["continue"] = token
            body = await self._get(path, headers, params, absent_is_invalid=True)
            if not isinstance(body, dict):
                raise _PayloadError(f"{path}: expected an object")
            parsed = (parse(item) for item in _objs(body, "items"))
            items.extend(item for item in parsed if item is not None)
            next_token = _str(_obj(body, "metadata"), "continue")
            if not next_token:
                return items
            if next_token == token:
                raise _PayloadError(f"{path}: continue token did not advance")
            token = next_token

    async def _resolve_owners(
        self,
        drafts: list[_PodDraft],
        headers: Mapping[str, str],
        versions: Mapping[str, tuple[str, ...]],
        deadline: float,
    ) -> tuple[list[ClusterPod], _OwnerWalk]:
        """
        Walk every kept pod's owners concurrently; chains unfinished at the deadline stay partial.

        Args:
            drafts (list[_PodDraft]): Kept pods and their controller references.
            headers (Mapping[str, str]): The auth header.
            versions (Mapping[str, tuple[str, ...]]): Served versions per API group.
            deadline (float): Event-loop time at which the fetch must end.

        Returns:
            tuple[list[ClusterPod], _OwnerWalk]: Pods with owners set, and the walk's counters.
        """
        walk = _OwnerWalk(
            headers=headers,
            versions=versions,
            semaphore=asyncio.Semaphore(self._owner_concurrency),
        )
        chains: list[list[OwnerRef]] = [[] for _ in drafts]
        tasks = {
            i: asyncio.create_task(self._walk(walk, d.pod.namespace, d.owner, chains[i]))
            for i, d in enumerate(drafts)
            if d.owner is not None
        }
        if tasks:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _, pending = await asyncio.wait(tasks.values(), timeout=remaining)
            stragglers = [*pending, *(t for t in walk.lookups.values() if not t.done())]
            for task in stragglers:
                task.cancel()
            await asyncio.gather(*stragglers, return_exceptions=True)
            if pending:
                walk.failures["deadline"] += len(pending)
                log.warning("kube_owner_walk_deadline", cluster=self._conn.name, pods=len(pending))
        pods = []
        for i, draft in enumerate(drafts):
            task = tasks.get(i)
            complete = task is None or (not task.cancelled() and task.result())
            pods.append(
                dataclasses.replace(draft.pod, owners=tuple(chains[i]), owners_complete=complete)
            )
        return pods, walk

    async def _walk(
        self, walk: _OwnerWalk, namespace: str, ref: _Ref, chain: list[OwnerRef]
    ) -> bool:
        """
        Follow controller references upward, appending each to `chain` as it is found.

        Args:
            walk (_OwnerWalk): Per-fetch state.
            namespace (str): The pod's namespace.
            ref (_Ref): The pod's controller reference.
            chain (list[OwnerRef]): Receives the owners, direct owner first.

        Returns:
            bool: True when the top owner was reached within `MAX_OWNER_DEPTH`.
        """
        for _ in range(MAX_OWNER_DEPTH):
            chain.append(ref.owner)
            try:
                parent, namespace = await self._parent(walk, namespace, ref)
            except _LookupFailedError as exc:
                walk.failures[exc.reason] += 1
                return False
            if parent is None:
                return True
            ref = parent
        walk.failures["depth"] += 1
        return False

    async def _parent(self, walk: _OwnerWalk, namespace: str, ref: _Ref) -> tuple[_Ref | None, str]:
        """
        Find the owner of `ref`'s object, sharing one lookup per object across pods.

        Args:
            walk (_OwnerWalk): Per-fetch state.
            namespace (str): Namespace of the referring object; "" when it is cluster-scoped.
            ref (_Ref): Reference to the object.

        Returns:
            tuple[_Ref | None, str]: The object's controller reference (None at the top), and
                the namespace its owner is looked up in ("" when the object is cluster-scoped).

        Raises:
            _LookupFailedError: The kind is unknown or in the wrong scope, the object could not
                be read, or it is not the referenced one.
        """
        resolved = await self._resource(walk, ref)
        if resolved is None:
            raise _LookupFailedError("unknown_kind")
        version, resource = resolved
        if resource.namespaced and not namespace:
            raise _LookupFailedError("invalid_scope")
        scope = namespace if resource.namespaced else ""
        key = (ref.group, ref.kind, scope, ref.name)
        task = walk.lookups.get(key)
        if task is None:
            task = asyncio.create_task(self._lookup(walk, ref, version, resource, scope))
            walk.lookups[key] = task
        parent, uid = await asyncio.shield(task)
        if ref.uid and uid and ref.uid != uid:
            raise _LookupFailedError("uid_mismatch")
        return parent, scope

    async def _lookup(
        self, walk: _OwnerWalk, ref: _Ref, version: str, resource: _Resource, namespace: str
    ) -> tuple[_Ref | None, str | None]:
        """
        GET one owner object, namespaced or cluster-scoped, and read its UID and controller ref.

        Args:
            walk (_OwnerWalk): Per-fetch state.
            ref (_Ref): Reference to the object.
            version (str): API version the kind resolved under.
            resource (_Resource): The kind's plural and scope.
            namespace (str): The object's namespace; "" for a cluster-scoped object.

        Returns:
            tuple[_Ref | None, str | None]: Its controller reference and its UID.

        Raises:
            _LookupFailedError: The path is not allowed, the GET failed or the body is malformed.
        """
        base = f"/apis/{ref.group}/{version}" if ref.group else CORE_V1_PATH
        scope = f"/namespaces/{namespace}" if namespace else ""
        path = f"{base}{scope}/{resource.plural}/{ref.name}"
        if not _is_allowed_path(path):
            raise _LookupFailedError("refused")
        walk.owner_gets += 1
        body = await self._bounded_get(walk, path)
        if not isinstance(body, dict):
            raise _LookupFailedError("invalid")
        try:
            meta = _obj(body, "metadata")
            return _controller_ref(meta), _str(meta, "uid")
        except _PayloadError:
            raise _LookupFailedError("invalid") from None

    async def _bounded_get(self, walk: _OwnerWalk, path: str) -> Any:
        """
        GET one document within the owner-walk concurrency bound.

        Args:
            walk (_OwnerWalk): Per-fetch state.
            path (str): Path relative to the API URL.

        Returns:
            Any: The decoded JSON.

        Raises:
            _LookupFailedError: The GET failed; the reason is the failure kind.
        """
        async with walk.semaphore:
            try:
                return await self._get(path, walk.headers)
            except SourceError as exc:
                raise _LookupFailedError(str(exc.kind)) from None
            except httpx.HTTPError:
                raise _LookupFailedError("http") from None

    async def _resource(self, walk: _OwnerWalk, ref: _Ref) -> tuple[str, _Resource] | None:
        """
        Resolve a reference's kind to a resource: its own version, then the group's preferred.

        Args:
            walk (_OwnerWalk): Per-fetch state.
            ref (_Ref): The reference.

        Returns:
            tuple[str, _Resource] | None: The version used and the resource, or None if unknown.

        Raises:
            _LookupFailedError: A discovery document could not be read.
        """
        if not ref.group:
            candidates: tuple[str, ...] = ("v1",) if ref.version == "v1" else ()
        elif _is_name(ref.group):
            candidates = tuple(dict.fromkeys((ref.version, *walk.versions.get(ref.group, ())[:1])))
        else:
            candidates = ()
        for version in candidates:
            if not _is_version(version):
                continue
            kinds = await self._kinds_of(walk, ref.group, version, ref.kind)
            if ref.kind in kinds:
                return version, kinds[ref.kind]
        return None

    async def _kinds_of(
        self, walk: _OwnerWalk, group: str, version: str, kind: str
    ) -> Mapping[str, _Resource]:
        """
        Return a group version's kinds; cached across fetches, refetched once per fetch on a miss.

        Args:
            walk (_OwnerWalk): Per-fetch state.
            group (str): API group ("" for core).
            version (str): API version.
            kind (str): The kind being resolved (a miss triggers the refetch).

        Returns:
            Mapping[str, _Resource]: Kind → resource; empty when the version is not served.

        Raises:
            _LookupFailedError: The document could not be read this fetch.
        """
        key = (group, version)
        async with self._discovery_lock:
            kinds = self._kinds.get(key)
            if kinds is not None and (kind in kinds or key in walk.refreshed):
                return kinds
            if key in walk.refreshed:
                raise _LookupFailedError("discovery_unavailable")
            walk.refreshed.add(key)
            walk.discovery_gets += 1
            try:
                body = await self._bounded_get(
                    walk, f"/apis/{group}/{version}" if group else CORE_V1_PATH
                )
            except _LookupFailedError as exc:
                if exc.reason != SourceErrorKind.CONFIRMED_ABSENT:
                    raise
                kinds = {}
            else:
                try:
                    kinds = _parse_kinds(body)
                except _PayloadError:
                    raise _LookupFailedError("invalid") from None
            self._kinds[key] = kinds
            return kinds
