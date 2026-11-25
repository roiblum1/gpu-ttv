"""Cluster rows 11-22 of ADR-0002 (with ADR-0009) on synthetic snapshots (fixture-tested)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from conftest import T0, server
from ttv.classify import ClassifyContext, classify
from ttv.cluster_view import ClusterView, build_cluster_view
from ttv.config import Policy, Pool, ServingPolicy, TeamMap
from ttv.models import (
    AssetInput,
    Bucket,
    Category,
    ClusterNode,
    ClusterPod,
    Decision,
    Demand,
    Flag,
    KubeSnapshot,
    OwnerRef,
    PodPower,
    Reason,
    Stage,
    ThanosSnapshot,
)

CLUSTER = "ocp4-prod-gpu-site1"
NODE = "ocp4-prod-site1-gpu-01"
QWEN = "team-a/LLMInferenceService/qwen"


def node(**overrides: Any) -> ClusterNode:
    """A Ready, schedulable node advertising 8 GPUs."""
    base = ClusterNode(
        name=f"{NODE}.example.internal",
        uid="node-uid-1",
        ready=True,
        ready_changed_at=T0 - timedelta(days=2),
        unschedulable=False,
        allocatable_gpus=8,
        labels={"pool": "serving"},
    )
    return dataclasses.replace(base, **overrides)


def pod(name: str, gpus: int, *owners: OwnerRef, **overrides: Any) -> ClusterPod:
    """A Ready, Running pod on the node holding `gpus` GPUs."""
    base = ClusterPod(
        namespace="team-a",
        name=name,
        uid=f"uid-{name}",
        node_name=f"{NODE}.example.internal",
        phase="Running",
        ready=True,
        started_at=T0 - timedelta(days=1),
        gpus=gpus,
        owners=owners,
        images=("registry.example/vllm/vllm-openai:v0.32.0",),
    )
    return dataclasses.replace(base, **overrides)


def llmisvc(name: str) -> tuple[OwnerRef, ...]:
    """Owner chain of an LLMInferenceService decode pod (lab-verified shape)."""
    return (
        OwnerRef("apps", "ReplicaSet", f"{name}-kserve-84fd6f5c76"),
        OwnerRef("apps", "Deployment", f"{name}-kserve"),
        OwnerRef("serving.kserve.io", "LLMInferenceService", name),
    )


def demand(
    requests: float, *, complete: bool = True, covered: float = 24.0, recent: float = 0.0
) -> Demand:
    """A serving deployment's requests, as the engine would compute them from pod counters."""
    return Demand(requests, complete, covered, recent, complete, ("team-a/p",), ())


def view(
    *pods: ClusterPod,
    n: ClusterNode | None = None,
    uuids: int = 8,
    activity: dict[tuple[str, str], float] | None = None,
    requests: dict[str, Demand] | None = None,
    power: dict[tuple[str, str], PodPower] | None = None,
    free_power: tuple[float, ...] = (),
    **kw: Any,
) -> ClusterView:
    """Index a one-node cluster; GPU-holding pods are busy unless `activity` says otherwise."""
    the_node = n or node()
    kube = KubeSnapshot(CLUSTER, "cid", T0, (the_node,), pods, frozenset({"apps"}))
    busy = {(p.namespace, p.name): 0.9 for p in pods} if activity is None else activity
    thanos = ThanosSnapshot(
        CLUSTER,
        T0,
        gpu_uuids_by_node={the_node.name: frozenset(f"GPU-{i}" for i in range(uuids))},
        activity_by_pod=busy,
        activity_metric="DCGM_FI_PROF_GR_ENGINE_ACTIVE",
        power_by_pod=power or {},
        free_gpu_power_by_node={the_node.name: free_power} if free_power else {},
    )
    return build_cluster_view(CLUSTER, kube, thanos, demand=requests, **kw)


