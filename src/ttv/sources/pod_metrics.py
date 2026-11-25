"""
Read-only reader of each vLLM pod's own `/metrics` page through the kube API pod proxy (ADR-0009).

One GET per pod, `/api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/metrics`, with the
reader token; RBAC is `get` on `pods/proxy` (deploy/gpu-cluster-rbac/, `podMetrics.enabled`).
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx
import structlog
from prometheus_client.parser import text_string_to_metric_families

from ttv.config import ClusterConnection
from ttv.sources.http import UnsafeRequestError, tls_verify

log = structlog.get_logger(__name__)

MAX_BODY_BYTES = 4 * 1024 * 1024
SCHEMES = ("http", "https")
RBAC_FIX = "grant get on pods/proxy to the reader (deploy/gpu-cluster-rbac/, podMetrics.enabled)"

_REQUEST_TIMEOUT_CAP = 10.0
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_DNS_SUBDOMAIN = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
)
_PROXY_PATH = re.compile(
    r"/api/v1/namespaces/(?P<ns>[^/]+)/pods/"
    r"(?:http|https):(?P<pod>[^/:]+):(?P<port>[1-9][0-9]{0,4})/proxy/metrics"
)
_DENIED = frozenset({401, 403})
_NO_FALLBACK = _DENIED | {404}


@dataclass(frozen=True)
class MetricsTarget:
    """
    One pod whose `/metrics` page is read.

    Attributes:
        namespace (str): Pod namespace.
        pod (str): Pod name.
        port (int): Container port serving `/metrics`.
    """

    namespace: str
    pod: str
    port: int


@dataclass(frozen=True)
class PodMetricsRead:
    """
    Outcome of one fetch; every target is in exactly one of `values` and `failed`.

    Attributes:
        values (Mapping[tuple[str, str], float]): `(namespace, pod)` → the metric summed over
            all its label sets.
        failed (Mapping[tuple[str, str], str]): `(namespace, pod)` → short reason, e.g.
            `http_403`, `metric_absent` or `deadline`.
        permission_denied (bool): Some pod answered 401 or 403 (the reader lacks `pods/proxy`).
    """

    values: Mapping[tuple[str, str], float]
    failed: Mapping[tuple[str, str], str]
    permission_denied: bool


@dataclass(frozen=True)
class _Outcome:
    """One pod's result: a value or a reason, its HTTP status and the scheme that answered."""

    value: float | None = None
    reason: str | None = None
    status: int | None = None
    scheme: str | None = None


def is_namespace(value: str) -> bool:
    """
    Tell whether a string is a valid namespace name (an RFC 1123 DNS label).

    Args:
        value (str): Candidate name.

    Returns:
        bool: True for at most 63 lowercase alphanumerics or `-`, alphanumeric at both ends.
    """
    return _DNS_LABEL.fullmatch(value) is not None


def is_pod_name(value: str) -> bool:
    """
    Tell whether a string is a valid pod name (an RFC 1123 DNS subdomain).

    Args:
        value (str): Candidate name.

    Returns:
        bool: True for at most 253 characters of dot-separated DNS labels.
    """
    return len(value) <= 253 and _DNS_SUBDOMAIN.fullmatch(value) is not None


def is_valid_target(target: MetricsTarget) -> bool:
    """
    Tell whether a target's names and port are safe to put in a proxy path.

    Args:
        target (MetricsTarget): The target.

    Returns:
        bool: True when the namespace and pod are kube names and the port is 1-65535.
    """
    port = target.port
    return (
        is_namespace(target.namespace)
        and is_pod_name(target.pod)
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
    )


def metrics_path(target: MetricsTarget, scheme: str) -> str:
    """
    Build the pod-proxy path of a target's `/metrics` page.

    Args:
        target (MetricsTarget): A target that passed `is_valid_target`.
        scheme (str): `http` or `https`, how the API server talks to the pod.

    Returns:
        str: Path relative to the API URL.
    """
    return (
        f"/api/v1/namespaces/{target.namespace}/pods/"
        f"{scheme}:{target.pod}:{target.port}/proxy/metrics"
    )


