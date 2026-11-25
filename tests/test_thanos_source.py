"""Tests for the read-only Thanos querier source (fixture-tested; lab captures under lab/)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from ttv.config import ClusterConnection, Policy
from ttv.models import ThanosSnapshot
from ttv.sources.http import SourceError, SourceErrorKind
from ttv.sources.serverscan import UnsafeRequestError
from ttv.sources.thanos import (
    MIXED_ACTIVITY,
    PROF_ACTIVITY,
    QUERY_PATH,
    UTIL_ACTIVITY,
    ThanosSource,
    check_request,
    format_window,
    merge_activity,
    parse_vector,
)

FIXTURES = Path(__file__).parent / "fixtures" / "thanos"
BASE_URL = "https://thanos-querier-openshift-monitoring.apps.ocp4-prod-gpu-site1.test"
NOW = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
NOW_PARAM = "1793397600.000"
TOKEN = "synthetic-token-7f3a-never-log-me"

Q_GPUS = "group by (Hostname, UUID) (DCGM_FI_DEV_POWER_USAGE)"
Q_XID = "max by (Hostname, UUID) (DCGM_FI_DEV_XID_ERRORS) != 0"
Q_PROF = (
    "max by (exported_namespace, exported_pod) "
    '(max_over_time(DCGM_FI_PROF_GR_ENGINE_ACTIVE{exported_pod!=""}[21600s]))'
)
Q_UTIL = (
    "max by (exported_namespace, exported_pod) "
    '(max_over_time(DCGM_FI_DEV_GPU_UTIL{exported_pod!=""}[21600s])) / 100'
)
PER_GPU = "Hostname, UUID, exported_namespace, exported_pod, modelName"
Q_POWER_PEAK = f"max by ({PER_GPU}) (max_over_time(DCGM_FI_DEV_POWER_USAGE[21600s]))"
Q_POWER_AVG = f"max by ({PER_GPU}) (avg_over_time(DCGM_FI_DEV_POWER_USAGE[86400s]))"
Q_POWER_LIMIT = "max by (Hostname, UUID) (DCGM_FI_DEV_POWER_MGMT_LIMIT)"
DEFAULT_QUERIES = [Q_GPUS, Q_XID, Q_PROF, Q_UTIL, Q_POWER_PEAK, Q_POWER_AVG, Q_POWER_LIMIT]

Reply = Callable[[], httpx.Response]


def fixture(rel: str) -> Reply:
    """A reply serving a fixture body with the HTTP status recorded in its `_meta`."""
    data = json.loads((FIXTURES / rel).read_text())
    status = data.pop("_meta")["http_status"]
    return lambda: httpx.Response(status, json=data)


def status(code: int, **headers: str) -> Reply:
    return lambda: httpx.Response(code, text="synthetic", headers=headers)


def uuid(node: str, i: int) -> str:
    return f"GPU-0000000{node}-0000-0000-0000-00000000000{i}"


class FakeThanos:
    """A MockTransport handler: replies per PromQL (consumed in order, last repeats)."""

    def __init__(self, replies: dict[str, Reply | list[Reply]] | None = None) -> None:
        self.replies = {q: r if isinstance(r, list) else [r] for q, r in (replies or {}).items()}
        self.requests: list[httpx.Request] = []
        self.delay = 0.0
        self.in_flight = 0
        self.max_in_flight = 0

    def queries(self) -> list[str]:
        return [r.url.params["query"] for r in self.requests]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        replies = self.replies.get(request.url.params.get("query", ""))
        if not replies:
            return fixture("lab/empty-vector.json")()
        return (replies.pop(0) if len(replies) > 1 else replies[0])()


def make_conn(tmp_path: Path, token: str = TOKEN, **overrides: Any) -> ClusterConnection:
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    return ClusterConnection(
        name="ocp4-prod-gpu-site1",
        api_url="https://api.ocp4-prod-gpu-site1.test:6443",
        thanos_url=BASE_URL,
        token_file=token_file,
        **overrides,
    )


def make_source(
    fake: FakeThanos,
    conn: ClusterConnection,
    policy: Policy,
    *,
    timeout: float = 30.0,
) -> ThanosSource:
    return ThanosSource(
        conn,
        activity_window=policy.allocated_idle.window,
        timeout_seconds=timeout,
        power_window=policy.serving.demand_window,
        rated_watts=policy.power.rated_watts,
        transport=httpx.MockTransport(fake.handler),
    )


def vector(*series: tuple[dict[str, str], float]) -> Reply:
    """A successful instant-query reply holding these (labels, value) samples."""
    result = [{"metric": labels, "value": [NOW.timestamp(), str(v)]} for labels, v in series]
    body = {"status": "success", "data": {"resultType": "vector", "result": result}}
    return lambda: httpx.Response(200, json=body)


async def test_fetch_sends_the_documented_promql_in_order(policy: Policy, tmp_path: Path) -> None:
    fake = FakeThanos()
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        snapshot = await source.fetch(NOW)
    assert fake.queries() == DEFAULT_QUERIES
    for request in fake.requests:
        assert request.method == "GET"
        assert str(request.url).startswith(BASE_URL + QUERY_PATH + "?")
        assert dict(request.url.params) == {
            "query": request.url.params["query"],
            "time": NOW_PARAM,
            "partial_response": "false",
        }
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert snapshot == ThanosSnapshot(cluster="ocp4-prod-gpu-site1", fetched_at=NOW)


async def test_lab_cluster_shape_is_all_empty(policy: Policy, tmp_path: Path) -> None:
    fake = FakeThanos()
    with capture_logs() as events:
        async with make_source(fake, make_conn(tmp_path), policy) as source:
            snapshot = await source.fetch(NOW)
    assert snapshot.activity_metric is None
    [event] = [e for e in events if e["event"] == "thanos_fetch"]
    assert event["log_level"] == "info"
    assert event["cluster"] == "ocp4-prod-gpu-site1"
    assert event["queries"] == len(DEFAULT_QUERIES)
    assert event["nodes_with_dcgm"] == event["pods_with_activity"] == 0
    assert not any(key.startswith("counters") for key in event)
    assert event["duration_s"] >= 0


async def test_gpus_counts_distinct_uuids_per_node_and_xid_evidence(
    policy: Policy, tmp_path: Path
) -> None:
    fake = FakeThanos(
        {Q_GPUS: fixture("synthetic/dcgm-gpus.json"), Q_XID: fixture("synthetic/dcgm-xid.json")}
    )
    with capture_logs() as events:
        async with make_source(fake, make_conn(tmp_path), policy) as source:
            snapshot = await source.fetch(NOW)
    assert snapshot.gpu_uuids_by_node == {
        "synthetic-gpu-a": frozenset(uuid("a", i) for i in range(8)),
        "synthetic-gpu-b": frozenset(uuid("b", i) for i in range(7)),
    }
    assert snapshot.last_xid_by_node == {"synthetic-gpu-b": {uuid("b", 3): 79.0}}
    [fetch_event] = [e for e in events if e["event"] == "thanos_fetch"]
    assert fetch_event["gpus"] == 15
    assert fetch_event["skipped_series"] == 2
    assert fetch_event["rejected_samples"] == 1
    assert any(e["event"] == "thanos.series_dropped" for e in events)


async def test_activity_prefers_prof_falls_back_to_util_and_reports_mixed(
    policy: Policy, tmp_path: Path
) -> None:
    fake = FakeThanos(
        {
            Q_PROF: fixture("synthetic/activity-prof.json"),
            Q_UTIL: fixture("synthetic/activity-util.json"),
        }
    )
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        snapshot = await source.fetch(NOW)
    assert snapshot.activity_by_pod == {
        ("ml-train", "trainer-0"): 0.82,
        ("ml-train", "notebook-0"): 0.01,
        ("serving", "both-0"): 0.4,
        ("serving", "nan-0"): 0.02,
        ("serving", "sentinel-0"): 0.07,
        ("legacy", "old-gpu-0"): 0.03,
    }
    assert snapshot.activity_metric == MIXED_ACTIVITY


@pytest.mark.parametrize(
    ("prof", "util", "metric"),
    [
        ({}, {}, None),
        ({("a", "p"): 0.5}, {}, PROF_ACTIVITY),
        ({}, {("a", "p"): 0.5}, UTIL_ACTIVITY),
        ({("a", "p"): 0.5}, {("a", "p"): 0.9}, PROF_ACTIVITY),
        ({("a", "p"): 0.5}, {("a", "q"): 0.9}, MIXED_ACTIVITY),
    ],
)
def test_merge_activity_names_the_field_used(
    prof: dict[tuple[str, str], float], util: dict[tuple[str, str], float], metric: str | None
) -> None:
    activity, used = merge_activity(prof, util)
    assert used == metric
    assert activity == {**util, **prof}


async def test_power_is_a_share_of_rated_power_per_pod_and_for_gpus_with_no_pod(
    policy: Policy, tmp_path: Path
) -> None:
    """ADR-0009: the power limit wins over the model table; unrated GPUs and bad values drop."""

    def gpu(i: int, pod: str = "", model: str = "NVIDIA H100 80GB HBM3") -> dict[str, str]:
        labels = {"Hostname": "gpu-a", "UUID": f"GPU-{i}", "modelName": model}
        if pod:
            labels |= {"exported_namespace": "team-a", "exported_pod": pod}
        return labels

    fake = FakeThanos(
        {
            Q_POWER_PEAK: vector(
                (gpu(0, "train-0"), 560.0),
                (gpu(1, "train-0"), 70.0),
                (gpu(2), 350.0),
                (gpu(3), 60.0),
                (gpu(4, "odd", model="Some Future GPU"), 900.0),
                (gpu(5, "bad"), 9000.0),
                (gpu(6, "pcie", model="NVIDIA H100 PCIe"), 100.0),
            ),
            Q_POWER_AVG: vector((gpu(0, "train-0"), 350.0), (gpu(1, "train-0"), 140.0)),
            Q_POWER_LIMIT: vector(({"Hostname": "gpu-a", "UUID": "GPU-1"}, 350.0)),
        }
    )
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        snapshot = await source.fetch(NOW)
    train = snapshot.power_by_pod[("team-a", "train-0")]
    assert (train.peak, train.gpus) == (pytest.approx(0.8), 2)
    assert train.average == pytest.approx((350 / 700 + 140 / 350) / 2)
    assert snapshot.power_by_pod[("team-a", "pcie")].peak == pytest.approx(100 / 350)
    assert ("team-a", "odd") not in snapshot.power_by_pod
    assert ("team-a", "bad") not in snapshot.power_by_pod
    assert snapshot.free_gpu_power_by_node == {"gpu-a": (0.5, pytest.approx(60 / 700))}


async def test_labels_and_window_come_from_config(tmp_path: Path) -> None:
    conn = make_conn(
        tmp_path,
        dcgm_node_label="node",
        dcgm_uuid_label="gpu_uuid",
        dcgm_pod_label="pod",
        dcgm_namespace_label="namespace",
    )
    fake = FakeThanos()
    source = ThanosSource(
        conn,
        activity_window=timedelta(minutes=90),
        timeout_seconds=30,
        transport=httpx.MockTransport(fake.handler),
    )
    async with source:
        await source.fetch(NOW)
    assert fake.queries() == [
        "group by (node, gpu_uuid) (DCGM_FI_DEV_POWER_USAGE)",
        "max by (node, gpu_uuid) (DCGM_FI_DEV_XID_ERRORS) != 0",
        'max by (namespace, pod) (max_over_time(DCGM_FI_PROF_GR_ENGINE_ACTIVE{pod!=""}[5400s]))',
        'max by (namespace, pod) (max_over_time(DCGM_FI_DEV_GPU_UTIL{pod!=""}[5400s])) / 100',
        "max by (node, gpu_uuid, namespace, pod, modelName)"
        " (max_over_time(DCGM_FI_DEV_POWER_USAGE[5400s]))",
        "max by (node, gpu_uuid, namespace, pod, modelName)"
        " (avg_over_time(DCGM_FI_DEV_POWER_USAGE[5400s]))",
        "max by (node, gpu_uuid) (DCGM_FI_DEV_POWER_MGMT_LIMIT)",
    ]


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        (
            "lab/increase-requests.json",
            {
                ("openshift-monitoring", "prometheus-k8s-0"): 2318.64406779661,
                ("openshift-user-workload-monitoring", "prometheus-user-workload-0"): (
                    2302.372881355932
                ),
            },
        ),
        (
            "lab/group-up.json",
            {
                ("default", ""): 1.0,
                ("openshift-apiserver", "apiserver-7dfc7df8d9-mt9xc"): 1.0,
                ("openshift-oauth-apiserver", "apiserver-6744d9ffd7-gqt7g"): 1.0,
                ("openshift-gitops", "openshift-gitops-application-controller-0"): 1.0,
            },
        ),
    ],
)
def test_real_lab_responses_parse(rel: str, expected: dict[tuple[str, str], float]) -> None:
    data = json.loads((FIXTURES / rel).read_text())
    query = data.pop("_meta")["query"]
    samples, dropped = parse_vector(data, source="thanos:lab", query=query)
    assert dropped == 0
    assert {(s.labels["namespace"], s.labels.get("pod", "")): s.value for s in samples} == expected


async def test_lab_series_without_the_pod_label_are_skipped(policy: Policy, tmp_path: Path) -> None:
    conn = make_conn(tmp_path, dcgm_namespace_label="namespace", dcgm_pod_label="pod")
    q_prof = (
        'max by (namespace, pod) (max_over_time(DCGM_FI_PROF_GR_ENGINE_ACTIVE{pod!=""}[21600s]))'
    )
    fake = FakeThanos({q_prof: fixture("lab/group-up.json")})
    with capture_logs() as events:
        async with make_source(fake, conn, policy) as source:
            snapshot = await source.fetch(NOW)
    assert snapshot.activity_by_pod == {
        ("openshift-apiserver", "apiserver-7dfc7df8d9-mt9xc"): 1.0,
        ("openshift-oauth-apiserver", "apiserver-6744d9ffd7-gqt7g"): 1.0,
        ("openshift-gitops", "openshift-gitops-application-controller-0"): 1.0,
    }
    [event] = [e for e in events if e["event"] == "thanos_fetch"]
    assert event["skipped_series"] == 1


def test_lab_promql_info_annotation_is_not_a_failure() -> None:
    data = json.loads((FIXTURES / "lab/promql-info-warning.json").read_text())
    query = data.pop("_meta")["query"]
    assert data["warnings"][0].startswith("PromQL info:")
    samples, _ = parse_vector(data, source="thanos:lab", query=query)
    assert samples


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (status(403), SourceErrorKind.PERMISSION_DENIED),
        (status(401), SourceErrorKind.PERMISSION_DENIED),
        (status(302, location="https://oauth.test/"), SourceErrorKind.INVALID),
        (fixture("lab/bad-data-400.json"), SourceErrorKind.INVALID),
        (fixture("synthetic/error-status.json"), SourceErrorKind.INVALID),
        (fixture("synthetic/partial-warnings.json"), SourceErrorKind.TEMPORARILY_UNAVAILABLE),
        (fixture("lab/matrix.json"), SourceErrorKind.INVALID),
        (fixture("lab/scalar.json"), SourceErrorKind.INVALID),
        (lambda: httpx.Response(200, text="<html>login</html>"), SourceErrorKind.INVALID),
        (
            lambda: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": [{}]}}
            ),
            SourceErrorKind.INVALID,
        ),
        (
            lambda: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "x"]}]},
                },
            ),
            SourceErrorKind.INVALID,
        ),
    ],
)
async def test_any_failed_query_fails_the_whole_fetch(
    policy: Policy, tmp_path: Path, reply: Reply, kind: SourceErrorKind
) -> None:
    fake = FakeThanos({Q_GPUS: fixture("synthetic/dcgm-gpus.json"), Q_POWER_LIMIT: reply})
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        with pytest.raises(SourceError) as excinfo:
            await source.fetch(NOW)
    assert excinfo.value.kind == kind
    assert excinfo.value.source == "thanos:ocp4-prod-gpu-site1"
    assert fake.queries() == DEFAULT_QUERIES


async def test_permission_denied_is_not_retried_and_stops_the_fetch(
    policy: Policy, tmp_path: Path
) -> None:
    fake = FakeThanos({Q_GPUS: status(403)})
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        with pytest.raises(SourceError) as excinfo:
            await source.fetch(NOW)
        assert excinfo.value.kind == SourceErrorKind.PERMISSION_DENIED
        assert fake.queries() == [Q_GPUS]
        fake.replies.clear()
        assert (await source.fetch(NOW)).cluster == "ocp4-prod-gpu-site1"


async def test_unavailable_is_retried_with_retry_after(policy: Policy, tmp_path: Path) -> None:
    fake = FakeThanos(
        {Q_GPUS: [status(503, **{"Retry-After": "0"}), fixture("synthetic/dcgm-gpus.json")]}
    )
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        snapshot = await source.fetch(NOW)
    assert fake.queries() == [Q_GPUS, *DEFAULT_QUERIES]
    assert len(snapshot.gpu_uuids_by_node) == 2


async def test_whole_fetch_deadline(policy: Policy, tmp_path: Path) -> None:
    fake = FakeThanos()
    fake.delay = 0.04
    async with make_source(fake, make_conn(tmp_path), policy, timeout=0.1) as source:
        started = time.monotonic()
        with pytest.raises(SourceError) as excinfo:
            await source.fetch(NOW)
    assert time.monotonic() - started < 0.5
    assert excinfo.value.kind == SourceErrorKind.TEMPORARILY_UNAVAILABLE
    assert "deadline" in str(excinfo.value)
    assert 1 < len(fake.requests) < len(DEFAULT_QUERIES)


async def test_queries_never_overlap_even_across_fetches(policy: Policy, tmp_path: Path) -> None:
    fake = FakeThanos()
    fake.delay = 0.001
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        await asyncio.gather(source.fetch(NOW), source.fetch(NOW))
    assert len(fake.requests) == 2 * len(DEFAULT_QUERIES)
    assert fake.max_in_flight == 1


async def test_token_reread_each_fetch_and_never_logged(
    policy: Policy, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    conn = make_conn(tmp_path)
    fake = FakeThanos({Q_XID: fixture("synthetic/dcgm-xid.json")})
    with capture_logs() as events:
        async with make_source(fake, conn, policy) as source:
            await source.fetch(NOW)
            conn.token_file.write_text("synthetic-rotated-token\n")
            fake.replies[Q_UTIL] = [status(401)]
            with pytest.raises(SourceError) as excinfo:
                await source.fetch(NOW)
    headers = [r.headers["authorization"] for r in fake.requests]
    assert (
        headers
        == [f"Bearer {TOKEN}"] * len(DEFAULT_QUERIES) + ["Bearer synthetic-rotated-token"] * 4
    )
    logged = repr(events) + caplog.text + str(excinfo.value)
    assert TOKEN not in logged
    assert "synthetic-rotated-token" not in logged


@pytest.mark.parametrize("content", ["", "  \n", None])
async def test_unreadable_token_fails_before_any_request(
    policy: Policy, tmp_path: Path, content: str | None
) -> None:
    conn = make_conn(tmp_path)
    if content is None:
        conn.token_file.unlink()
    else:
        conn.token_file.write_text(content)
    fake = FakeThanos()
    async with make_source(fake, conn, policy) as source:
        with pytest.raises(SourceError) as excinfo:
            await source.fetch(NOW)
    assert excinfo.value.kind == SourceErrorKind.PERMISSION_DENIED
    assert fake.requests == []


async def test_naive_now_is_refused(policy: Policy, tmp_path: Path) -> None:
    async with make_source(FakeThanos(), make_conn(tmp_path), policy) as source:
        with pytest.raises(ValueError, match="timezone-aware"):
            await source.fetch(NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("overrides", "kwargs"),
    [
        ({"dcgm_node_label": "Host name"}, {}),
        ({"dcgm_pod_label": 'pod="x"'}, {}),
        ({}, {"activity_window": timedelta(milliseconds=500)}),
        ({}, {"activity_window": timedelta(0)}),
        ({}, {"timeout_seconds": 0}),
        ({"thanos_insecure_skip_tls_verify": True, "thanos_ca_bundle": "/etc/pki/ttv/ca.pem"}, {}),
    ],
)
def test_invalid_configuration_is_refused(
    tmp_path: Path, overrides: dict[str, Any], kwargs: dict[str, Any]
) -> None:
    args: dict[str, Any] = {
        "activity_window": timedelta(hours=6),
        "timeout_seconds": 30.0,
        **kwargs,
    }
    with pytest.raises(ValueError):
        ThanosSource(make_conn(tmp_path, **overrides), **args)


def test_skipping_tls_verification_is_opt_in_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verify: list[object] = []
    real_client = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        verify.append(kwargs["verify"])
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    args: dict[str, Any] = {
        "activity_window": timedelta(hours=6),
        "timeout_seconds": 30.0,
    }
    with capture_logs() as events:
        ThanosSource(make_conn(tmp_path), **args)
        ThanosSource(make_conn(tmp_path, thanos_insecure_skip_tls_verify=True), **args)
    assert verify == [True, False]
    warnings = [e for e in events if e["log_level"] == "warning"]
    assert [e["event"] for e in warnings] == ["thanos.tls_verification_disabled"]
    assert warnings[0]["source"] == "thanos:ocp4-prod-gpu-site1"
    assert TOKEN not in repr(events)


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (timedelta(hours=24), "86400s"),
        (timedelta(minutes=90), "5400s"),
        (timedelta(seconds=1.5), "1s"),
    ],
)
def test_format_window_whole_seconds(window: timedelta, expected: str) -> None:
    assert format_window(window) == expected


def test_parse_vector_drops_non_finite_and_keeps_zero() -> None:
    body = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"pod": "a"}, "value": [0, "NaN"]},
                {"metric": {"pod": "b"}, "value": [0, "+Inf"]},
                {"metric": {"pod": "c"}, "value": [0, "-Inf"]},
                {"metric": {"pod": "d"}, "value": [0, "0"]},
            ],
        },
    }
    samples, dropped = parse_vector(body, source="thanos:test", query="q")
    assert dropped == 3
    assert [(s.labels, s.value) for s in samples] == [({"pod": "d"}, 0.0)]


GUARD_BASE = httpx.URL(BASE_URL)
ALLOWED = f"{BASE_URL}{QUERY_PATH}?query=up&time=1&partial_response=false"


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", ALLOWED),
        ("PUT", ALLOWED),
        ("DELETE", ALLOWED),
        ("HEAD", ALLOWED),
        ("GET", f"https://elsewhere.test{QUERY_PATH}?query=up"),
        (
            "GET",
            f"http://thanos-querier-openshift-monitoring.apps.ocp4-prod-gpu-site1.test{QUERY_PATH}",
        ),
        ("GET", f"{BASE_URL}:8443{QUERY_PATH}?query=up"),
        ("GET", f"{BASE_URL}/api/v1/series?match[]=up"),
        ("GET", f"{BASE_URL}/api/v1/query_range?query=up"),
        ("GET", f"{BASE_URL}/api/v1/labels"),
        ("GET", f"{BASE_URL}/api/v1/admin/tsdb/snapshot"),
        ("GET", f"{BASE_URL}/federate?match[]=up"),
        ("GET", f"{BASE_URL}{QUERY_PATH}/"),
        ("GET", f"{BASE_URL}{QUERY_PATH}/../series?match[]=up"),
        ("GET", f"{BASE_URL}{QUERY_PATH}?query=up&dedup=false"),
        ("GET", f"{BASE_URL}{QUERY_PATH}?query=up&timeout=1s"),
        ("GET", f"{BASE_URL}{QUERY_PATH}?query=up&query=vector(1)"),
    ],
)
async def test_guard_refuses_everything_but_get_instant_query(
    policy: Policy, tmp_path: Path, method: str, url: str
) -> None:
    with pytest.raises(UnsafeRequestError):
        check_request(method, httpx.URL(url), GUARD_BASE)
    fake = FakeThanos()
    async with make_source(fake, make_conn(tmp_path), policy) as source:
        with pytest.raises(UnsafeRequestError):
            await source._client.request(method, url)
    assert fake.requests == []


@pytest.mark.parametrize(
    ("base", "url"),
    [
        (BASE_URL, ALLOWED),
        (BASE_URL, f"{BASE_URL}{QUERY_PATH}?query=up"),
        (BASE_URL + "/", f"{BASE_URL}{QUERY_PATH}"),
        ("https://proxy.test/thanos", f"https://proxy.test/thanos{QUERY_PATH}?query=up"),
    ],
)
def test_guard_allows_the_instant_query(base: str, url: str) -> None:
    check_request("GET", httpx.URL(url), httpx.URL(base.rstrip("/")))
