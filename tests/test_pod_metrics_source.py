"""
Tests for the vLLM pod `/metrics` reader through the kube API pod proxy (fixture-tested).

Every exposition below is synthetic, shaped like vLLM 0.32's `/metrics` page; no network.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from ttv.config import ClusterConnection
from ttv.sources.http import UnsafeRequestError
from ttv.sources.pod_metrics import (
    MAX_BODY_BYTES,
    MetricsTarget,
    PodMetricsRead,
    PodMetricsSource,
    check_request,
    metrics_path,
    sum_metric,
)

API = "https://api.lab.test:6443"
METRIC = "vllm:request_success_total"
TOKEN = "sha256~synthetic-token-never-log-me"
ROTATED = "sha256~synthetic-token-rotated"
NS = "vllm-lab"
POD = "qwen-tiny-kserve-84fd6f5c76-b4wzq"
QWEN = MetricsTarget(NS, POD, 8000)

# Synthetic, in vLLM 0.32's layout: five finished_reason series for the served model plus one
# LoRA adapter, the `_created` gauges prometheus-client adds, and neighbours that must not count.
VLLM_METRICS = """\
# HELP python_gc_objects_collected_total Objects collected during gc
# TYPE python_gc_objects_collected_total counter
python_gc_objects_collected_total{generation="0"} 21744.0
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="qwen-tiny"} 1.0
# HELP vllm:request_success_total Count of successfully processed requests.
# TYPE vllm:request_success_total counter
vllm:request_success_total{engine="0",finished_reason="stop",model_name="qwen-tiny"} 120.0
vllm:request_success_total{engine="0",finished_reason="length",model_name="qwen-tiny"} 7.0
vllm:request_success_total{engine="0",finished_reason="abort",model_name="qwen-tiny"} 3.0
vllm:request_success_total{engine="0",finished_reason="error",model_name="qwen-tiny"} 1.0
vllm:request_success_total{engine="0",finished_reason="repetition",model_name="qwen-tiny"} 0.0
vllm:request_success_total{engine="0",finished_reason="stop",model_name="qwen-tiny-lora"} 5.0
# HELP vllm:request_success_created Count of successfully processed requests.
# TYPE vllm:request_success_created gauge
vllm:request_success_created{engine="0",finished_reason="stop",model_name="qwen-tiny"} 1.7933976e+09
# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0",model_name="qwen-tiny"} 48211.0
# HELP vllm:e2e_request_latency_seconds Histogram of e2e request latency in seconds.
# TYPE vllm:e2e_request_latency_seconds histogram
vllm:e2e_request_latency_seconds_bucket{engine="0",le="1.0",model_name="qwen-tiny"} 100.0
vllm:e2e_request_latency_seconds_bucket{engine="0",le="+Inf",model_name="qwen-tiny"} 131.0
vllm:e2e_request_latency_seconds_count{engine="0",model_name="qwen-tiny"} 131.0
vllm:e2e_request_latency_seconds_sum{engine="0",model_name="qwen-tiny"} 88.5
"""
VLLM_TOTAL = 120.0 + 7.0 + 3.0 + 1.0 + 0.0 + 5.0

Reply = Callable[[], httpx.Response]


def text(body: str) -> Reply:
    return lambda: httpx.Response(
        200, text=body, headers={"Content-Type": "text/plain; version=0.0.4"}
    )


def status(code: int, message: str = "synthetic") -> Reply:
    """A kube `Status` error, as the API server answers for the proxy."""
    body = {"kind": "Status", "apiVersion": "v1", "status": "Failure", "message": message}
    return lambda: httpx.Response(code, json={**body, "code": code})


class FakeApi:
    """MockTransport handler: replies per request path; unknown paths are 404."""

    def __init__(self) -> None:
        self.replies: dict[str, Reply] = {}
        self.requests: list[httpx.Request] = []
        self.delay = 0.0
        self.in_flight = 0
        self.max_in_flight = 0

    def serve(self, target: MetricsTarget, reply: Reply, scheme: str = "http") -> None:
        self.replies[metrics_path(target, scheme)] = reply

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        reply = self.replies.get(request.url.path)
        return reply() if reply else status(404, "pods not found")()


@pytest.fixture
def fake() -> FakeApi:
    return FakeApi()


@pytest.fixture
def conn(tmp_path: Path) -> ClusterConnection:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    return ClusterConnection(
        name="ocp4-prod-gpu-site1",
        api_url=API,
        thanos_url="https://thanos.lab.test",
        token_file=token_file,
    )


def source(
    fake: FakeApi, conn: ClusterConnection, *, timeout: float = 5.0, concurrency: int = 8
) -> PodMetricsSource:
    return PodMetricsSource(
        conn,
        metric=METRIC,
        timeout_seconds=timeout,
        concurrency=concurrency,
        transport=httpx.MockTransport(fake.handler),
    )


async def read(fake: FakeApi, conn: ClusterConnection, *targets: MetricsTarget) -> PodMetricsRead:
    async with source(fake, conn) as src:
        return await src.fetch(list(targets))


# --- values ------------------------------------------------------------------------------------


async def test_sums_the_counter_over_every_label_set(
    fake: FakeApi, conn: ClusterConnection
) -> None:
    fake.serve(QWEN, text(VLLM_METRICS))

    async with source(fake, conn) as src:
        assert src.source == "vllm:ocp4-prod-gpu-site1"
        result = await src.fetch([QWEN])

    assert result == PodMetricsRead(
        values={(NS, POD): VLLM_TOTAL}, failed={}, permission_denied=False
    )
    [request] = fake.requests
    assert request.method == "GET"
    assert str(request.url) == f"{API}/api/v1/namespaces/{NS}/pods/http:{POD}:8000/proxy/metrics"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (VLLM_METRICS.replace(METRIC, "vllm:request_other_total"), "metric_absent"),
        ("", "metric_absent"),
        (f'{METRIC}{{finished_reason="stop"}} NaN\n', "invalid_value"),
        (f'{METRIC}{{finished_reason="stop"}} +Inf\n', "invalid_value"),
        (
            f'{METRIC}{{finished_reason="stop"}} 4\n{METRIC}{{finished_reason="abort"}} -1\n',
            "invalid_value",
        ),
        ("<html><body>login</body></html>\n", "parse_error"),
    ],
)
async def test_absent_or_bad_samples_fail_the_pod(
    fake: FakeApi, conn: ClusterConnection, body: str, reason: str
) -> None:
    fake.serve(QWEN, text(body))

    result = await read(fake, conn, QWEN)

    assert result.values == {}
    assert result.failed == {(NS, POD): reason}
    assert len(fake.requests) == 1


def test_sum_metric_matches_the_sample_name_exactly() -> None:
    assert sum_metric(VLLM_METRICS, METRIC) == (VLLM_TOTAL, None)
    assert sum_metric(VLLM_METRICS, "vllm:request_success") == (None, "metric_absent")
    assert sum_metric(VLLM_METRICS, "vllm:prompt_tokens_total") == (48211.0, None)


async def test_one_pods_failure_never_fails_the_fetch(
    fake: FakeApi, conn: ClusterConnection
) -> None:
    other = MetricsTarget(NS, "qwen-tiny-kserve-84fd6f5c76-755hl", 8000)
    fake.serve(QWEN, text(VLLM_METRICS))

    result = await read(fake, conn, QWEN, other, QWEN)

    assert result.values == {(NS, POD): VLLM_TOTAL}
    assert result.failed == {(NS, other.pod): "http_404"}
    assert len(fake.requests) == 2  # the repeated target is read once


async def test_empty_targets_send_no_request(fake: FakeApi, conn: ClusterConnection) -> None:
    result = await read(fake, conn)

    assert result == PodMetricsRead(values={}, failed={}, permission_denied=False)
    assert fake.requests == []


# --- schemes and HTTP failures -----------------------------------------------------------------


async def test_https_fallback_is_remembered_per_pod(fake: FakeApi, conn: ClusterConnection) -> None:
    tls = MetricsTarget("serving", "granite-predictor-00001-deployment-7d9f8-x2k9p", 8443)
    fake.serve(tls, status(400, "Client sent an HTTP request to an HTTPS server."))
    fake.serve(tls, text(VLLM_METRICS), scheme="https")
    fake.serve(QWEN, text(VLLM_METRICS))
    http_path, https_path = metrics_path(tls, "http"), metrics_path(tls, "https")

    async with source(fake, conn) as src:
        first = await src.fetch([tls, QWEN])
        second = await src.fetch([tls, QWEN])
        await src.fetch([QWEN])  # tls is not a target: its scheme is forgotten
        fake.requests.clear()
        await src.fetch([tls])

    assert first == second
    assert first.values == {(tls.namespace, tls.pod): VLLM_TOTAL, (NS, POD): VLLM_TOTAL}
    assert fake.paths() == [http_path, https_path]


async def test_both_schemes_failing_leaves_the_pod_failed(
    fake: FakeApi, conn: ClusterConnection
) -> None:
    fake.serve(QWEN, status(503, "error trying to reach service: connection refused"))
    fake.serve(QWEN, status(502, "error trying to reach service: tls"), scheme="https")

    async with source(fake, conn) as src:
        first = await src.fetch([QWEN])
        second = await src.fetch([QWEN])

    assert first.failed == second.failed == {(NS, POD): "http_502"}
    assert fake.paths() == [metrics_path(QWEN, "http"), metrics_path(QWEN, "https")] * 2


@pytest.mark.parametrize("code", [401, 403])
async def test_denied_sets_permission_denied_and_warns_once(
    fake: FakeApi, conn: ClusterConnection, code: int
) -> None:
    pods = [MetricsTarget(NS, f"qwen-tiny-kserve-84fd6f5c76-{i}abcd", 8000) for i in range(3)]
    for pod in pods:
        fake.serve(pod, status(code, 'pods "x" is forbidden: cannot get resource "pods/proxy"'))

    with capture_logs() as events:
        result = await read(fake, conn, *pods)

    assert result.permission_denied
    assert result.values == {}
    assert set(result.failed.values()) == {f"http_{code}"}
    assert len(fake.requests) == 3  # no https retry after 401/403
    [warning] = [e for e in events if e["log_level"] == "warning"]
    assert warning["event"] == "pod_metrics.permission_denied"
    assert warning["pods"] == 3
    assert "pods/proxy" in warning["fix"]
    assert "deploy/gpu-cluster-rbac/" in warning["fix"]


async def test_not_found_fails_without_retry(fake: FakeApi, conn: ClusterConnection) -> None:
    result = await read(fake, conn, QWEN)

    assert result == PodMetricsRead(
        values={}, failed={(NS, POD): "http_404"}, permission_denied=False
    )
    assert fake.paths() == [metrics_path(QWEN, "http")]


async def big_stream() -> AsyncIterator[bytes]:
    chunk = b"# padding line\n" * 4096
    for _ in range(MAX_BODY_BYTES // len(chunk) + 2):
        yield chunk


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (lambda: httpx.Response(200, content=b"#" * (MAX_BODY_BYTES + 1)), "too_large"),
        (lambda: httpx.Response(200, content=big_stream()), "too_large"),
        (
            lambda: httpx.Response(
                200,
                content=gzip.compress(VLLM_METRICS.encode()),
                headers={"Content-Encoding": "gzip"},
            ),
            "encoded",
        ),
        (lambda: httpx.Response(200, content=b"\xff\xfe\x00bad"), "parse_error"),
    ],
)
async def test_oversized_or_encoded_bodies_fail_the_pod(
    fake: FakeApi, conn: ClusterConnection, reply: Reply, reason: str
) -> None:
    fake.serve(QWEN, reply)

    result = await read(fake, conn, QWEN)

    assert result.failed == {(NS, POD): reason}


async def test_transport_errors_fail_the_pod(conn: ClusterConnection) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic: connection refused", request=request)

    src = PodMetricsSource(
        conn, metric=METRIC, timeout_seconds=5, transport=httpx.MockTransport(refuse)
    )
    async with src:
        result = await src.fetch([QWEN])

    assert result.failed == {(NS, POD): "transport"}


# --- concurrency and deadline ------------------------------------------------------------------


async def test_concurrency_never_exceeds_the_bound(fake: FakeApi, conn: ClusterConnection) -> None:
    pods = [MetricsTarget(NS, f"vllm-{i}", 8000) for i in range(20)]
    for pod in pods:
        fake.serve(pod, text(VLLM_METRICS))
    fake.delay = 0.01

    async with source(fake, conn, concurrency=3) as src:
        result = await src.fetch(pods)

    assert len(result.values) == 20
    assert fake.max_in_flight == 3


async def test_pods_unfinished_at_the_deadline_fail_with_deadline(
    fake: FakeApi, conn: ClusterConnection
) -> None:
    pods = [MetricsTarget(NS, f"vllm-{i}", 8000) for i in range(6)]
    for pod in pods:
        fake.serve(pod, text(VLLM_METRICS))
    fake.delay = 0.05

    async with source(fake, conn, timeout=0.12, concurrency=1) as src:
        started = time.monotonic()
        result = await src.fetch(pods)

    assert time.monotonic() - started < 0.4
    assert (NS, "vllm-0") in result.values
    assert (NS, "vllm-5") in result.failed
    assert set(result.failed.values()) == {"deadline"}
    assert len(result.values) + len(result.failed) == 6
    assert fake.in_flight == 0


async def test_cancelling_the_fetch_cancels_every_read(
    fake: FakeApi, conn: ClusterConnection
) -> None:
    fake.delay = 5.0

    async with source(fake, conn) as src:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(src.fetch([MetricsTarget(NS, "vllm-0", 8000), QWEN]), 0.05)

    assert len(fake.requests) == 2
    assert fake.in_flight == 0


# --- token -------------------------------------------------------------------------------------


async def test_token_reread_each_fetch_and_never_logged(
    fake: FakeApi, conn: ClusterConnection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.serve(QWEN, text(VLLM_METRICS))

    with capture_logs() as events:
        async with source(fake, conn) as src:
            await src.fetch([QWEN])
            conn.token_file.write_text(ROTATED + "\n")
            fake.serve(QWEN, status(403))
            denied = await src.fetch([QWEN])

    assert [r.headers["authorization"] for r in fake.requests] == [
        f"Bearer {TOKEN}",
        f"Bearer {ROTATED}",
    ]
    assert denied.permission_denied
    logged = repr(events) + caplog.text + repr(denied)
    assert TOKEN not in logged
    assert ROTATED not in logged
    assert "Bearer" not in logged


@pytest.mark.parametrize("content", ["", "two words\n", None])
async def test_unreadable_token_fails_every_pod_without_a_request(
    fake: FakeApi, conn: ClusterConnection, content: str | None
) -> None:
    if content is None:
        conn.token_file.unlink()
    else:
        conn.token_file.write_text(content)

    result = await read(fake, conn, QWEN)

    assert result.failed == {(NS, POD): "token_unreadable"}
    assert fake.requests == []


# --- request guard -----------------------------------------------------------------------------

GUARD_BASE = httpx.URL(API)
ALLOWED = f"{API}/api/v1/namespaces/{NS}/pods/http:{POD}:8000/proxy/metrics"


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", ALLOWED),
        ("PUT", ALLOWED),
        ("DELETE", ALLOWED),
        ("PATCH", ALLOWED),
        ("HEAD", ALLOWED),
        ("GET", ALLOWED + "?format=json"),
        ("GET", ALLOWED + "?"),
        ("GET", ALLOWED + "/"),
        ("GET", ALLOWED.replace("api.lab.test", "elsewhere.test")),
        ("GET", ALLOWED.replace(":6443", ":443")),
        ("GET", ALLOWED.replace("https://", "http://")),
        ("GET", ALLOWED.replace("/proxy/metrics", "/proxy/debug/pprof")),
        ("GET", ALLOWED.replace("/proxy/metrics", "/proxy/v1/completions")),
        ("GET", ALLOWED.replace("/proxy/metrics", "/log")),
        ("GET", ALLOWED.replace("/proxy/metrics", "/exec")),
        ("GET", f"{API}/api/v1/namespaces/{NS}/services/http:{POD}:8000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/{POD}:8000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/http:{POD}/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/ftp:{POD}:8000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/http:{POD}:0/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/http:{POD}:70000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/http:Bad_Pod:8000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/http:-pod:8000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/Bad.NS/pods/http:{POD}:8000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/pods/http:{POD}%2F..:8000/proxy/metrics"),
        ("GET", f"{API}/api/v1/namespaces/{NS}/secrets/x"),
        ("GET", f"{API}/api/v1/pods"),
    ],
)
async def test_guard_refuses_everything_but_a_pod_metrics_get(
    fake: FakeApi, conn: ClusterConnection, method: str, url: str
) -> None:
    with pytest.raises(UnsafeRequestError):
        check_request(method, httpx.URL(url), GUARD_BASE)
    async with source(fake, conn) as src:
        with pytest.raises(UnsafeRequestError):
            await src._client.request(method, url)
    assert fake.requests == []


@pytest.mark.parametrize(
    ("base", "url"),
    [
        (API, ALLOWED),
        (API, ALLOWED.replace("/http:", "/https:")),
        (API, f"{API}/api/v1/namespaces/a/pods/http:a.b-c.d:1/proxy/metrics"),
        (
            "https://proxy.test/k8s",
            f"https://proxy.test/k8s/api/v1/namespaces/{NS}/pods/http:p:8000/proxy/metrics",
        ),
    ],
)
def test_guard_allows_a_pod_metrics_get(base: str, url: str) -> None:
    check_request("GET", httpx.URL(url), httpx.URL(base.rstrip("/")))


@pytest.mark.parametrize(
    "target",
    [
        MetricsTarget(NS, f"a/../../other/pods/http:{POD}", 8000),
        MetricsTarget(NS, "Bad_Pod", 8000),
        MetricsTarget("../kube-system", POD, 8000),
        MetricsTarget(NS, "vllm-0", 0),
        MetricsTarget(NS, "vllm-0", 65536),
        MetricsTarget(NS, "x" * 254, 8000),
    ],
)
async def test_unsafe_targets_fail_without_a_request(
    fake: FakeApi, conn: ClusterConnection, target: MetricsTarget
) -> None:
    fake.serve(QWEN, text(VLLM_METRICS))

    result = await read(fake, conn, target, QWEN)

    assert result.values == {(NS, POD): VLLM_TOTAL}
    assert result.failed == {(target.namespace, target.pod): "invalid_target"}
    assert fake.paths() == [metrics_path(QWEN, "http")]


async def test_a_pod_is_read_once_even_when_listed_with_two_ports(
    fake: FakeApi, conn: ClusterConnection
) -> None:
    fake.serve(QWEN, text(VLLM_METRICS))

    result = await read(fake, conn, QWEN, MetricsTarget(NS, POD, 9090))

    assert result.values == {(NS, POD): VLLM_TOTAL}
    assert fake.paths() == [metrics_path(QWEN, "http")]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric": "vllm:request success"},
        {"metric": 'x{a="b"}'},
        {"timeout_seconds": 0},
        {"concurrency": 0},
    ],
)
def test_invalid_configuration_is_refused(conn: ClusterConnection, kwargs: dict[str, Any]) -> None:
    args: dict[str, Any] = {"metric": METRIC, "timeout_seconds": 5.0, **kwargs}
    with pytest.raises(ValueError):
        PodMetricsSource(conn, **args)