def check_request(method: str, url: httpx.URL, base_url: httpx.URL) -> None:
    """
    Refuse every request except a same-origin GET of one pod's `/metrics` through the proxy.

    Args:
        method (str): HTTP method.
        url (httpx.URL): Absolute request URL.
        base_url (httpx.URL): The cluster's API URL.

    Raises:
        UnsafeRequestError: The request is not on the allow-list.
    """
    if method != "GET":
        raise UnsafeRequestError(f"refused {method}: pod metrics are read with GET only")
    if (url.scheme, url.host, url.port) != (base_url.scheme, base_url.host, base_url.port):
        raise UnsafeRequestError("refused a request to another origin")
    if url.userinfo or url.fragment:
        raise UnsafeRequestError("refused a URL with userinfo or a fragment")
    if url.query or b"?" in url.raw_path:
        raise UnsafeRequestError("refused a query string on a pod metrics path")
    if url.raw_path.decode("ascii") != url.path:
        raise UnsafeRequestError("refused a percent-encoded path")
    base_path = base_url.path.rstrip("/")
    if not url.path.startswith(base_path + "/"):
        raise UnsafeRequestError("refused a path outside the API root")
    match = _PROXY_PATH.fullmatch(url.path.removeprefix(base_path))
    if match is None:
        raise UnsafeRequestError("refused a path other than a pod's /proxy/metrics")
    if not is_namespace(match["ns"]) or not is_pod_name(match["pod"]):
        raise UnsafeRequestError("refused a namespace or pod that is not a kube name")
    if int(match["port"]) > 65535:
        raise UnsafeRequestError("refused a port outside 1-65535")


def sum_metric(text: str, metric: str) -> tuple[float | None, str | None]:
    """
    Sum every sample named exactly `metric` across all its labels in a Prometheus text page.

    Args:
        text (str): The `/metrics` body.
        metric (str): Sample name, e.g. `vllm:request_success_total`.

    Returns:
        tuple[float | None, str | None]: The sum, or None and the reason: `parse_error`,
            `metric_absent` or `invalid_value` (a NaN, infinite or negative sample).
    """
    total, seen = 0.0, False
    try:
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                if sample.name != metric:
                    continue
                if not math.isfinite(sample.value) or sample.value < 0:
                    return None, "invalid_value"
                total += sample.value
                seen = True
    except (ValueError, TypeError, IndexError, KeyError):
        return None, "parse_error"
    return (total, None) if seen else (None, "metric_absent")