def decide(policy: Policy, v: ClusterView | None = None, **fields: Any) -> Decision:
    """Classify the INSTALLED asset on `NODE` against one cluster view."""
    s = server(lifecycle_state="INSTALLED", cluster_name=CLUSTER, reported_name=NODE, **fields)
    clusters = {CLUSTER: v} if v is not None else {}
    ctx = ClassifyContext(policy=policy, clusters=clusters, now=T0)
    decision = classify(AssetInput("cisco:cis0001", s), ctx)
    assert sum(b.gpus for b in decision.buckets) == decision.expected_gpus
    return decision


def summary(decision: Decision) -> set[tuple[str, str, int]]:
    """(category, reason, gpus) per bucket."""
    return {(str(b.category), str(b.reason), b.gpus) for b in decision.buckets}


def rows(decision: Decision) -> list[tuple[str, str | None, int]]:
    """(reason, holder, gpus) per bucket, sorted; keeps duplicates a set would merge."""
    return sorted(
        ((str(b.reason), b.holder_ref, b.gpus) for b in decision.buckets),
        key=lambda r: (r[0], r[1] or "", r[2]),
    )


def only(decision: Decision, reason: Reason) -> Bucket:
    """The single bucket with this reason."""
    matches = [b for b in decision.buckets if b.reason == reason]
    assert len(matches) == 1, decision.buckets
    return matches[0]


# --- rows 11-14: server-level on a target cluster -------------------------------------------


def test_row11_unconfigured_target_cluster_is_not_observed(policy: Policy) -> None:
    assert summary(decide(policy)) == {("UNKNOWN", "CLUSTER_NOT_OBSERVED", 8)}


def test_row11_cluster_failed_beyond_grace_is_unreachable(policy: Policy) -> None:
    assert summary(decide(policy, view(unreachable=True))) == {
        ("UNKNOWN", "CLUSTER_UNREACHABLE", 8)
    }


def test_row12_no_matching_node_is_unknown(policy: Policy) -> None:
    v = view(n=node(name="some-other-node"))
    assert summary(decide(policy, v)) == {("UNKNOWN", "NODE_NOT_FOUND", 8)}


def test_row12_node_join_uses_server_scan_hostname_cleaning(policy: Policy) -> None:
    v = view(n=node(name=f"{NODE.upper()}.Example.Internal"))
    assert summary(decide(policy, v)) == {("IDLE", "NO_WORKLOAD", 8)}


def test_row12_two_nodes_with_one_clean_name_is_a_conflict_not_a_guess(policy: Policy) -> None:
    a, b = node(name=f"{NODE}.a.example"), node(name=f"{NODE}.b.example", uid="node-uid-2")
    kube = KubeSnapshot(CLUSTER, "cid", T0, (a, b), ())
    v = build_cluster_view(CLUSTER, kube, ThanosSnapshot(CLUSTER, T0))
    assert summary(decide(policy, v)) == {("UNKNOWN", "IDENTITY_CONFLICT", 8)}


def test_row13_not_ready_node_is_redbull_with_a_lower_bound_clock(policy: Policy) -> None:
    since = T0 - timedelta(hours=3)
    decision = decide(policy, view(n=node(ready=False, ready_changed_at=since)))
    bucket = only(decision, Reason.NODE_NOT_READY)
    assert (bucket.category, bucket.stage_owner) == (Category.IDLE, "Redbull")
    clock = decision.stage_clocks["PROVISIONING"]
    assert (clock.since, clock.lower_bound) == (since, True)


def test_row13_not_ready_with_critical_hardware_is_redbull(policy: Policy) -> None:
    decision = decide(policy, view(n=node(ready=False)), health_overall="CRITICAL")
    bucket = only(decision, Reason.HARDWARE_CRITICAL)
    assert (bucket.stage_owner, bucket.gpus) == ("Redbull", 8)


def test_c3_ready_node_with_critical_hardware_keeps_gpu_rows_and_flags(policy: Policy) -> None:
    decision = decide(policy, view(), health_overall="CRITICAL")
    bucket = only(decision, Reason.NO_WORKLOAD)
    assert Flag.HARDWARE_CRITICAL in bucket.flags and bucket.stage_owner == "Samurai"


