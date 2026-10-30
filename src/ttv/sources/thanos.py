"""
Read-only async client for one target cluster's Thanos querier (AGENT.md §20.3, §8).

Each fetch sends a fixed set of instant queries to `GET /api/v1/query`; the PromQL strings
are the documented contract (docs/discovery.md "Target GPU clusters", D1-D4, D8, D11).
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from ttv.config import ClusterConnection, CounterOfRecord
from ttv.models import ThanosSnapshot
from ttv.sources.http import (
    SourceError,
    SourceErrorKind,
    UnsafeRequestError,
    get_json,
    tls_verify,
)

log = structlog.get_logger(__name__)

QUERY_PATH = "/api/v1/query"
POWER_METRIC = "DCGM_FI_DEV_POWER_USAGE"
XID_METRIC = "DCGM_FI_DEV_XID_ERRORS"
PROF_ACTIVITY = "DCGM_FI_PROF_GR_ENGINE_ACTIVE"
UTIL_ACTIVITY = "DCGM_FI_DEV_GPU_UTIL"
MIXED_ACTIVITY = "mixed"
_ALLOWED_PARAMS = frozenset({"query", "time", "partial_response"})
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# Thanos merges PromQL info annotations into `warnings` (lab-tested on OCP 4.22); they say
# nothing about missing stores, so they are logged, not fatal. Any other warning fails.
_INFO_PREFIX = "PromQL info:"


def check_request(method: str, url: httpx.URL, base_url: httpx.URL) -> None:
    """
    Refuse every request except a same-origin GET instant query with known parameters.

    Args:
        method (str): HTTP method.
        url (httpx.URL): Absolute request URL.
        base_url (httpx.URL): The Thanos querier route root.

    Raises:
        UnsafeRequestError: The request is not on the allow-list.
    """
    if method != "GET":
        raise UnsafeRequestError(f"refused {method}: the Thanos querier is read-only")
    if (url.scheme, url.host, url.port) != (base_url.scheme, base_url.host, base_url.port):
        raise UnsafeRequestError("refused a request to another origin")
    if url.path != base_url.path.rstrip("/") + QUERY_PATH:
        raise UnsafeRequestError(f"refused a path other than {QUERY_PATH}")
    keys = [key for key, _ in url.params.multi_items()]
    if not set(keys) <= _ALLOWED_PARAMS or len(keys) != len(set(keys)):
        raise UnsafeRequestError("refused unknown or repeated query parameters")


def format_window(window: timedelta) -> str:
    """
    Format a PromQL range as whole seconds, e.g. `86400s`.

    Args:
        window (timedelta): The range; at least one second.

    Returns:
        str: The range duration.

    Raises:
        ValueError: The window is shorter than one second.
    """
    seconds = int(window.total_seconds())
    if seconds < 1:
        raise ValueError(f"a PromQL window must be at least 1s, got {window}")
    return f"{seconds}s"


@dataclass(frozen=True)
class Sample:
    """
    One finite sample of an instant-vector result.

    Attributes:
        labels (Mapping[str, str]): Series labels.
        value (float): Sample value.
    """

    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True)
class Queries:
    """
    The PromQL one fetch sends, in this order.

    Attributes:
        gpus (str): Distinct GPU UUIDs per node (D1, D2).
        xid (str): Non-zero last XID per GPU.
        prof (str): Peak `GR_ENGINE_ACTIVE` per pod over the activity window (0-1).
        util (str): Peak `GPU_UTIL` per pod over the activity window, scaled to 0-1.
        counters (tuple[tuple[str, str], ...]): `(selector, promql)` per distinct counter.
    """

    gpus: str
    xid: str
    prof: str
    util: str
    counters: tuple[tuple[str, str], ...]

    def all(self) -> list[str]:
        """
        List every query in the order a fetch sends them.

        Returns:
            list[str]: The PromQL strings.
        """
        return [self.gpus, self.xid, self.prof, self.util, *(p for _, p in self.counters)]


def build_queries(
    conn: ClusterConnection,
    counters: Sequence[CounterOfRecord],
    demand_window: timedelta,
    activity_window: timedelta,
) -> Queries:
    """
    Build the fetch's PromQL from the connection's label names and the policy windows.

    Args:
        conn (ClusterConnection): Supplies the DCGM and serving label names.
        counters (Sequence[CounterOfRecord]): Serving counters; repeated selectors are dropped.
        demand_window (timedelta): `increase()` range for serving counters.
        activity_window (timedelta): `max_over_time()` range for GPU activity.

    Returns:
        Queries: The queries.

    Raises:
        ValueError: A label name is not a valid Prometheus label or a window is under 1s.
    """
    labels = (
        conn.dcgm_node_label,
        conn.dcgm_uuid_label,
        conn.dcgm_pod_label,
        conn.dcgm_namespace_label,
        conn.serving_pod_label,
        conn.serving_namespace_label,
    )
    bad = [name for name in labels if not _LABEL_NAME.fullmatch(name)]
    if bad:
        raise ValueError(f"{conn.name}: invalid Prometheus label names {bad}")
    node, uuid = conn.dcgm_node_label, conn.dcgm_uuid_label
    ns, pod = conn.dcgm_namespace_label, conn.dcgm_pod_label
    activity = format_window(activity_window)
    demand = format_window(demand_window)
    by_pod = f"{conn.serving_namespace_label}, {conn.serving_pod_label}"

    def peak(metric: str) -> str:
        """
        Peak of a DCGM field per workload pod over the activity window.

        Args:
            metric (str): DCGM field.

        Returns:
            str: The PromQL.
        """
        return f'max by ({ns}, {pod}) (max_over_time({metric}{{{pod}!=""}}[{activity}]))'

    return Queries(
        gpus=f"group by ({node}, {uuid}) ({POWER_METRIC})",
        xid=f"max by ({node}, {uuid}) ({XID_METRIC}) != 0",
        prof=peak(PROF_ACTIVITY),
        util=f"{peak(UTIL_ACTIVITY)} / 100",
        counters=tuple(
            (sel, f"sum by ({by_pod}) (increase({sel}[{demand}]))")
            for sel in dict.fromkeys(c.selector for c in counters)
        ),
    )


def parse_vector(body: Any, *, source: str, query: str) -> tuple[list[Sample], int]:
    """
    Validate an instant-query response and return its finite samples.

    Args:
        body (Any): Decoded JSON of `GET /api/v1/query`.
        source (str): Source name for errors.
        query (str): The PromQL, for errors and logs.

    Returns:
        tuple[list[Sample], int]: Finite samples, and how many NaN/±Inf samples were dropped.

    Raises:
        SourceError: The query failed, returned warnings, or is not a well-formed vector.
    """
    if not isinstance(body, dict):
        raise SourceError(source, SourceErrorKind.INVALID, f"response is not an object: {query}")
    if body.get("status") != "success":
        error = f"{body.get('errorType')}: {body.get('error')}"
        raise SourceError(source, SourceErrorKind.INVALID, f"query failed ({error}): {query}")
    warnings = body.get("warnings") or []
    if not isinstance(warnings, list):
        raise SourceError(source, SourceErrorKind.INVALID, f"warnings is not a list: {query}")
    infos = [w for w in warnings if isinstance(w, str) and w.startswith(_INFO_PREFIX)]
    if len(infos) != len(warnings):
        raise SourceError(
            source,
            SourceErrorKind.TEMPORARILY_UNAVAILABLE,
            f"partial or degraded answer, {len(warnings) - len(infos)} warning(s): {query}",
        )
    if infos:
        log.debug("thanos.query_info", source=source, query=query, infos=infos)
    data = body.get("data")
    result_type = data.get("resultType") if isinstance(data, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    if result_type != "vector" or not isinstance(result, list):
        raise SourceError(
            source, SourceErrorKind.INVALID, f"expected a vector, got {result_type}: {query}"
        )
    samples: list[Sample] = []
    dropped = 0
    for item in result:
        metric = item.get("metric") if isinstance(item, dict) else None
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) != 2:
            raise SourceError(source, SourceErrorKind.INVALID, f"malformed sample: {query}")
        try:
            number = float(value[1])
        except (TypeError, ValueError) as exc:
            raise SourceError(
                source, SourceErrorKind.INVALID, f"non-numeric sample: {query}"
            ) from exc
        if not math.isfinite(number):
            dropped += 1
            continue
        samples.append(Sample({str(k): str(v) for k, v in metric.items()}, number))
    return samples, dropped


@dataclass
class _Tally:
    """Series a fetch could not use, for the fetch log."""

    skipped_series: int = 0
    rejected_samples: int = 0


def _keyed(
    samples: Sequence[Sample],
    labels: tuple[str, str],
    tally: _Tally,
    *,
    low: float = -math.inf,
    high: float = math.inf,
) -> dict[tuple[str, str], float]:
    """
    Key samples by two labels; skip series lacking either and reject values out of range.

    Args:
        samples (Sequence[Sample]): Finite samples.
        labels (tuple[str, str]): The two label names, both required non-empty.
        tally (_Tally): Counts what was skipped or rejected.
        low (float): Lowest valid value.
        high (float): Highest valid value.

    Returns:
        dict[tuple[str, str], float]: Label values → sample value.
    """
    out: dict[tuple[str, str], float] = {}
    for sample in samples:
        first, second = (sample.labels.get(name, "") for name in labels)
        if not first or not second:
            tally.skipped_series += 1
        elif not low <= sample.value <= high:
            tally.rejected_samples += 1
        else:
            out[(first, second)] = sample.value
    return out


def merge_activity(
    prof: Mapping[tuple[str, str], float], util: Mapping[tuple[str, str], float]
) -> tuple[dict[tuple[str, str], float], str | None]:
    """
    Combine per-pod activity, preferring `GR_ENGINE_ACTIVE` over `GPU_UTIL` (D4).

    Args:
        prof (Mapping[tuple[str, str], float]): `(namespace, pod)` → PROF ratio.
        util (Mapping[tuple[str, str], float]): `(namespace, pod)` → UTIL scaled to 0-1.

    Returns:
        tuple[dict[tuple[str, str], float], str | None]: Activity per pod, and the field it
            came from: one field name, `mixed`, or None when there is none.
    """
    used = [PROF_ACTIVITY] if prof else []
    if util.keys() - prof.keys():
        used.append(UTIL_ACTIVITY)
    metric = None if not used else used[0] if len(used) == 1 else MIXED_ACTIVITY
    return {**util, **prof}, metric


class ThanosSource:
    """
    Async, GET-only client for one cluster's Thanos querier; returns one `ThanosSnapshot`.

    Every request passes `check_request`. Queries run one at a time, also across concurrent
    `fetch` calls (Red Hat asks for at most one query every 30 s; discovery row 36).
    """

    def __init__(
        self,
        conn: ClusterConnection,
        *,
        counters: Sequence[CounterOfRecord],
        demand_window: timedelta,
        activity_window: timedelta,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Create the client; TLS verification is always on.

        Args:
            conn (ClusterConnection): Route, token file, CA bundle and label names.
            counters (Sequence[CounterOfRecord]): Serving counters, e.g. `Policy.counters()`.
            demand_window (timedelta): `ServingPolicy.demand_window`.
            activity_window (timedelta): `AllocatedIdlePolicy.window`.
            timeout_seconds (float): Deadline for one whole fetch (and for each request).
            transport (httpx.AsyncBaseTransport | None): Test transport; None for the network.

        Raises:
            ValueError: A label name, window or timeout is invalid, or the URL is not absolute.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._conn = conn
        self.source = f"thanos:{conn.name}"
        self._base_url = httpx.URL(conn.thanos_url.rstrip("/"))
        if self._base_url.scheme != "https" or not self._base_url.host:
            raise ValueError(f"{conn.name}: thanos_url must be an absolute https URL")
        self.queries = build_queries(conn, counters, demand_window, activity_window)
        self._timeout = timeout_seconds
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Accept": "application/json", "User-Agent": "gpu-ttv"},
            timeout=httpx.Timeout(timeout_seconds),
            verify=tls_verify(conn.thanos_ca_bundle),
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [self._guard]},
        )

    async def __aenter__(self) -> ThanosSource:
        """
        Enter an `async with` block.

        Returns:
            ThanosSource: This client.
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

    async def fetch(self, now: datetime) -> ThanosSnapshot:
        """
        Run every query at `now`; all succeed or the fetch fails (never a partial snapshot).

        Args:
            now (datetime): Evaluation time, timezone-aware; becomes `fetched_at`.

        Returns:
            ThanosSnapshot: GPUs per node, XIDs, pod activity and serving counters.

        Raises:
            SourceError: The token is unreadable, a query failed, or the deadline passed.
            ValueError: `now` is naive.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        try:
            token = self._conn.token()
        except (OSError, ValueError) as exc:
            raise SourceError(
                self.source, SourceErrorKind.PERMISSION_DENIED, f"cannot read token: {exc}"
            ) from exc
        headers = {"Authorization": f"Bearer {token}"}
        async with self._lock:
            started = time.monotonic()
            try:
                async with asyncio.timeout(self._timeout):
                    snapshot, tally = await self._read(now, headers)
            except TimeoutError as exc:
                raise SourceError(
                    self.source,
                    SourceErrorKind.TEMPORARILY_UNAVAILABLE,
                    f"fetch exceeded its {self._timeout:g}s deadline",
                ) from exc
        log.info(
            "thanos_fetch",
            cluster=self._conn.name,
            queries=len(self.queries.all()),
            nodes_with_dcgm=len(snapshot.gpu_uuids_by_node),
            gpus=sum(len(uuids) for uuids in snapshot.gpu_uuids_by_node.values()),
            nodes_with_xid=len(snapshot.last_xid_by_node),
            pods_with_activity=len(snapshot.activity_by_pod),
            activity_metric=snapshot.activity_metric,
            counters_with_series=list(snapshot.counters),
            counters_absent=[s for s, _ in self.queries.counters if s not in snapshot.counters],
            **dataclasses.asdict(tally),
            duration_s=round(time.monotonic() - started, 3),
        )
        return snapshot

    async def _read(
        self, now: datetime, headers: Mapping[str, str]
    ) -> tuple[ThanosSnapshot, _Tally]:
        """
        Send the queries in order and assemble the snapshot.

        Args:
            now (datetime): Evaluation time in UTC.
            headers (Mapping[str, str]): The bearer header.

        Returns:
            tuple[ThanosSnapshot, _Tally]: The snapshot, and what was skipped or rejected.

        Raises:
            SourceError: Any query failed.
        """
        conn, q, tally = self._conn, self.queries, _Tally()

        async def run(promql: str) -> list[Sample]:
            """
            Run one query and tally its dropped samples.

            Args:
                promql (str): The query.

            Returns:
                list[Sample]: Finite samples.
            """
            samples, dropped = await self._query_vector(promql, now, headers)
            tally.rejected_samples += dropped
            return samples

        dcgm_gpu = (conn.dcgm_node_label, conn.dcgm_uuid_label)
        dcgm_pod = (conn.dcgm_namespace_label, conn.dcgm_pod_label)
        serving_pod = (conn.serving_namespace_label, conn.serving_pod_label)
        gpus: dict[str, set[str]] = {}
        for node, uuid in _keyed(await run(q.gpus), dcgm_gpu, tally):
            gpus.setdefault(node, set()).add(uuid)
        xids: dict[str, dict[str, float]] = {}
        for (node, uuid), xid in _keyed(await run(q.xid), dcgm_gpu, tally).items():
            xids.setdefault(node, {})[uuid] = xid
        prof = _keyed(await run(q.prof), dcgm_pod, tally, low=0.0, high=1.0)
        util = _keyed(await run(q.util), dcgm_pod, tally, low=0.0, high=1.0)
        activity, activity_metric = merge_activity(prof, util)
        counters: dict[str, dict[tuple[str, str], float]] = {}
        for selector, promql in q.counters:
            if values := _keyed(await run(promql), serving_pod, tally, low=0.0):
                counters[selector] = values
        if tally.skipped_series or tally.rejected_samples:
            log.debug("thanos.series_dropped", source=self.source, **dataclasses.asdict(tally))
        snapshot = ThanosSnapshot(
            cluster=conn.name,
            fetched_at=now,
            gpu_uuids_by_node={node: frozenset(uuids) for node, uuids in gpus.items()},
            last_xid_by_node=xids,
            activity_by_pod=activity,
            activity_metric=activity_metric,
            counters=counters,
        )
        return snapshot, tally

    async def _query_vector(
        self, promql: str, now: datetime, headers: Mapping[str, str]
    ) -> tuple[list[Sample], int]:
        """
        Run one instant query at `now` with partial responses disabled.

        Args:
            promql (str): The query.
            now (datetime): Evaluation time.
            headers (Mapping[str, str]): The bearer header.

        Returns:
            tuple[list[Sample], int]: Finite samples, and the number of NaN/±Inf ones dropped.

        Raises:
            SourceError: The request or the query failed.
        """
        params = {"query": promql, "time": f"{now.timestamp():.3f}", "partial_response": "false"}
        body = await get_json(
            self._client, QUERY_PATH, source=self.source, params=params, headers=headers
        )
        return parse_vector(body, source=self.source, query=promql)
