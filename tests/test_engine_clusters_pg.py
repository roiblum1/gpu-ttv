"""End-to-end scans with target-cluster sources against a real Postgres (§17, §20.6).

Needs `TTV_TEST_DSN`; skipped otherwise. Cluster snapshots are synthetic (fixture-tested).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import DSN, T0, server
from test_engine_pg import Clock, FakeSource, hours, rows
from ttv.config import ClusterConnection, Policy
from ttv.engine import ClusterSources, Engine
from ttv.metrics import EngineMetrics
from ttv.models import (
    ClusterNode,
    ClusterPod,
    KubeSnapshot,
    OwnerRef,
    ServerScanServer,
    ThanosSnapshot,
)
from ttv.queries import IDLE_GPU_HOURS_BY_OWNER
from ttv.sources.http import SourceError, SourceErrorKind
from ttv.sources.pod_metrics import MetricsTarget, PodMetricsRead
from ttv.store import PostgresStore

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

CLUSTER = "ocp4-prod-gpu-site1"
NODE = "ocp4-prod-site1-gpu-01"


class FakeKube:
    """Kube source answering from a script; the last answer repeats."""

    def __init__(self, *answers: KubeSnapshot | Exception) -> None:
        """Store the script."""
        self.answers = list(answers)

    async def fetch(self, now: datetime) -> KubeSnapshot:
        """Return (or raise) the next answer, stamped with `now`."""
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return dataclasses.replace(answer, fetched_at=now)

    async def aclose(self) -> None:
        """Nothing to release."""


class FakeThanos:
    """Thanos source answering from a script; the last answer repeats."""

    def __init__(self, *answers: ThanosSnapshot | Exception) -> None:
        """Store the script."""
        self.answers = list(answers)

    async def fetch(self, now: datetime) -> ThanosSnapshot:
        """Return (or raise) the next answer, stamped with `now`."""
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return dataclasses.replace(answer, fetched_at=now)

    async def aclose(self) -> None:
        """Nothing to release."""


class FakePodMetrics:
    """vLLM pod-metrics source answering from a script of counter values; the last repeats."""

    def __init__(self, *answers: dict[str, float], denied: bool = False) -> None:
        """Store the script (pod name → counter value)."""
        self.answers = list(answers)
        self.denied = denied
        self.targets: list[list[MetricsTarget]] = []

    async def fetch(self, targets: Sequence[MetricsTarget]) -> PodMetricsRead:
        """Answer for the targets the engine asked for."""
        self.targets.append(list(targets))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        values = {(t.namespace, t.pod): answer[t.pod] for t in targets if t.pod in answer}
        failed = {(t.namespace, t.pod): "http_403" for t in targets if t.pod not in answer}
        return PodMetricsRead(values=values, failed=failed, permission_denied=self.denied)

    async def aclose(self) -> None:
        """Nothing to release."""


def model_pod(name: str = "qwen-kserve-84fd6f5c76-abcde", role: str = "decode") -> ClusterPod:
    """A Ready vLLM pod of LLMInferenceService `qwen` holding 4 GPUs, up for two days."""
    return ClusterPod(
        "team-a",
        name,
        f"uid-{name}",
        NODE,
        "Running",
        True,
        T0 - timedelta(days=2),
        4,
        owners=(
            OwnerRef("apps", "ReplicaSet", "qwen-kserve-84fd6f5c76"),
            OwnerRef("apps", "Deployment", "qwen-kserve"),
            OwnerRef("serving.kserve.io", "LLMInferenceService", "qwen"),
        ),
        labels={"llm-d.ai/role": role},
        images=("registry.example/vllm/vllm-openai:v0.32.0",),
        metrics_port=8000,
    )


def kube(*pods: ClusterPod) -> KubeSnapshot:
    """One Ready node with 8 allocatable GPUs."""
    n = ClusterNode(NODE, "node-uid-1", True, T0 - timedelta(days=1), False, 8)
    return KubeSnapshot(CLUSTER, "cid", T0, (n,), pods)


def thanos() -> ThanosSnapshot:
    """DCGM reporting 8 GPUs on the node, and the trainer's GPUs busy."""
    return ThanosSnapshot(
        CLUSTER,
        T0,
        {NODE: frozenset(f"GPU-{i}" for i in range(8))},
        activity_by_pod={("team-a", "train-0"): 0.8},
    )


def trainer() -> ClusterPod:
    """A Run:ai training pod holding 2 GPUs."""
    return ClusterPod(
        "team-a",
        "train-0",
        "uid-t",
        NODE,
        "Running",
        True,
        T0 - timedelta(hours=1),
        2,
        owners=(OwnerRef("run.ai", "TrainingWorkload", "ft"),),
    )


def installed(**kw: object) -> ServerScanServer:
    """The 8-GPU server, INSTALLED in the target cluster as `NODE`."""
    return server(lifecycle_state="INSTALLED", cluster_name=CLUSTER, reported_name=NODE, **kw)


