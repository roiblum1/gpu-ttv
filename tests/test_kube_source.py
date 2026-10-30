"""
Tests for the read-only kube API source (fixture-tested; no network).

`tests/fixtures/kube/*.json` are sanitised captures from the OpenShift 4.22 lab cluster (no GPUs);
everything built by the helpers below is synthetic and says so.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from ttv.config import ClusterConnection
from ttv.models import OwnerRef
from ttv.sources import serverscan
from ttv.sources.http import SourceError, SourceErrorKind, UnsafeRequestError
from ttv.sources.kube import (
    ACTIVE_PODS_SELECTOR,
    CLUSTER_VERSION_PATH,
    MAX_OWNER_DEPTH,
    NODES_PATH,
    PODS_PATH,
    KubeSource,
    check_request,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kube"
API = "https://api.lab.test:6443"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
LAB_CLUSTER_ID = "00000000-0000-4000-8000-00000000c064"
GPU = "nvidia.com/gpu"
PLURALS = {
    "ReplicaSet": "replicasets",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "LLMInferenceService": "llminferenceservices",
    "RunaiJob": "runaijobs",
    "TrainingWorkload": "trainingworkloads",
    "ArgoCD": "argocds",
    "GitopsService": "gitopsservices",
    "Node": "nodes",
}

# Synthetic discovery documents: the lab has no `run.ai` group (D5).
RUNAI_V1 = {
    "kind": "APIResourceList",
    "apiVersion": "v1",
    "groupVersion": "run.ai/v1",
    "resources": [
        {"name": "runaijobs", "namespaced": True, "kind": "RunaiJob", "verbs": ["get", "list"]},
        {"name": "runaijobs/status", "namespaced": True, "kind": "RunaiJob", "verbs": ["get"]},
    ],
}
RUNAI_V2ALPHA1 = {
    "kind": "APIResourceList",
    "apiVersion": "v1",
    "groupVersion": "run.ai/v2alpha1",
    "resources": [
        {
            "name": "trainingworkloads",
            "namespaced": True,
            "kind": "TrainingWorkload",
            "verbs": ["get", "list"],
        },
        {
            "name": "interactiveworkloads",
            "namespaced": True,
            "kind": "InteractiveWorkload",
            "verbs": ["get", "list"],
        },
    ],
}
RUNAI_GROUP = {
    "name": "run.ai",
    "versions": [
        {"groupVersion": "run.ai/v2alpha1", "version": "v2alpha1"},
        {"groupVersion": "run.ai/v1", "version": "v1"},
    ],
    "preferredVersion": {"groupVersion": "run.ai/v2alpha1", "version": "v2alpha1"},
}


def load(name: str) -> dict[str, Any]:
    """Load a lab fixture without its `_meta` block."""
    data = json.loads((FIXTURES / name).read_text())
    data.pop("_meta", None)
    return data


def status(code: int, reason: str) -> httpx.Response:
    """A kube `Status` error response."""
    body = {"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": reason}
    return httpx.Response(code, json={**body, "code": code})


class FakeKube:
    """MockTransport handler: lab fixtures by default, plus synthetic objects and faults."""

    def __init__(self) -> None:
        self.cluster_version: dict[str, Any] | None = load("clusterversion.json")
        self.docs: dict[str, Any] = {
            "/apis": load("apis.json"),
            "/api/v1": load("api-v1.json"),
            "/apis/apps/v1": load("apis-apps-v1.json"),
            "/apis/serving.kserve.io/v1alpha2": load("apis-serving.kserve.io-v1alpha2.json"),
        }
        self.nodes: list[dict[str, Any]] = load("nodes.json")["items"]
        self.pods: list[dict[str, Any]] = load("pods-page.json")["items"]
        self.objects: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, int] = {}
        self.delays: dict[str, float] = {}
        self.page_size: int | None = None
        self.expire_continue = 0
        self.apply_selector = True
        self.requests: list[httpx.Request] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def add(self, obj: dict[str, Any]) -> str:
        """Serve an owner object at its namespaced or cluster-scoped path; return the path."""
        api_version, meta = obj["apiVersion"], obj["metadata"]
        prefix = "/api/v1" if api_version == "v1" else f"/apis/{api_version}"
        scope = f"/namespaces/{meta['namespace']}" if meta.get("namespace") else ""
        path = f"{prefix}{scope}/{PLURALS[obj['kind']]}/{meta['name']}"
        self.objects[path] = obj
        return path

    def add_runai(self) -> None:
        """Make the cluster serve the synthetic `run.ai` group."""
        self.docs["/apis"]["groups"].append(RUNAI_GROUP)
        self.docs["/apis/run.ai/v1"] = RUNAI_V1
        self.docs["/apis/run.ai/v2alpha1"] = RUNAI_V2ALPHA1

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.delays:
            await asyncio.sleep(self.delays[path])
        if path in self.errors:
            return status(self.errors[path], "Injected")
        if path == CLUSTER_VERSION_PATH:
            if self.cluster_version is None:
                return status(404, "NotFound")
            return httpx.Response(200, json=self.cluster_version)
        if path in (NODES_PATH, PODS_PATH):
            return self._list(request, self.nodes if path == NODES_PATH else self.pods)
        if path in self.docs:
            return httpx.Response(200, json=self.docs[path])
        if path in self.objects:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0.01)
            finally:
                self.in_flight -= 1
            return httpx.Response(200, json=self.objects[path])
        return status(404, "NotFound")

    def _list(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        params = request.url.params
        if self.apply_selector and params.get("fieldSelector") == ACTIVE_PODS_SELECTOR:
            items = [i for i in items if i["status"].get("phase") not in ("Succeeded", "Failed")]
        token = params.get("continue")
        if token and self.expire_continue > 0:
            self.expire_continue -= 1
            return status(410, "Expired")
        size = self.page_size or int(params["limit"])
        offset = int(token.removeprefix("o")) if token else 0
        meta: dict[str, Any] = {"resourceVersion": "61156650"}
        if offset + size < len(items):
            meta["continue"] = f"o{offset + size}"
        page = items[offset : offset + size]
        return httpx.Response(
            200, json={"kind": "List", "apiVersion": "v1", "metadata": meta, "items": page}
        )


@pytest.fixture
def fake() -> FakeKube:
    return FakeKube()


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("sha256~synthetic-token-one\n")
    return path


def connection(token_file: Path, **overrides: Any) -> ClusterConnection:
    """A connection to the fake API; `cluster_id` defaults to the fixture's fake id."""
    fields: dict[str, Any] = {
        "name": "ocp4-prod-gpu-site1",
        "api_url": API,
        "thanos_url": "https://thanos.lab.test",
        "token_file": token_file,
        "cluster_id": LAB_CLUSTER_ID,
    }
    return ClusterConnection.model_validate(fields | overrides)


