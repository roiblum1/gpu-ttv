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

from ttv.config import ClusterConnection, CounterOfRecord, Policy
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
Q_EPP = "sum by (namespace, pod) (increase(llm_d_epp_request_total[86400s]))"
Q_OBJECTIVE = "sum by (namespace, pod) (increase(inference_objective_request_total[86400s]))"
Q_MODEL = "sum by (namespace, pod) (increase(inference_model_request_total[86400s]))"
Q_VLLM = "sum by (namespace, pod) (increase(vllm:request_success_total[86400s]))"
DEFAULT_QUERIES = [Q_GPUS, Q_XID, Q_PROF, Q_UTIL, Q_EPP, Q_OBJECTIVE, Q_MODEL, Q_VLLM]

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
    counters: list[CounterOfRecord] | None = None,
    timeout: float = 30.0,
) -> ThanosSource:
    return ThanosSource(
        conn,
        counters=policy.counters() if counters is None else counters,
        demand_window=policy.serving.demand_window,
        activity_window=policy.allocated_idle.window,
        timeout_seconds=timeout,
        transport=httpx.MockTransport(fake.handler),
    )


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


async def test_lab_cluster_shape_is_all_empty_and_every_counter_absent(
    policy: Policy, tmp_path: Path
) -> None:
    fake = FakeThanos()
    with capture_logs() as events:
        async with make_source(fake, make_conn(tmp_path), policy) as source:
            snapshot = await source.fetch(NOW)
    assert snapshot.counters == {}
    assert snapshot.activity_metric is None
    [event] = [e for e in events if e["event"] == "thanos_fetch"]
    assert event["log_level"] == "info"
    assert event["cluster"] == "ocp4-prod-gpu-site1"
    assert event["nodes_with_dcgm"] == event["pods_with_activity"] == 0
    assert event["counters_with_series"] == []
    assert event["counters_absent"] == [c.selector for c in policy.counters()]
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


async def test_counters_keep_zero_and_leave_missing_selectors_absent(
    policy: Policy, tmp_path: Path
) -> None:
    fake = FakeThanos(
        {
            Q_EPP: fixture("synthetic/counter-epp.json"),
            Q_VLLM: fixture("synthetic/counter-vllm-zero.json"),
            Q_MODEL: fixture("synthetic/counter-all-nan.json"),
        }
    )
    with capture_logs() as events:
        async with make_source(fake, make_conn(tmp_path), policy) as source:
            snapshot = await source.fetch(NOW)
    assert snapshot.counters == {
        "llm_d_epp_request_total": {
            ("llm", "qwen-kserve-router-scheduler-5d8f9c7b4-x2k9p"): 1234.0,
            ("llm", "qwen-kserve-router-scheduler-5d8f9c7b4-q7w3z"): 5.0,
        },
        "vllm:request_success_total": {
            ("llm", "qwen-kserve-7c9d6f5b8-h4j2k"): 0.0,
            ("llm", "qwen-kserve-7c9d6f5b8-m8n3p"): 0.0,
        },
    }
    [event] = [e for e in events if e["event"] == "thanos_fetch"]
    assert event["counters_with_series"] == [
        "llm_d_epp_request_total",
        "vllm:request_success_total",
    ]
    assert event["counters_absent"] == [
        "inference_objective_request_total",
        "inference_model_request_total",
    ]
    assert event["rejected_samples"] == 3