def engine_for(
    store: PostgresStore,
    policy: Policy,
    source: FakeSource,
    clock: Clock,
    sources: ClusterSources,
    *,
    dry_run: bool = False,
) -> Engine:
    """An engine observing one cluster, grace 2."""
    return Engine(
        policy=policy,
        source=source,
        store=store,
        metrics=EngineMetrics(),
        dry_run=dry_run,
        unknown_grace_scans=2,
        tombstone_after=timedelta(hours=24),
        clusters=[sources],
        clock=clock,
    )


def conn() -> ClusterConnection:
    """A connection record (never dialled by the fakes)."""
    return ClusterConnection(
        name=CLUSTER,
        api_url="https://api.example:6443",
        thanos_url="https://thanos.example",
        token_file=Path("/nonexistent"),
    )


async def open_rows(store: PostgresStore) -> set[tuple[str, str, int]]:
    """(category, reason, gpus) of open intervals."""
    result = await rows(
        store, "SELECT category, reason_code, gpus FROM capacity_intervals WHERE ended_at IS NULL"
    )
    return {(r["category"], r["reason_code"], r["gpus"]) for r in result}


async def test_partial_idle_is_12_gpu_hours_through_the_classifier(
    store: PostgresStore, policy: Policy
) -> None:
    # AGENT.md §17: an 8-GPU node, 2 GPUs productive, 6 idle for 2 h = 12 idle GPU-hours.
    await store.sync_objectives(policy)
    source, clock = FakeSource(), Clock()
    source.answers = [[installed()], [installed()]]
    sources = ClusterSources(conn(), FakeKube(kube(trainer())), FakeThanos(thanos()))
    engine = engine_for(store, policy, source, clock, sources)
    await engine.run_once()
    clock.now = T0 + timedelta(hours=2)
    await engine.run_once()
    idle = await hours(store, IDLE_GPU_HOURS_BY_OWNER, T0, clock.now, "idle_gpu_hours")
    assert idle == {"Samurai": 12.0}
    assert await open_rows(store) == {
        ("PRODUCTIVE", "RUNAI_WORKLOAD", 2),
        ("IDLE", "NO_WORKLOAD", 6),
    }
    stored = await rows(
        store, "SELECT DISTINCT cluster, node_uid FROM capacity_intervals WHERE ended_at IS NULL"
    )
    assert stored == [{"cluster": CLUSTER, "node_uid": "node-uid-1"}]
    names = await rows(store, "SELECT milestone FROM milestones ORDER BY 1")
    assert {r["milestone"] for r in names} >= {
        "assigned_cluster",
        "node_ready",
        "gpu_ready",
        "workload_running",
    }