def test_row13_cordoned_node_is_redbull(policy: Policy) -> None:
    decision = decide(policy, view(n=node(unschedulable=True)))
    assert only(decision, Reason.NODE_UNSCHEDULABLE).stage_owner == "Redbull"


@pytest.mark.parametrize(("allocatable", "uuids"), [(0, 8), (8, 0)])
def test_row14_no_allocatable_or_no_dcgm_is_popai(
    policy: Policy, allocatable: int, uuids: int
) -> None:
    decision = decide(policy, view(n=node(allocatable_gpus=allocatable), uuids=uuids))
    bucket = only(decision, Reason.GPU_PLATFORM_NOT_READY)
    assert (bucket.stage_owner, bucket.gpus) == ("POPAI", 8)


# --- rows 15-22: GPU level -------------------------------------------------------------------


def test_row15_gpus_missing_from_dcgm_are_popai(policy: Policy) -> None:
    decision = decide(policy, view(uuids=7))
    assert summary(decision) == {("IDLE", "GPU_MISSING", 1), ("IDLE", "NO_WORKLOAD", 7)}
    assert only(decision, Reason.GPU_MISSING).stage_owner == "POPAI"


def test_row22_empty_healthy_gpus_are_samurais(policy: Policy) -> None:
    bucket = only(decide(policy, view()), Reason.NO_WORKLOAD)
    assert (bucket.category, bucket.stage, bucket.stage_owner, bucket.gpus) == (
        Category.IDLE,
        Stage.EMPTY,
        "Samurai",
        8,
    )


def test_section6_eight_gpu_server_with_two_productive_is_not_fully_productive(
    policy: Policy,
) -> None:
    trainer = pod("train-0", 2, OwnerRef("run.ai", "TrainingWorkload", "llama-ft"))
    decision = decide(policy, view(trainer))
    assert summary(decision) == {("PRODUCTIVE", "RUNAI_WORKLOAD", 2), ("IDLE", "NO_WORKLOAD", 6)}
    productive = only(decision, Reason.RUNAI_WORKLOAD)
    assert (productive.holder_ref, productive.team) == (
        "team-a/TrainingWorkload/llama-ft",
        "unassigned",
    )
    assert productive.evidence["runai"] is True and productive.evidence["gpu_busy_peak"] == 0.9


def test_every_runai_workload_is_machine_learning_judged_by_gpu_use(policy: Policy) -> None:
    """ADR-0009: workspaces and build jobs are no longer idle by kind, only by GPU activity."""
    workspace = pod("ws-0", 1, OwnerRef("run.ai", "InteractiveWorkload", "notebook"))
    build_job = pod(
        "job-0", 1, OwnerRef("run.ai", "RunaiJob", "debug"), labels={"priorityClassName": "build"}
    )
    busy = decide(policy, view(workspace, build_job))
    assert rows(busy) == [
        ("NO_WORKLOAD", None, 6),
        ("RUNAI_WORKLOAD", "team-a/InteractiveWorkload/notebook", 1),
        ("RUNAI_WORKLOAD", "team-a/RunaiJob/debug", 1),
    ]
    quiet = view(
        workspace, build_job, activity={("team-a", "ws-0"): 0.01, ("team-a", "job-0"): 0.6}
    )
    bucket = only(decide(policy, quiet), Reason.LOW_GPU_UTILIZATION)
    assert (bucket.holder_ref, bucket.stage, bucket.stage_owner, bucket.category) == (
        "team-a/InteractiveWorkload/notebook",
        Stage.MACHINE_LEARNING,
        "Machine Learning",
        Category.IDLE,
    )
    assert bucket.evidence["threshold"] == 0.05