class PodMetricsSource:
    """
    Async, GET-only reader of vLLM pods' `/metrics` through one cluster's kube API pod proxy.

    Every request passes `check_request`. Fetches run one at a time; within one, at most
    `concurrency` pods are read at once.
    """

    def __init__(
        self,
        conn: ClusterConnection,
        *,
        metric: str,
        timeout_seconds: float,
        concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Create the client; TLS verification is always on, as for `KubeSource`.

        Args:
            conn (ClusterConnection): The cluster's API URL, token file and CA bundle.
            metric (str): Counter to sum, `ServingPolicy.metric`.
            timeout_seconds (float): Deadline for one whole `fetch`; caps each request too.
            concurrency (int): Most pod reads in flight.
            transport (httpx.AsyncBaseTransport | None): Test transport; None for the network.

        Raises:
            ValueError: The metric name, deadline, concurrency or API URL is invalid.
        """
        if timeout_seconds <= 0 or concurrency < 1:
            raise ValueError("timeout_seconds and concurrency must be positive")
        if not _METRIC_NAME.fullmatch(metric):
            raise ValueError(f"invalid Prometheus metric name {metric!r}")
        self._conn = conn
        self.source = f"vllm:{conn.name}"
        self._base_url = httpx.URL(conn.api_url.rstrip("/"))
        if self._base_url.scheme != "https" or not self._base_url.host:
            raise ValueError("api_url must be an absolute https URL")
        self._metric = metric
        self._timeout = timeout_seconds
        self._request_timeout = min(timeout_seconds, _REQUEST_TIMEOUT_CAP)
        self._concurrency = concurrency
        self._schemes: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Accept": "text/plain;version=0.0.4",
                "Accept-Encoding": "identity",
                "User-Agent": "gpu-ttv",
            },
            timeout=httpx.Timeout(self._request_timeout),
            verify=tls_verify(conn.ca_bundle),
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [self._guard]},
        )

    async def __aenter__(self) -> PodMetricsSource:
        """
        Enter an `async with` block.

        Returns:
            PodMetricsSource: This client.
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

    def _auth_headers(self) -> dict[str, str] | None:
        """
        Read the bearer token now (so rotation works) and build the auth header.

        Returns:
            dict[str, str] | None: The `Authorization` header, or None when the token file is
                unreadable, empty or malformed.
        """
        try:
            token = self._conn.token()
        except (OSError, ValueError):
            return None
        if not (token.isascii() and token.isprintable()) or " " in token:
            return None
        return {"Authorization": f"Bearer {token}"}

    async def fetch(self, targets: Sequence[MetricsTarget]) -> PodMetricsRead:
        """
        Read each target's `/metrics` and sum the metric; a pod's failure never fails the fetch.

        Args:
            targets (Sequence[MetricsTarget]): Pods to read, once per `(namespace, pod)` (the
                first target wins); an empty list sends no request.

        Returns:
            PodMetricsRead: Values, per-pod failure reasons and whether the reader was denied.

        Raises:
            UnsafeRequestError: A request left the allow-list (a bug, never a pod failure).
        """
        by_key: dict[tuple[str, str], MetricsTarget] = {}
        for target in targets:
            by_key.setdefault(_key(target), target)
        unique = list(by_key.values())
        if not unique:
            return PodMetricsRead(values={}, failed={}, permission_denied=False)
        async with self._lock:
            started = time.monotonic()
            valid = [t for t in unique if is_valid_target(t)]
            failed = {_key(t): "invalid_target" for t in unique if not is_valid_target(t)}
            if failed:
                log.warning("pod_metrics.invalid_targets", source=self.source, pods=len(failed))
            headers = self._auth_headers()
            if headers is None:
                log.warning("pod_metrics.token_unreadable", source=self.source, pods=len(valid))
                failed.update({_key(t): "token_unreadable" for t in valid})
                valid = []
            outcomes = await self._read_all(valid, headers or {})
            read = self._collect(valid, outcomes, failed, started)
            self._schemes = {k: v for k, v in self._schemes.items() if k in by_key}
        return read

    def _collect(
        self,
        targets: Sequence[MetricsTarget],
        outcomes: Mapping[tuple[str, str], _Outcome],
        failed: dict[tuple[str, str], str],
        started: float,
    ) -> PodMetricsRead:
        """
        Turn per-pod outcomes into the read, remember working schemes and log the fetch.

        Args:
            targets (Sequence[MetricsTarget]): The targets that were requested.
            outcomes (Mapping[tuple[str, str], _Outcome]): Finished pods by key.
            failed (dict[tuple[str, str], str]): Failures so far; pods without an outcome are
                added with reason `deadline`.
            started (float): `time.monotonic()` when the fetch began.

        Returns:
            PodMetricsRead: The read.
        """
        values: dict[tuple[str, str], float] = {}
        denied: Counter[int] = Counter()
        for target in targets:
            key = _key(target)
            outcome = outcomes.get(key, _Outcome(reason="deadline"))
            if outcome.status in _DENIED:
                denied[outcome.status] += 1
            if outcome.scheme is not None:
                self._schemes[key] = outcome.scheme
            if outcome.value is not None:
                values[key] = outcome.value
            else:
                failed[key] = outcome.reason or "unknown"
        if denied:
            log.warning(
                "pod_metrics.permission_denied",
                source=self.source,
                cluster=self._conn.name,
                pods=sum(denied.values()),
                statuses=sorted(denied),
                fix=RBAC_FIX,
            )
        log.info(
            "pod_metrics_fetch",
            source=self.source,
            cluster=self._conn.name,
            targets=len(values) + len(failed),
            read=len(values),
            failed=dict(Counter(failed.values())),
            https=sum(1 for t in targets if self._schemes.get(_key(t)) == "https"),
            duration_s=round(time.monotonic() - started, 3),
        )
        return PodMetricsRead(values=values, failed=failed, permission_denied=bool(denied))

    async def _read_all(
        self, targets: Sequence[MetricsTarget], headers: Mapping[str, str]
    ) -> dict[tuple[str, str], _Outcome]:
        """
        Read the targets within the concurrency bound; pods unfinished at the deadline are omitted.

        Args:
            targets (Sequence[MetricsTarget]): Valid targets.
            headers (Mapping[str, str]): The auth header.

        Returns:
            dict[tuple[str, str], _Outcome]: Outcomes of the pods that finished in time.

        Raises:
            UnsafeRequestError: A request left the allow-list.
        """
        if not targets:
            return {}
        semaphore = asyncio.Semaphore(self._concurrency)
        tasks = {
            _key(t): asyncio.create_task(self._read_pod(t, headers, semaphore)) for t in targets
        }
        try:
            await asyncio.wait(tasks.values(), timeout=self._timeout)
        finally:  # also when the caller cancels the fetch: no read outlives it
            pending = [task for task in tasks.values() if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return {key: task.result() for key, task in tasks.items() if task not in pending}

    async def _read_pod(
        self, target: MetricsTarget, headers: Mapping[str, str], semaphore: asyncio.Semaphore
    ) -> _Outcome:
        """
        Read one pod with its remembered scheme, then once with the other on a proxy error.

        Args:
            target (MetricsTarget): The pod.
            headers (Mapping[str, str]): The auth header.
            semaphore (asyncio.Semaphore): The fetch's concurrency bound.

        Returns:
            _Outcome: The pod's value or failure reason.

        Raises:
            UnsafeRequestError: A request left the allow-list.
        """
        first = self._schemes.get(_key(target), SCHEMES[0])
        async with semaphore:
            outcome = await self._get(target, first, headers)
            status = outcome.status
            if status is not None and not 200 <= status < 300 and status not in _NO_FALLBACK:
                other = SCHEMES[1] if first == SCHEMES[0] else SCHEMES[0]
                outcome = await self._get(target, other, headers)
        return outcome

    async def _get(
        self, target: MetricsTarget, scheme: str, headers: Mapping[str, str]
    ) -> _Outcome:
        """
        GET one pod's `/metrics` with one scheme, reading at most `MAX_BODY_BYTES`.

        Args:
            target (MetricsTarget): The pod.
            scheme (str): `http` or `https`.
            headers (Mapping[str, str]): The auth header.

        Returns:
            _Outcome: The value, or the reason and the HTTP status when there was an answer.

        Raises:
            UnsafeRequestError: The request left the allow-list.
        """
        try:
            async with asyncio.timeout(self._request_timeout):
                body, status, refused = await self._get_body(metrics_path(target, scheme), headers)
        except (TimeoutError, httpx.TimeoutException):
            return _Outcome(reason="timeout")
        except httpx.HTTPError:
            return _Outcome(reason="transport")
        if not 200 <= status < 300:
            return _Outcome(reason=f"http_{status}", status=status)
        if body is None:
            return _Outcome(reason=refused, status=status, scheme=scheme)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return _Outcome(reason="parse_error", status=status, scheme=scheme)
        value, reason = sum_metric(text, self._metric)
        return _Outcome(value=value, reason=reason, status=status, scheme=scheme)

    async def _get_body(
        self, path: str, headers: Mapping[str, str]
    ) -> tuple[bytes | None, int, str]:
        """
        Stream a response, keeping the body only for an unencoded 2xx of at most `MAX_BODY_BYTES`.

        Args:
            path (str): Path relative to the API URL.
            headers (Mapping[str, str]): The auth header.

        Returns:
            tuple[bytes | None, int, str]: The raw body (None when not kept), the status, and
                why a 2xx body was not kept (`too_large` or `encoded`; "" otherwise).
        """
        async with self._client.stream("GET", path, headers=headers) as response:
            status = response.status_code
            if not response.is_success:
                return None, status, ""
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return None, status, "encoded"
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BODY_BYTES:
                return None, status, "too_large"
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return None, status, "too_large"
                chunks.append(chunk)
        return b"".join(chunks), status, ""


def _key(target: MetricsTarget) -> tuple[str, str]:
    """
    Key a target by namespace and pod.

    Args:
        target (MetricsTarget): The target.

    Returns:
        tuple[str, str]: `(namespace, pod)`.
    """
    return target.namespace, target.pod