async def test_one_failed_cluster_read_keeps_the_last_snapshot(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    source.answers = [[installed()]] * 3
    down = SourceError(f"kube:{CLUSTER}", SourceErrorKind.TEMPORARILY_UNAVAILABLE, "timeout")
    sources = ClusterSources(conn(), FakeKube(kube(trainer()), down, down), FakeThanos(thanos()))
    engine = engine_for(store, policy, source, clock, sources)
    await engine.run_once()
    before = await open_rows(store)
    clock.now = T0 + timedelta(minutes=5)
    report = await engine.run_once()  # first failure: within grace, nothing changes
    assert report.status == "partial" and await open_rows(store) == before
    clock.now = T0 + timedelta(minutes=10)
    await engine.run_once()  # second consecutive failure reaches grace
    assert await open_rows(store) == {("UNKNOWN", "CLUSTER_UNREACHABLE", 8)}


async def test_cold_start_with_a_failing_cluster_carries_its_assets(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    source.answers = [[installed()]]
    down = SourceError(f"thanos:{CLUSTER}", SourceErrorKind.PERMISSION_DENIED, "HTTP 403")
    sources = ClusterSources(conn(), FakeKube(kube()), FakeThanos(down))
    report = await engine_for(store, policy, source, clock, sources).run_once()
    assert report.coverage["carried_cluster_pending"] == 1
    assert await open_rows(store) == set()


async def test_two_assets_claiming_one_node_are_a_conflict(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    twin = installed(id="srv_2", serial_normalized="cis0002")
    source.answers = [[installed(), twin]]
    sources = ClusterSources(conn(), FakeKube(kube()), FakeThanos(thanos()))
    await engine_for(store, policy, source, clock, sources).run_once()
    assert await open_rows(store) == {("UNKNOWN", "IDENTITY_CONFLICT", 8)}
    conflicts = await rows(store, "SELECT kind, key FROM identity_conflicts")
    assert conflicts == [{"kind": "NODE_CLAIM", "key": f"{CLUSTER}/{NODE}"}]


async def workloads(store: PostgresStore) -> list[dict[str, object]]:
    """The `workloads_now` rows."""
    return await rows(
        store,
        "SELECT holder_ref, reason_code, category, kind, stage_owner, gpus, requests_window,"
        " window_complete, requests_recent, recent_complete FROM workloads_now ORDER BY 1, 2",
    )


async def test_model_requests_come_from_its_pods_and_survive_an_engine_restart(
    store: PostgresStore, policy: Policy
) -> None:
    """ADR-0009: counters read through the pod proxy, prefill skipped, history kept in Postgres."""
    await store.sync_objectives(policy)
    source, clock = FakeSource(), Clock()
    source.answers = [[installed()]] * 3
    pods = kube(model_pod(), model_pod("qwen-kserve-prefill-5d8f9-xyzab", role="prefill"))
    metrics = FakePodMetrics({"qwen-kserve-84fd6f5c76-abcde": 100.0})
    sources = ClusterSources(conn(), FakeKube(pods), FakeThanos(thanos()), metrics)
    engine = engine_for(store, policy, source, clock, sources)
    await engine.run_once()
    assert [t.pod for t in metrics.targets[0]] == ["qwen-kserve-84fd6f5c76-abcde"]
    assert metrics.targets[0][0].port == 8000
    assert ("UNKNOWN", "TRAFFIC_UNOBSERVED", 8) in await open_rows(store)

    metrics.answers = [{"qwen-kserve-84fd6f5c76-abcde": 160.0}]
    clock.now = T0 + timedelta(hours=1)
    await engine.run_once()
    assert ("PRODUCTIVE", "SERVING_IN_USE", 8) in await open_rows(store)
    (row,) = await workloads(store)
    assert row == {
        "holder_ref": "team-a/LLMInferenceService/qwen",
        "reason_code": "SERVING_IN_USE",
        "category": "PRODUCTIVE",
        "kind": "serving",
        "stage_owner": None,
        "gpus": 8,
        "requests_window": 60.0,
        "window_complete": False,
        "requests_recent": 60.0,
        "recent_complete": True,
    }

    metrics.answers = [{"qwen-kserve-84fd6f5c76-abcde": 170.0}]
    clock.now = T0 + timedelta(hours=2)
    restarted = engine_for(store, policy, source, clock, sources)
    await restarted.run_once()
    (row,) = await workloads(store)
    assert (row["requests_window"], row["requests_recent"]) == (70.0, 10.0)
    count = await rows(store, "SELECT count(*) AS n FROM serving_counter_samples")
    assert count == [{"n": 3}]


async def test_a_model_with_a_counter_at_zero_is_apply_waste(
    store: PostgresStore, policy: Policy
) -> None:
    await store.sync_objectives(policy)
    source, clock = FakeSource(), Clock()
    source.answers = [[installed()]]
    metrics = FakePodMetrics({"qwen-kserve-84fd6f5c76-abcde": 0.0})
    sources = ClusterSources(conn(), FakeKube(kube(model_pod())), FakeThanos(thanos()), metrics)
    await engine_for(store, policy, source, clock, sources).run_once()
    (row,) = await workloads(store)
    assert (row["reason_code"], row["stage_owner"], row["window_complete"]) == (
        "NO_REAL_DEMAND",
        "Apply",
        True,
    )


async def test_a_denied_pod_proxy_leaves_models_unknown_and_marks_the_source(
    store: PostgresStore, policy: Policy
) -> None:
    await store.sync_objectives(policy)
    source, clock = FakeSource(), Clock()
    source.answers = [[installed()]]
    metrics = FakePodMetrics({}, denied=True)
    sources = ClusterSources(conn(), FakeKube(kube(model_pod())), FakeThanos(thanos()), metrics)
    await engine_for(store, policy, source, clock, sources).run_once()
    assert ("UNKNOWN", "TRAFFIC_UNOBSERVED", 4) in await open_rows(store)
    state = await rows(
        store,
        "SELECT failed_scans, last_error FROM source_state WHERE name = 'vllm:' || %(c)s",
        c=CLUSTER,
    )
    assert state[0]["failed_scans"] == 1 and "pods/proxy" in state[0]["last_error"]


async def test_dry_run_keeps_counter_readings_but_writes_no_ledger(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    source.answers = [[installed()]]
    metrics = FakePodMetrics({"qwen-kserve-84fd6f5c76-abcde": 5.0})
    sources = ClusterSources(conn(), FakeKube(kube(model_pod())), FakeThanos(thanos()), metrics)
    await engine_for(store, policy, source, clock, sources, dry_run=True).run_once()
    counts = await rows(
        store,
        "SELECT (SELECT count(*) FROM serving_counter_samples) AS samples,"
        " (SELECT count(*) FROM workloads_now) AS workloads,"
        " (SELECT count(*) FROM capacity_intervals) AS intervals",
    )
    assert counts == [{"samples": 1, "workloads": 0, "intervals": 0}]