def test_row21_ml_waste_needs_low_activity_for_the_whole_window(policy: Policy) -> None:
    owner = OwnerRef("run.ai", "TrainingWorkload", "t")
    idle = view(pod("train-0", 8, owner), activity={("team-a", "train-0"): 0.01})
    assert only(decide(policy, idle), Reason.LOW_GPU_UTILIZATION).gpus == 8
    at_threshold = view(pod("train-0", 8, owner), activity={("team-a", "train-0"): 0.05})
    assert only(decide(policy, at_threshold), Reason.RUNAI_WORKLOAD).gpus == 8
    young = pod("train-0", 8, owner, started_at=T0 - timedelta(hours=5))
    fresh = view(young, activity={("team-a", "train-0"): 0.0})
    assert only(decide(policy, fresh), Reason.RUNAI_WORKLOAD).gpus == 8


def test_row16d_ml_without_gpu_activity_is_unknown_not_in_use(policy: Policy) -> None:
    """Unknown is never zero: no DCGM data for a pod is neither waste nor proven use."""
    trainer = pod("train-0", 4, OwnerRef("run.ai", "TrainingWorkload", "t"))
    decision = decide(policy, view(trainer, activity={}))
    assert summary(decision) == {
        ("UNKNOWN", "ACTIVITY_UNOBSERVED", 4),
        ("IDLE", "NO_WORKLOAD", 4),
    }
    starting = pod(
        "train-0",
        4,
        OwnerRef("run.ai", "TrainingWorkload", "t"),
        started_at=T0 - timedelta(minutes=5),
    )
    assert only(decide(policy, view(starting, activity={})), Reason.RUNAI_WORKLOAD).gpus == 4


def test_row16c_any_other_workload_is_machine_learning(policy: Policy) -> None:
    bench = pod("bench-x", 1, OwnerRef("batch", "Job", "aiperf-1"), images=("aiperf:1",))
    bucket = only(decide(policy, view(bench)), Reason.ML_WORKLOAD)
    assert (bucket.category, bucket.holder_ref, bucket.evidence["pods"]) == (
        Category.PRODUCTIVE,
        "team-a/Job/aiperf-1",
        ["team-a/bench-x"],
    )
    assert bucket.evidence["runai"] is False
    quiet = view(bench, activity={("team-a", "bench-x"): 0.0})
    assert only(decide(policy, quiet), Reason.LOW_GPU_UTILIZATION).stage_owner == "Machine Learning"


def test_row20b_crash_looping_ml_pod_beyond_warmup_is_ml_waste(policy: Policy) -> None:
    crash = pod("dbg-0", 2, ready=False, waiting_reasons=("CrashLoopBackOff",))
    bucket = only(decide(policy, view(crash)), Reason.WORKLOAD_NOT_READY)
    assert (bucket.stage_owner, bucket.evidence["waiting"]) == (
        "Machine Learning",
        ["CrashLoopBackOff"],
    )