def source(
    fake: FakeKube,
    token_file: Path,
    *,
    timeout: float = 30.0,
    concurrency: int = 8,
    **overrides: Any,
) -> KubeSource:
    return KubeSource(
        connection(token_file, **overrides),
        timeout_seconds=timeout,
        owner_concurrency=concurrency,
        transport=httpx.MockTransport(fake.handler),
    )


async def fetch(fake: FakeKube, token_file: Path, **kwargs: Any) -> Any:
    async with source(fake, token_file, **kwargs) as src:
        return await src.fetch(NOW)


# --- synthetic objects ------------------------------------------------------------------------


def container(
    name: str = "main",
    *,
    image: str = "registry.test/vllm/vllm-openai:v0.32.0",
    requests: dict[str, str] | None = None,
    limits: dict[str, str] | None = None,
) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    if requests is not None:
        resources["requests"] = requests
    if limits is not None:
        resources["limits"] = limits
    return {"name": name, "image": image, "resources": resources}


def ref(api_version: str, kind: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"apiVersion": api_version, "kind": kind, "name": name, "controller": True, **extra}


def gpu_pod(
    name: str,
    *,
    ns: str = "ml",
    node: str | None = "gpu-node-1",
    containers: list[dict[str, Any]] | None = None,
    init: list[dict[str, Any]] | None = None,
    phase: str = "Running",
    owners: list[dict[str, Any]] | None = None,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A synthetic pod holding 8 GPUs unless `containers` says otherwise."""
    meta: dict[str, Any] = {"name": name, "namespace": ns, "uid": f"uid-pod-{name}"}
    meta["labels"] = labels or {}
    if annotations:
        meta["annotations"] = annotations
    if owners:
        meta["ownerReferences"] = owners
    spec: dict[str, Any] = {
        "containers": containers or [container(requests={GPU: "8"}, limits={GPU: "8"})]
    }
    if node:
        spec["nodeName"] = node
    if init:
        spec["initContainers"] = init
    pod_status: dict[str, Any] = {
        "phase": phase,
        "startTime": "2026-10-31T10:00:00Z",
        "conditions": [
            {"type": "Ready", "status": "True", "lastTransitionTime": "2026-10-31T10:05:00Z"}
        ],
    }
    if statuses:
        pod_status["containerStatuses"] = statuses
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": spec, "status": pod_status}


def owner_obj(
    api_version: str, kind: str, name: str, *, ns: str = "ml", owner: dict[str, Any] | None = None
) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "namespace": ns, "uid": f"uid-{kind}-{name}"}
    if owner:
        meta["ownerReferences"] = [owner]
    return {"apiVersion": api_version, "kind": kind, "metadata": meta}


def gpu_node(name: str = "gpu-node-1", gpus: str = "8") -> dict[str, Any]:
    """A synthetic GPU node: a lab worker with `nvidia.com/gpu` added."""
    node = copy.deepcopy(load("nodes.json")["items"][1])
    node["metadata"]["name"] = name
    node["metadata"]["uid"] = f"uid-node-{name}"
    node["metadata"]["labels"]["nvidia.com/gpu.product"] = "NVIDIA-H100-80GB-HBM3"
    node["status"]["allocatable"][GPU] = gpus
    node["status"]["capacity"][GPU] = gpus
    return node


def by_name(snapshot: Any) -> dict[str, Any]:
    return {p.name: p for p in snapshot.pods}


# --- lab fixtures ------------------------------------------------------------------------------


async def test_lab_fixtures_with_default_gpu_resource(fake: FakeKube, token_file: Path) -> None:
    snap = await fetch(fake, token_file)

    assert snap.cluster == "ocp4-prod-gpu-site1"
    assert snap.cluster_id == LAB_CLUSTER_ID
    assert [n.name for n in snap.nodes] == [
        "control-plane-cluster-64spr-1",
        "worker-cluster-64spr-1",
        "worker-cluster-64spr-2",
    ]
    assert all(n.ready is True and not n.unschedulable for n in snap.nodes)
    assert all(n.allocatable_gpus == 0 for n in snap.nodes)
    assert snap.nodes[0].ready_changed_at == datetime(2026, 9, 20, 17, 11, 28, tzinfo=UTC)
    assert snap.nodes[0].labels["kubernetes.io/hostname"] == "control-plane-cluster-64spr-1"
    assert snap.pods == ()
    assert "serving.kserve.io" in snap.api_groups
    assert "run.ai" not in snap.api_groups
    assert snap.fetched_at >= NOW
    assert {r.method for r in fake.requests} == {"GET"}
    assert all(
        r.headers["authorization"] == "Bearer sha256~synthetic-token-one" for r in fake.requests
    )
    pods_request = next(r for r in fake.requests if r.url.path == PODS_PATH)
    assert dict(pods_request.url.params) == {
        "limit": "500",
        "fieldSelector": "status.phase!=Succeeded,status.phase!=Failed",
    }
    assert not fake.objects


async def test_lab_owner_walk_reaches_llminferenceservice(fake: FakeKube, token_file: Path) -> None:
    for name in (
        "rs-qwen-tiny-kserve.json",
        "rs-qwen-tiny-kserve-router-scheduler.json",
        "deploy-qwen-tiny-kserve.json",
        "deploy-qwen-tiny-kserve-router-scheduler.json",
        "llmisvc-qwen-tiny.json",
    ):
        fake.add(load(name))

    # The lab has no GPUs; counting whole CPUs as the resource makes its CPU pods "holders".
    snap = await fetch(fake, token_file, gpu_resource="cpu")

    pods = by_name(snap)
    assert sorted(pods) == [
        "qwen-tiny-kserve-84fd6f5c76-b4wzq",
        "qwen-tiny-kserve-router-scheduler-65bdbffc9-jch5b",
    ]
    vllm = pods["qwen-tiny-kserve-84fd6f5c76-b4wzq"]
    assert vllm.owners == (
        OwnerRef("apps", "ReplicaSet", "qwen-tiny-kserve-84fd6f5c76"),
        OwnerRef("apps", "Deployment", "qwen-tiny-kserve"),
        OwnerRef("serving.kserve.io", "LLMInferenceService", "qwen-tiny"),
    )
    assert vllm.owners_complete
    assert (vllm.gpus, vllm.phase, vllm.ready) == (4, "Running", True)
    assert vllm.node_name == "control-plane-cluster-64spr-1"
    assert vllm.started_at == datetime(2026, 10, 30, 12, 6, 27, tzinfo=UTC)
    assert vllm.images == ("docker.io/vllm/vllm-openai-cpu:v0.32.0",)
    assert vllm.labels["llm-d.ai/role"] == "both"
    assert (vllm.waiting_reasons, vllm.restarts, vllm.fractional) == ((), 0, False)
    epp = pods["qwen-tiny-kserve-router-scheduler-65bdbffc9-jch5b"]
    assert [o.kind for o in epp.owners] == ["ReplicaSet", "Deployment", "LLMInferenceService"]
    assert epp.owners[-1] == vllm.owners[-1]
    assert epp.owners_complete
    llmisvc = "/apis/serving.kserve.io/v1alpha2/namespaces/vllm-lab/llminferenceservices/qwen-tiny"
    assert fake.paths().count(llmisvc) == 1


async def test_finished_pods_are_dropped_even_if_the_selector_is_ignored(
    fake: FakeKube, token_file: Path
) -> None:
    fake.apply_selector = False

    snap = await fetch(fake, token_file, gpu_resource="cpu")

    assert {p.phase for p in snap.pods} == {"Running"}
    assert len(snap.pods) == 2


# --- synthetic GPU nodes and pods --------------------------------------------------------------


async def test_gpu_node_fields(fake: FakeKube, token_file: Path) -> None:
    cordoned = gpu_node("gpu-node-2")
    cordoned["spec"]["unschedulable"] = True
    cordoned["status"]["conditions"][-1].update(
        status="False", lastTransitionTime="2026-10-31T09:00:00Z"
    )
    no_ready = gpu_node("gpu-node-3", gpus="8.5")
    no_ready["status"]["conditions"] = []
    fake.nodes = [gpu_node(), cordoned, no_ready, gpu_node("gpu-node-4", gpus="1k")]

    nodes = {n.name: n for n in (await fetch(fake, token_file)).nodes}

    assert nodes["gpu-node-1"].allocatable_gpus == 8
    assert nodes["gpu-node-1"].uid == "uid-node-gpu-node-1"
    assert nodes["gpu-node-1"].labels["nvidia.com/gpu.product"] == "NVIDIA-H100-80GB-HBM3"
    assert nodes["gpu-node-2"].unschedulable
    assert nodes["gpu-node-2"].ready is False
    assert nodes["gpu-node-2"].ready_changed_at == datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    assert (nodes["gpu-node-3"].ready, nodes["gpu-node-3"].ready_changed_at) == (None, None)
    assert nodes["gpu-node-3"].allocatable_gpus == 0
    assert nodes["gpu-node-4"].allocatable_gpus == 0


async def test_gpu_counting_and_pod_fields(fake: FakeKube, token_file: Path) -> None:
    fake.nodes = [gpu_node()]
    fake.pods = [
        gpu_pod(
            "two-containers",
            containers=[
                container("a", requests={GPU: "2"}, limits={GPU: "2"}),
                container("b", image="registry.test/sidecar:1", limits={GPU: "2"}),
            ],
            statuses=[
                {"name": "a", "restartCount": 3, "state": {"running": {}}},
                {
                    "name": "b",
                    "restartCount": 2,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                },
            ],
        ),
        gpu_pod(
            "init-wins",
            phase="Pending",
            containers=[container(requests={GPU: "1"})],
            init=[container("loader", requests={GPU: "4"}), container("warm", requests={GPU: "2"})],
        ),
        gpu_pod("not-integer", containers=[container(requests={GPU: "1.5"}, limits={GPU: "abc"})]),
        gpu_pod("no-gpu", containers=[container(requests={"cpu": "4"})]),
        gpu_pod("unbound", node=None, phase="Pending"),
        gpu_pod("done", phase="Succeeded"),
    ]

    pods = by_name(await fetch(fake, token_file))

    assert sorted(pods) == ["init-wins", "two-containers"]
    two = pods["two-containers"]
    assert two.gpus == 4
    assert two.images == ("registry.test/vllm/vllm-openai:v0.32.0", "registry.test/sidecar:1")
    assert two.waiting_reasons == ("CrashLoopBackOff",)
    assert two.restarts == 5
    assert (two.owners, two.owners_complete) == ((), True)
    assert two.ready
    assert two.uid == "uid-pod-two-containers"
    assert pods["init-wins"].gpus == 4
    assert pods["init-wins"].phase == "Pending"


@pytest.mark.parametrize(
    ("init", "app", "expected"),
    [
        # Regular init 4 runs after sidecar 1 (4 + 1); the app phase is 2 + 1.
        ([("loader", None, "2"), ("proxy", "Always", "1"), ("warm", None, "4")], "2", 5),
        # App 4 runs beside sidecar 1; the init after it needs only 2 + 1.
        ([("proxy", "Always", "1"), ("loader", None, "2")], "4", 5),
        # A sidecar declared after an init container does not add to that init container.
        ([("loader", None, "3"), ("proxy", "Always", "2")], "0", 3),
    ],
)
async def test_sidecar_init_containers_count_like_kubernetes(
    fake: FakeKube,
    token_file: Path,
    init: list[tuple[str, str | None, str]],
    app: str,
    expected: int,
) -> None:
    inits = [
        container(name, requests={GPU: gpus}) | ({"restartPolicy": policy} if policy else {})
        for name, policy, gpus in init
    ]
    fake.nodes = [gpu_node()]
    fake.pods = [gpu_pod("sidecars", containers=[container(requests={GPU: app})], init=inits)]

    pod = (await fetch(fake, token_file)).pods[0]

    assert pod.gpus == expected


async def test_runai_chains(fake: FakeKube, token_file: Path) -> None:
    fake.add_runai()
    fake.nodes = [gpu_node()]
    fake.pods = [
        gpu_pod("legacy-0", ns="runai-team-a", owners=[ref("run.ai/v1", "RunaiJob", "legacy")]),
        gpu_pod("train-0", ns="runai-team-a", owners=[ref("run.ai/v1", "RunaiJob", "train")]),
    ]
    fake.add(owner_obj("run.ai/v1", "RunaiJob", "legacy", ns="runai-team-a"))
    top = ref("run.ai/v2alpha1", "TrainingWorkload", "train", uid="uid-TrainingWorkload-train")
    fake.add(owner_obj("run.ai/v1", "RunaiJob", "train", ns="runai-team-a", owner=top))
    fake.add(owner_obj("run.ai/v2alpha1", "TrainingWorkload", "train", ns="runai-team-a"))

    snap = await fetch(fake, token_file)

    assert "run.ai" in snap.api_groups
    pods = by_name(snap)
    assert pods["legacy-0"].owners == (OwnerRef("run.ai", "RunaiJob", "legacy"),)
    assert pods["legacy-0"].owners_complete
    assert pods["train-0"].owners == (
        OwnerRef("run.ai", "RunaiJob", "train"),
        OwnerRef("run.ai", "TrainingWorkload", "train"),
    )
    assert pods["train-0"].owners_complete


async def test_reservation_and_fractional_pods(fake: FakeKube, token_file: Path) -> None:
    fake.nodes = [gpu_node()]
    fake.pods = [
        gpu_pod(
            "runai-reservation-gpu-gpu-node-1-x7k2p",
            ns="runai-reservation",
            containers=[container(requests={GPU: "1"}, limits={GPU: "1"})],
            annotations={"run.ai/reserve_for_gpu_index": "GPU-0000-synthetic"},
            labels={"runai-gpu-group": "grp-1", "app": "runai-reservation"},
        ),
        gpu_pod(
            "half-gpu",
            ns="runai-team-a",
            containers=[container(requests={"cpu": "2"})],
            annotations={"gpu-fraction": "0.5"},
            labels={"runai-gpu-group": "grp-1"},
        ),
        gpu_pod(
            "by-memory",
            ns="runai-team-a",
            containers=[container(requests={"cpu": "2"})],
            annotations={"gpu-memory": "20000"},
            labels={"runai-gpu-group/grp-2": "true"},
        ),
        gpu_pod(
            "fraction-unbound",
            ns="runai-team-a",
            node=None,
            phase="Pending",
            containers=[container()],
            annotations={"gpu-fraction": "0.5"},
        ),
    ]

    pods = by_name(await fetch(fake, token_file))

    assert sorted(pods) == ["by-memory", "half-gpu", "runai-reservation-gpu-gpu-node-1-x7k2p"]
    reservation = pods["runai-reservation-gpu-gpu-node-1-x7k2p"]
    assert (reservation.gpus, reservation.fractional, reservation.gpu_group) == (1, False, "grp-1")
    assert reservation.namespace == "runai-reservation"
    assert (pods["half-gpu"].gpus, pods["half-gpu"].fractional) == (0, True)
    assert pods["half-gpu"].gpu_group == "grp-1"
    assert (pods["by-memory"].fractional, pods["by-memory"].gpu_group) == (True, "grp-2")


# --- owner walk failures -----------------------------------------------------------------------


def deployment_chain(fake: FakeKube) -> tuple[str, str]:
    """Pod → ReplicaSet → Deployment (top), synthetic; returns the two object paths."""
    fake.nodes = [gpu_node()]
    fake.pods = [gpu_pod("vllm-abc-1", owners=[ref("apps/v1", "ReplicaSet", "vllm-abc")])]
    rs = fake.add(
        owner_obj("apps/v1", "ReplicaSet", "vllm-abc", owner=ref("apps/v1", "Deployment", "vllm"))
    )
    return rs, fake.add(owner_obj("apps/v1", "Deployment", "vllm"))


@pytest.mark.parametrize(
    ("code", "reason"), [(403, "permission_denied"), (404, "confirmed_absent")]
)
async def test_failed_owner_get_keeps_the_partial_chain(
    fake: FakeKube, token_file: Path, code: int, reason: str
) -> None:
    _, deployment = deployment_chain(fake)
    fake.errors[deployment] = code

    with capture_logs() as logs:
        snap = await fetch(fake, token_file)

    pod = snap.pods[0]
    assert pod.owners == (
        OwnerRef("apps", "ReplicaSet", "vllm-abc"),
        OwnerRef("apps", "Deployment", "vllm"),
    )
    assert not pod.owners_complete
    event = next(e for e in logs if e["event"] == "kube_fetch")
    assert event["owner_failures"] == {reason: 1}
    assert event["owners_incomplete"] == 1


async def test_unknown_kind_is_incomplete_and_discovery_is_cached(
    fake: FakeKube, token_file: Path
) -> None:
    deployment_chain(fake)
    fake.pods.append(gpu_pod("odd-0", owners=[ref("apps/v1", "Mystery", "odd")]))

    async with source(fake, token_file) as src:
        first = by_name(await src.fetch(NOW))
        second = by_name(await src.fetch(NOW))

    for pods in (first, second):
        assert pods["odd-0"].owners == (OwnerRef("apps", "Mystery", "odd"),)
        assert not pods["odd-0"].owners_complete
        assert pods["vllm-abc-1"].owners_complete
    # Fetched once per fetch: first load, then one refetch for the still-unknown kind.
    assert fake.paths().count("/apis/apps/v1") == 2


async def test_known_kinds_reuse_cached_discovery(fake: FakeKube, token_file: Path) -> None:
    deployment_chain(fake)

    async with source(fake, token_file) as src:
        await src.fetch(NOW)
        await src.fetch(NOW)

    assert fake.paths().count("/apis/apps/v1") == 1


def gitops_chain(fake: FakeKube) -> str:
    """Pod → StatefulSet → ArgoCD → cluster-scoped GitopsService (lab objects, synthetic pod)."""
    fake.docs["/apis/argoproj.io/v1beta1"] = load("apis-argoproj.io-v1beta1.json")
    fake.docs["/apis/pipelines.openshift.io/v1alpha1"] = load(
        "apis-pipelines.openshift.io-v1alpha1.json"
    )
    sts = load("sts-openshift-gitops-application-controller.json")
    fake.add(sts)
    fake.add(load("argocd-openshift-gitops.json"))
    sts_ref = ref("apps/v1", "StatefulSet", sts["metadata"]["name"], uid=sts["metadata"]["uid"])
    fake.nodes = [gpu_node()]
    fake.pods = [gpu_pod("app-controller-0", ns="openshift-gitops", owners=[sts_ref])]
    return fake.add(load("gitopsservice-cluster.json"))


GITOPS_OWNERS = (
    OwnerRef("apps", "StatefulSet", "openshift-gitops-application-controller"),
    OwnerRef("argoproj.io", "ArgoCD", "openshift-gitops"),
    OwnerRef("pipelines.openshift.io", "GitopsService", "cluster"),
)


async def test_chain_through_a_cluster_scoped_owner_is_complete(
    fake: FakeKube, token_file: Path
) -> None:
    gitops = gitops_chain(fake)

    pod = (await fetch(fake, token_file)).pods[0]

    assert pod.owners == GITOPS_OWNERS
    assert pod.owners_complete
    assert gitops == "/apis/pipelines.openshift.io/v1alpha1/gitopsservices/cluster"
    assert fake.paths().count(gitops) == 1


@pytest.mark.parametrize("code", [403, 404])
async def test_failed_cluster_scoped_owner_get_is_incomplete(
    fake: FakeKube, token_file: Path, code: int
) -> None:
    fake.errors[gitops_chain(fake)] = code

    pod = (await fetch(fake, token_file)).pods[0]

    assert pod.owners == GITOPS_OWNERS
    assert not pod.owners_complete


async def test_cluster_scoped_object_cannot_have_a_namespaced_owner(
    fake: FakeKube, token_file: Path
) -> None:
    gitops = gitops_chain(fake)
    fake.objects[gitops]["metadata"]["ownerReferences"] = [ref("apps/v1", "Deployment", "x")]

    with capture_logs() as logs:
        pod = (await fetch(fake, token_file)).pods[0]

    assert pod.owners == (*GITOPS_OWNERS, OwnerRef("apps", "Deployment", "x"))
    assert not pod.owners_complete
    event = next(e for e in logs if e["event"] == "kube_fetch")
    assert event["owner_failures"] == {"invalid_scope": 1}
    assert not any(p.endswith("/deployments/x") for p in fake.paths())


async def test_static_pod_owned_by_its_node_is_complete(fake: FakeKube, token_file: Path) -> None:
    node = gpu_node()
    fake.nodes = [node]
    fake.add({"apiVersion": "v1", "kind": "Node", **node})
    owner = ref("v1", "Node", "gpu-node-1", uid="uid-node-gpu-node-1")
    fake.pods = [gpu_pod("static-gpu-gpu-node-1", ns="kube-system", owners=[owner])]

    pod = (await fetch(fake, token_file)).pods[0]

    assert pod.owners == (OwnerRef("", "Node", "gpu-node-1"),)
    assert pod.owners_complete
    assert "/api/v1/nodes/gpu-node-1" in fake.paths()


async def test_owner_uid_mismatch_is_incomplete(fake: FakeKube, token_file: Path) -> None:
    deployment_chain(fake)
    fake.pods[0]["metadata"]["ownerReferences"][0]["uid"] = "uid-of-a-deleted-replicaset"

    pod = (await fetch(fake, token_file)).pods[0]

    assert pod.owners == (OwnerRef("apps", "ReplicaSet", "vllm-abc"),)
    assert not pod.owners_complete


async def test_owner_walk_depth_is_bounded(fake: FakeKube, token_file: Path) -> None:
    fake.nodes = [gpu_node()]
    fake.pods = [gpu_pod("deep-0", owners=[ref("apps/v1", "ReplicaSet", "rs-0")])]
    for i in range(12):
        fake.add(
            owner_obj(
                "apps/v1",
                "ReplicaSet",
                f"rs-{i}",
                owner=ref("apps/v1", "ReplicaSet", f"rs-{i + 1}"),
            )
        )

    pod = (await fetch(fake, token_file)).pods[0]

    assert len(pod.owners) == MAX_OWNER_DEPTH == 8
    assert pod.owners[-1].name == "rs-7"
    assert not pod.owners_complete


async def test_owner_gets_are_bounded_and_shared(fake: FakeKube, token_file: Path) -> None:
    fake.nodes = [gpu_node()]
    fake.pods = [
        gpu_pod(f"p-{i}-{j}", owners=[ref("apps/v1", "ReplicaSet", f"rs-{i}")])
        for i in range(10)
        for j in range(2)
    ]
    for i in range(10):
        fake.add(owner_obj("apps/v1", "ReplicaSet", f"rs-{i}"))

    snap = await fetch(fake, token_file, concurrency=3)

    assert all(p.owners_complete for p in snap.pods)
    assert fake.max_in_flight == 3
    assert sum("/replicasets/" in p for p in fake.paths()) == 10


# --- listing ----------------------------------------------------------------------------------


async def test_pagination_follows_continue(fake: FakeKube, token_file: Path) -> None:
    fake.page_size = 2
    fake.nodes = [gpu_node(f"gpu-node-{i}") for i in range(3)]
    fake.pods = [gpu_pod(f"p-{i}") for i in range(3)]

    snap = await fetch(fake, token_file)

    assert [n.name for n in snap.nodes] == ["gpu-node-0", "gpu-node-1", "gpu-node-2"]
    assert [p.name for p in snap.pods] == ["p-0", "p-1", "p-2"]
    pod_pages = [r.url.params for r in fake.requests if r.url.path == PODS_PATH]
    assert [p.get("continue") for p in pod_pages] == [None, "o2"]
    assert all(p["fieldSelector"] == ACTIVE_PODS_SELECTOR for p in pod_pages)


async def test_expired_continue_restarts_the_listing_once(fake: FakeKube, token_file: Path) -> None:
    fake.page_size = 2
    fake.nodes = [gpu_node(f"gpu-node-{i}") for i in range(3)]
    fake.expire_continue = 1

    snap = await fetch(fake, token_file)

    assert [n.name for n in snap.nodes] == ["gpu-node-0", "gpu-node-1", "gpu-node-2"]
    node_pages = [r.url.params.get("continue") for r in fake.requests if r.url.path == NODES_PATH]
    assert node_pages == [None, "o2", None, "o2"]


async def test_continue_expiring_twice_fails_the_fetch(fake: FakeKube, token_file: Path) -> None:
    fake.page_size = 2
    fake.expire_continue = 2

    with pytest.raises(SourceError) as err:
        await fetch(fake, token_file)

    assert err.value.kind is SourceErrorKind.TEMPORARILY_UNAVAILABLE


# --- whole-fetch failures ---------------------------------------------------------------------


async def test_cluster_id_mismatch_is_invalid(fake: FakeKube, token_file: Path) -> None:
    with pytest.raises(SourceError) as err:
        await fetch(fake, token_file, cluster_id="11111111-1111-4111-8111-111111111111")

    assert err.value.kind is SourceErrorKind.INVALID
    assert err.value.source == "kube:ocp4-prod-gpu-site1"
    assert NODES_PATH not in fake.paths()


async def test_configured_cluster_id_but_no_clusterversion_is_invalid(
    fake: FakeKube, token_file: Path
) -> None:
    fake.cluster_version = None

    with pytest.raises(SourceError) as err:
        await fetch(fake, token_file)

    assert err.value.kind is SourceErrorKind.INVALID


async def test_not_openshift_without_configured_id(fake: FakeKube, token_file: Path) -> None:
    fake.cluster_version = None

    snap = await fetch(fake, token_file, cluster_id=None)

    assert snap.cluster_id is None
    assert len(snap.nodes) == 3


@pytest.mark.parametrize(
    ("path", "code", "kind"),
    [
        (NODES_PATH, 401, SourceErrorKind.PERMISSION_DENIED),
        (PODS_PATH, 403, SourceErrorKind.PERMISSION_DENIED),
        (NODES_PATH, 404, SourceErrorKind.INVALID),
        ("/apis", 403, SourceErrorKind.PERMISSION_DENIED),
        (CLUSTER_VERSION_PATH, 403, SourceErrorKind.PERMISSION_DENIED),
    ],
)
async def test_listing_failures_fail_the_fetch(
    fake: FakeKube, token_file: Path, path: str, code: int, kind: SourceErrorKind
) -> None:
    fake.errors[path] = code

    with pytest.raises(SourceError) as err:
        await fetch(fake, token_file)

    assert err.value.kind is kind


async def test_malformed_listing_is_invalid(fake: FakeKube, token_file: Path) -> None:
    fake.nodes = [{"metadata": {"name": "n1"}}]

    with pytest.raises(SourceError) as err:
        await fetch(fake, token_file)

    assert err.value.kind is SourceErrorKind.INVALID


async def test_deadline_fails_a_slow_listing(fake: FakeKube, token_file: Path) -> None:
    fake.delays[NODES_PATH] = 5.0

    with pytest.raises(SourceError) as err:
        await fetch(fake, token_file, timeout=0.2)

    assert err.value.kind is SourceErrorKind.TEMPORARILY_UNAVAILABLE


async def test_deadline_during_owner_walk_keeps_partial_chains(
    fake: FakeKube, token_file: Path
) -> None:
    rs, _ = deployment_chain(fake)
    fake.delays[rs] = 5.0

    snap = await fetch(fake, token_file, timeout=0.3)

    assert snap.pods[0].owners == (OwnerRef("apps", "ReplicaSet", "vllm-abc"),)
    assert not snap.pods[0].owners_complete


# --- token and logs ---------------------------------------------------------------------------


async def test_token_is_reread_every_fetch_and_never_logged(
    fake: FakeKube, token_file: Path
) -> None:
    with capture_logs() as logs:
        async with source(fake, token_file) as src:
            await src.fetch(NOW)
            token_file.write_text("sha256~synthetic-token-two")
            await src.fetch(NOW)

    tokens = {r.headers["authorization"] for r in fake.requests}
    assert tokens == {"Bearer sha256~synthetic-token-one", "Bearer sha256~synthetic-token-two"}
    assert "synthetic-token" not in repr(logs)
    events = [e for e in logs if e["event"] == "kube_fetch"]
    assert len(events) == 2
    assert {"cluster", "nodes", "pods", "owner_gets", "elapsed_ms"} <= set(events[0])
    assert (events[0]["cluster"], events[0]["nodes"], events[0]["pods"]) == (
        "ocp4-prod-gpu-site1",
        3,
        0,
    )


@pytest.mark.parametrize("content", [None, "", "two words"])
async def test_unusable_token_is_permission_denied(
    fake: FakeKube, tmp_path: Path, content: str | None
) -> None:
    token_file = tmp_path / "token"
    if content is not None:
        token_file.write_text(content)

    with pytest.raises(SourceError) as err:
        await fetch(fake, token_file)

    assert err.value.kind is SourceErrorKind.PERMISSION_DENIED
    assert fake.requests == []


def test_constructor_rejects_bad_settings(token_file: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        KubeSource(connection(token_file), timeout_seconds=0)
    with pytest.raises(ValueError, match="positive"):
        KubeSource(connection(token_file), timeout_seconds=1, owner_concurrency=0)


# --- request guard ----------------------------------------------------------------------------

BASE = httpx.URL(API)


@pytest.mark.parametrize(
    "url",
    [
        "/api",
        "/api/v1",
        "/apis",
        "/apis/apps/v1",
        "/apis/run.ai/v2alpha1",
        "/apis/config.openshift.io/v1/clusterversions/version",
        "/api/v1/nodes?limit=500",
        "/api/v1/nodes?limit=500&continue=eyJ2IjoibWV0YS5rOHMuaW8vdjEifQ",
        "/api/v1/pods?limit=500&fieldSelector=status.phase!=Succeeded,status.phase!=Failed",
        "/api/v1/namespaces/ml/pods/vllm-0",
        "/apis/apps/v1/namespaces/ml/replicasets/vllm-abc",
        "/apis/serving.kserve.io/v1alpha2/namespaces/vllm-lab/llminferenceservices/qwen-tiny",
        "/api/v1/nodes/gpu-node-1",
        "/apis/pipelines.openshift.io/v1alpha1/gitopsservices/cluster",
        "/apis/nvidia.com/v1/clusterpolicies/gpu-cluster-policy",
    ],
)
def test_guard_allows(url: str) -> None:
    check_request("GET", httpx.URL(API + url), BASE)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", API + "/api/v1/nodes"),
        ("DELETE", API + "/apis/apps/v1/namespaces/ml/deployments/vllm"),
        ("PATCH", API + "/api/v1/nodes"),
        ("GET", "https://evil.test:6443/api/v1/nodes"),
        ("GET", "https://api.lab.test/api/v1/nodes"),
        ("GET", "http://api.lab.test:6443/api/v1/nodes"),
        ("GET", "https://user:pw@api.lab.test:6443/api"),
        ("GET", API + "/api/v1/namespaces/ml/pods/vllm-0/exec"),
        ("GET", API + "/api/v1/namespaces/ml/pods/vllm-0/log"),
        ("GET", API + "/api/v1/namespaces/ml/services/svc/proxy"),
        ("GET", API + "/api/v1/nodes/gpu-node-1/proxy"),
        ("GET", API + "/api/v1/nodes/"),
        ("GET", API + "/api/v1/secrets"),
        ("GET", API + "/api/v1/secrets/hf-token"),
        ("GET", API + "/apis/oauth.openshift.io/v1/oauthclients/console"),
        ("GET", API + "/apis/oauth.openshift.io/v1/oauthaccesstokens/sha256-abc"),
        ("GET", API + "/api/v1/namespaces/ml"),
        ("GET", API + "/apis/apps/v1/namespaces/ml"),
        ("GET", API + "/apis/nvidia.com/v1/clusterpolicies"),
        ("GET", API + "/apis/nvidia.com/v1/clusterpolicies/p/status"),
        ("GET", API + "/apis/nvidia.com/latest/clusterpolicies/p"),
        ("GET", API + "/apis/apps/v1/namespaces/ml/deployments/vllm/status"),
        ("GET", API + "/apis/apps/v1/namespaces/ml/deployments/vllm/scale"),
        ("GET", API + "/api/v1/pods?watch=true"),
        ("GET", API + "/api/v1/watch/pods"),
        ("GET", API + "/apis/apps/v1/watch/deployments"),
        ("GET", API + "/api/v1/proxy/nodes"),
        ("GET", API + "/api/v1/pods?limit=500&labelSelector=app"),
        ("GET", API + "/api/v1/pods?limit=1&limit=2"),
        ("GET", API + "/api/v1/pods?limit=all"),
        ("GET", API + "/apis/apps/v1/namespaces/ml/replicasets/rs?watch=1"),
        ("GET", API + "/apis?timeout=1s"),
        ("GET", API + "/api/v1/namespaces/ml/pods"),
        ("GET", API + "/apis/apps/v1/deployments"),
        ("GET", API + "/api/v1/namespaces/ml/secrets/hf-token"),
        ("GET", API + "/api/v1/namespaces/ml/pods/../../nodes"),
        ("GET", API + "/api/v1/namespaces/%2e%2e/pods/vllm-0"),
        ("GET", API + "/api/v1/namespaces/ml%2Fx/pods/vllm-0"),
        ("GET", API + "/api/v1/namespaces/ML/pods/vllm-0"),
        ("GET", API + "/apis/apps/latest"),
        ("GET", API + "/apis/apps"),
        ("GET", API + "/version"),
        ("GET", API + "/"),
    ],
)
def test_guard_refuses(method: str, url: str) -> None:
    with pytest.raises(UnsafeRequestError):
        check_request(method, httpx.URL(url), BASE)


def test_guard_respects_a_path_prefix() -> None:
    base = httpx.URL("https://proxy.test/k8s/clusters/c-1")
    check_request("GET", httpx.URL("https://proxy.test/k8s/clusters/c-1/api/v1/nodes"), base)
    with pytest.raises(UnsafeRequestError):
        check_request("GET", httpx.URL("https://proxy.test/api/v1/nodes"), base)


async def test_guard_runs_before_the_transport(fake: FakeKube, token_file: Path) -> None:
    async with source(fake, token_file) as src:
        client = src._client
        with pytest.raises(UnsafeRequestError):
            await client.post(NODES_PATH, json={})
        with pytest.raises(UnsafeRequestError):
            await client.get("/api/v1/namespaces/ml/pods/vllm-0/exec")
        with pytest.raises(UnsafeRequestError):
            await client.get("https://evil.test/api/v1/nodes")

    assert fake.requests == []


def _iter_fixture_names() -> Iterator[str]:
    yield from sorted(p.name for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", list(_iter_fixture_names()))
def test_fixtures_are_labelled_and_sanitised(name: str) -> None:
    data = json.loads((FIXTURES / name).read_text())
    assert data.pop("_meta")["evidence"] == "fixture-tested"
    raw = json.dumps(data)
    assert "managedFields" not in raw
    assert '"podIP"' not in raw
    assert '"env"' not in raw


def test_unsafe_request_error_is_shared() -> None:
    assert serverscan.UnsafeRequestError is UnsafeRequestError