async def test_labels_windows_and_matchers_come_from_config(tmp_path: Path) -> None:
    conn = make_conn(
        tmp_path,
        dcgm_node_label="node",
        dcgm_uuid_label="gpu_uuid",
        dcgm_pod_label="pod",
        dcgm_namespace_label="namespace",
        serving_pod_label="exported_pod",
        serving_namespace_label="exported_namespace",
    )
    epp = CounterOfRecord(
        metric="llm_d_epp_request_total", pod_regex="^a$", matchers='fairness_id!="synthetic"'
    )
    counters = [epp, epp.model_copy(update={"pod_regex": "^b$"})]
    q_epp = (
        "sum by (exported_namespace, exported_pod) "
        '(increase(llm_d_epp_request_total{fairness_id!="synthetic"}[3600s]))'
    )
    reply = httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"exported_namespace": "llm", "exported_pod": "epp-0"},
                        "value": [0, "7"],
                    }
                ],
            },
        },
    )
    fake = FakeThanos({q_epp: lambda: reply})
    source = ThanosSource(
        conn,
        counters=counters,
        demand_window=timedelta(hours=1),
        activity_window=timedelta(minutes=90),
        timeout_seconds=30,
        transport=httpx.MockTransport(fake.handler),
    )
    async with source:
        snapshot = await source.fetch(NOW)
    assert fake.queries() == [
        "group by (node, gpu_uuid) (DCGM_FI_DEV_POWER_USAGE)",
        "max by (node, gpu_uuid) (DCGM_FI_DEV_XID_ERRORS) != 0",
        'max by (namespace, pod) (max_over_time(DCGM_FI_PROF_GR_ENGINE_ACTIVE{pod!=""}[5400s]))',
        'max by (namespace, pod) (max_over_time(DCGM_FI_DEV_GPU_UTIL{pod!=""}[5400s])) / 100',
        q_epp,
    ]
    assert snapshot.counters == {epp.selector: {("llm", "epp-0"): 7.0}}


async def test_real_lab_responses_parse_through_the_counter_path(
    policy: Policy, tmp_path: Path
) -> None:
    counters = [
        CounterOfRecord(metric="prometheus_http_requests_total", pod_regex="x"),
        CounterOfRecord(metric="up", pod_regex="x"),
        CounterOfRecord(metric="kubelet_running_pods", pod_regex="x"),
    ]
    fake = FakeThanos(
        {
            "sum by (namespace, pod) (increase(prometheus_http_requests_total[86400s]))": fixture(
                "lab/increase-requests.json"
            ),
            "sum by (namespace, pod) (increase(up[86400s]))": fixture("lab/group-up.json"),
            "sum by (namespace, pod) (increase(kubelet_running_pods[86400s]))": fixture(
                "lab/promql-info-warning.json"
            ),
        }
    )
    async with make_source(fake, make_conn(tmp_path), policy, counters=counters) as source:
        snapshot = await source.fetch(NOW)
    assert snapshot.counters == {
        "prometheus_http_requests_total": {
            ("openshift-monitoring", "prometheus-k8s-0"): 2318.64406779661,
            ("openshift-user-workload-monitoring", "prometheus-user-workload-0"): 2302.372881355932,
        },
        "up": {
            ("openshift-apiserver", "apiserver-7dfc7df8d9-mt9xc"): 1.0,
            ("openshift-oauth-apiserver", "apiserver-6744d9ffd7-gqt7g"): 1.0,
            ("openshift-gitops", "openshift-gitops-application-controller-0"): 1.0,
        },
    }


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
    fake = FakeThanos({Q_GPUS: fixture("synthetic/dcgm-gpus.json"), Q_VLLM: reply})
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
    fake.delay = 0.03
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
            fake.replies[Q_VLLM] = [status(401)]
            with pytest.raises(SourceError) as excinfo:
                await source.fetch(NOW)
    headers = [r.headers["authorization"] for r in fake.requests]
    assert headers == [f"Bearer {TOKEN}"] * 8 + ["Bearer synthetic-rotated-token"] * 8
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
        ({"serving_pod_label": 'pod="x"'}, {}),
        ({}, {"demand_window": timedelta(milliseconds=500)}),
        ({}, {"activity_window": timedelta(0)}),
        ({}, {"timeout_seconds": 0}),
    ],
)
def test_invalid_configuration_is_refused(
    tmp_path: Path, overrides: dict[str, str], kwargs: dict[str, Any]
) -> None:
    args: dict[str, Any] = {
        "counters": [],
        "demand_window": timedelta(hours=24),
        "activity_window": timedelta(hours=6),
        "timeout_seconds": 30.0,
        **kwargs,
    }
    with pytest.raises(ValueError):
        ThanosSource(make_conn(tmp_path, **overrides), **args)


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