def test_row17_serving_in_use_above_one_request_per_gpu_hour(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    decision = decide(policy, view(decode, requests={QWEN: demand(480.0, recent=12.0)}))
    bucket = only(decision, Reason.SERVING_IN_USE)
    assert (bucket.category, bucket.gpus, bucket.flags) == (
        Category.PRODUCTIVE,
        4,
        (Flag.USER_ATTRIBUTION_UNVERIFIED,),
    )
    assert bucket.evidence["requests_per_gpu_hour"] == pytest.approx(480 / (4 * 24))
    assert (bucket.evidence["requests"], bucket.evidence["recent_requests"]) == (480.0, 12.0)


def test_row18_below_one_request_per_gpu_hour_is_apply_low_use(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    low = only(decide(policy, view(decode, requests={QWEN: demand(48.0)})), Reason.LOW_USE)
    assert (low.category, low.stage_owner, low.evidence["threshold"]) == (
        Category.IDLE,
        "Apply",
        1.0,
    )
    assert low.evidence["requests_per_gpu_hour"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("d", "threshold"),
    [
        (demand(48.0, complete=False, covered=20.0), 1.0),
        (demand(1.0, covered=0.5), 1.0),
        (demand(48.0), None),
    ],
    ids=["history-filling", "younger-than-1h", "threshold-off"],
)
def test_low_rate_that_cannot_be_judged_yet_is_in_use_unverified(
    policy: Policy, d: Demand, threshold: float | None
) -> None:
    rules = policy.model_copy(
        update={"serving": ServingPolicy(useful_requests_per_gpu_hour=threshold)}
    )
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    bucket = only(decide(rules, view(decode, requests={QWEN: d})), Reason.SERVING_IN_USE)
    assert Flag.LOW_USE_UNVERIFIED in bucket.flags


def test_row19_ready_serving_with_zero_requests_is_no_real_demand(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 8, *llmisvc("qwen"))
    bucket = only(decide(policy, view(decode, requests={QWEN: demand(0.0)})), Reason.NO_REAL_DEMAND)
    assert (bucket.category, bucket.stage, bucket.stage_owner, bucket.gpus) == (
        Category.IDLE,
        Stage.SERVING,
        "Apply",
        8,
    )


@pytest.mark.parametrize(
    "requests", [{}, {QWEN: demand(0.0, complete=False, covered=3.0)}], ids=["unread", "partial"]
)
def test_unknown_is_never_zero_serving_without_a_full_count_is_unknown(
    policy: Policy, requests: dict[str, Demand]
) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    assert summary(decide(policy, view(decode, requests=requests))) == {
        ("UNKNOWN", "TRAFFIC_UNOBSERVED", 4),
        ("IDLE", "NO_WORKLOAD", 4),
    }


@pytest.mark.parametrize("requests", [{}, {QWEN: demand(0.0)}], ids=["unread", "zero"])
def test_row20_model_not_ready_beyond_warmup_is_apply_waste(
    policy: Policy, requests: dict[str, Demand]
) -> None:
    stuck = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"), ready=False)
    bucket = only(decide(policy, view(stuck, requests=requests)), Reason.MODEL_NOT_READY)
    assert (bucket.category, bucket.stage_owner) == (Category.IDLE, "Apply")


def test_row20_serving_pod_loading_within_warmup_is_judged_by_demand(policy: Policy) -> None:
    loading = pod(
        "qwen-kserve-84fd6f5c76-abcde",
        4,
        *llmisvc("qwen"),
        ready=False,
        started_at=T0 - timedelta(minutes=5),
    )
    decision = decide(policy, view(loading, requests={QWEN: demand(0.0, covered=0.1)}))
    assert only(decision, Reason.NO_REAL_DEMAND).gpus == 4


def test_plain_vllm_deployment_is_recognised_by_image(policy: Policy) -> None:
    owners = (OwnerRef("apps", "ReplicaSet", "chat-7d4b9c"), OwnerRef("apps", "Deployment", "chat"))
    plain = pod("chat-7d4b9c-abcde", 2, *owners)
    v = view(plain, requests={"team-a/Deployment/chat": demand(120.0)})
    assert only(decide(policy, v), Reason.SERVING_IN_USE).gpus == 2
    not_vllm = pod("chat-7d4b9c-abcde", 2, *owners, images=("registry.example/triton:24",))
    assert only(decide(policy, view(not_vllm)), Reason.ML_WORKLOAD).gpus == 2


def test_runai_inference_with_a_vllm_image_is_machine_learning(policy: Policy) -> None:
    infer = pod("infer-0", 2, OwnerRef("run.ai", "InferenceWorkload", "chat"))
    assert only(decide(policy, view(infer)), Reason.RUNAI_WORKLOAD).gpus == 2


def test_unresolved_owner_chain_is_unknown(policy: Policy) -> None:
    lost = pod("x-0", 2, OwnerRef("apps", "ReplicaSet", "x-1"), owners_complete=False)
    assert summary(decide(policy, view(lost))) == {
        ("UNKNOWN", "HOLDER_UNRESOLVED", 2),
        ("IDLE", "NO_WORKLOAD", 6),
    }


def test_d7_reservation_pod_takes_its_fractional_workload_class(policy: Policy) -> None:
    reservation = pod(
        "runai-reservation-gpu-abc",
        1,
        namespace="runai-reservation",
        gpu_group="g1",
        images=("runai/reservation:2.22",),
    )
    fraction = pod(
        "infer-0",
        0,
        OwnerRef("run.ai", "InferenceWorkload", "small-model"),
        fractional=True,
        gpu_group="g1",
    )
    namespaces = frozenset({"runai-reservation"})
    on_reservation = {("runai-reservation", "runai-reservation-gpu-abc"): 0.4}
    v = view(reservation, fraction, activity=on_reservation, reservation_namespaces=namespaces)
    bucket = only(decide(policy, v), Reason.RUNAI_WORKLOAD)
    assert bucket.gpus == 1 and bucket.holder_ref == "team-a/InferenceWorkload/small-model"
    assert bucket.evidence["gpu_busy_peak"] == 0.4


def test_d7_reservation_pod_without_fractions_is_no_workload(policy: Policy) -> None:
    reservation = pod("runai-reservation-gpu-abc", 1, namespace="runai-reservation", gpu_group="g9")
    v = view(reservation, reservation_namespaces=frozenset({"runai-reservation"}))
    assert rows(decide(policy, v)) == [
        ("NO_WORKLOAD", None, 7),
        ("NO_WORKLOAD", "runai-reservation/Pod/runai-reservation-gpu-abc", 1),
    ]


def test_holders_beyond_healthy_gpus_are_capped_in_row_order(policy: Policy) -> None:
    trainer = pod("train-0", 6, OwnerRef("run.ai", "TrainingWorkload", "big"))
    notebook = pod("nb-0", 2, OwnerRef("kubeflow.org", "Notebook", "nb"))
    decision = decide(policy, view(trainer, notebook, uuids=7))
    assert summary(decision) == {
        ("IDLE", "GPU_MISSING", 1),
        ("PRODUCTIVE", "RUNAI_WORKLOAD", 6),
        ("PRODUCTIVE", "ML_WORKLOAD", 1),
    }


def test_allocated_idle_flag_marks_quiet_gpus_of_a_busy_model(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    quiet = view(
        decode,
        activity={("team-a", "qwen-kserve-84fd6f5c76-abcde"): 0.01},
        requests={QWEN: demand(480.0)},
    )
    assert Flag.ALLOCATED_IDLE in only(decide(policy, quiet), Reason.SERVING_IN_USE).flags


def test_pool_and_team_are_recorded(policy: Policy) -> None:
    mapped = policy.model_copy(
        update={
            "pools": [Pool(name="serving-h100", node_selector={"pool": "serving"})],
            "team_map": TeamMap(namespaces={"team-a": "Applai"}),
        }
    )
    trainer = pod("train-0", 2, OwnerRef("run.ai", "TrainingWorkload", "t"))
    decision = decide(mapped, view(trainer))
    assert {b.pool for b in decision.buckets} == {"serving-h100"}
    assert only(decision, Reason.RUNAI_WORKLOAD).team == "Applai"
    assert only(decision, Reason.NO_WORKLOAD).team is None
    assert {(b.cluster, b.node_uid) for b in decision.buckets} == {(CLUSTER, "node-uid-1")}


def test_mixed_models_are_dealt_across_gpu_rows_and_sum_to_e(policy: Policy) -> None:
    trainer = pod("train-0", 2, OwnerRef("run.ai", "TrainingWorkload", "t"))
    models = ("NVIDIA H100 80GB",) * 6 + ("NVIDIA L40S 48GB",) * 2
    decision = decide(policy, view(trainer), gpu_models=models)
    assert sum(b.gpus for b in decision.buckets) == 8
    keys = [(b.reason, b.holder_ref, b.gpu_model) for b in decision.buckets]
    assert len(keys) == len(set(keys))


def test_milestones_seen_on_a_serving_node(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    decision = decide(policy, view(decode, requests={QWEN: demand(300.0)}))
    names = {m.name for m in decision.milestones}
    assert names == {
        "assigned_cluster",
        "node_ready",
        "gpu_ready",
        "workload_running",
        "first_genuine_request",
    }


# --- GPU power as a second sign of work (ADR-0009) ---------------------------------------------


@pytest.mark.parametrize(
    ("activity", "peak", "expected"),
    [
        (0.01, 0.6, Reason.RUNAI_WORKLOAD),
        (0.01, 0.1, Reason.LOW_GPU_UTILIZATION),
        (None, 0.6, Reason.RUNAI_WORKLOAD),
        (None, 0.1, Reason.LOW_GPU_UTILIZATION),
        (0.3, 0.1, Reason.RUNAI_WORKLOAD),
    ],
    ids=["busy-by-power", "both-low", "power-only-busy", "power-only-idle", "busy-by-activity"],
)
def test_ml_waste_needs_both_low_activity_and_low_power(
    policy: Policy, activity: float | None, peak: float, expected: Reason
) -> None:
    trainer = pod("train-0", 4, OwnerRef("run.ai", "TrainingWorkload", "t"))
    key = ("team-a", "train-0")
    v = view(
        trainer,
        activity={} if activity is None else {key: activity},
        power={key: PodPower(peak=peak, average=None, gpus=4)},
    )
    bucket = only(decide(policy, v), expected)
    assert bucket.evidence["gpu_power_peak"] == peak


def test_power_off_leaves_ml_without_activity_unknown(policy: Policy) -> None:
    off = policy.model_copy(update={"power": policy.power.model_copy(update={"threshold": None})})
    trainer = pod("train-0", 4, OwnerRef("run.ai", "TrainingWorkload", "t"))
    power = {("team-a", "train-0"): PodPower(peak=0.9, average=None, gpus=4)}
    assert only(decide(off, view(trainer, activity={}, power=power)), Reason.ACTIVITY_UNOBSERVED)


@pytest.mark.parametrize(
    ("average", "expected"),
    [(0.15, Reason.LOW_USE), (0.4, Reason.SERVING_IN_USE), (None, Reason.SERVING_IN_USE)],
    ids=["gpus-mostly-idle", "gpus-working", "no-power-data"],
)
def test_served_model_with_requests_but_idle_gpus_is_barely_used(
    policy: Policy, average: float | None, expected: Reason
) -> None:
    """The user: a model can get requests yet not use its GPUs well enough."""
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    power = (
        {}
        if average is None
        else {("team-a", decode.name): PodPower(peak=0.5, average=average, gpus=4)}
    )
    v = view(decode, requests={QWEN: demand(480.0)}, power=power)
    bucket = only(decide(policy, v), expected)
    assert bucket.evidence["gpu_power_avg"] == average
    if expected is Reason.LOW_USE:
        assert (bucket.evidence["basis"], bucket.evidence["threshold"]) == ("power", 0.25)


def test_empty_gpus_drawing_power_are_used_by_something_unseen(policy: Policy) -> None:
    decision = decide(policy, view(free_power=(0.6, 0.5, 0.05)))
    assert summary(decision) == {
        ("UNKNOWN", "POWER_WITHOUT_POD", 2),
        ("IDLE", "NO_WORKLOAD", 6),
    }
    assert only(decision, Reason.POWER_WITHOUT_POD).stage_owner is None


def test_power_of_an_unattributed_holder_is_not_counted_as_unseen_use(policy: Policy) -> None:
    """DCGM couldn't name the trainer's pod, so its two GPUs show up as GPUs with no pod."""
    trainer = pod("train-0", 2, OwnerRef("run.ai", "TrainingWorkload", "t"))
    decision = decide(policy, view(trainer, free_power=(0.7, 0.6)))
    assert summary(decision) == {("PRODUCTIVE", "RUNAI_WORKLOAD", 2), ("IDLE", "NO_WORKLOAD", 6)}
