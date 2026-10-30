"""Cluster rows 11-22 of ADR-0002 on synthetic snapshots (fixture-tested, not lab evidence)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from conftest import T0, server
from ttv.classify import ClassifyContext, classify
from ttv.cluster_view import ClusterView, build_cluster_view
from ttv.config import CounterOfRecord, Policy, Pool, ServingPolicy, TeamMap
from ttv.models import (
    AssetInput,
    Bucket,
    Category,
    ClusterNode,
    ClusterPod,
    Decision,
    Flag,
    KubeSnapshot,
    OwnerRef,
    Reason,
    ThanosSnapshot,
)

CLUSTER = "ocp4-prod-gpu-site1"
NODE = "ocp4-prod-site1-gpu-01"
EPP = "llm_d_epp_request_total"
VLLM = "vllm:request_success_total"


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


def view(
    *pods: ClusterPod,
    n: ClusterNode | None = None,
    uuids: int = 8,
    counters: dict[str, dict[tuple[str, str], float]] | None = None,
    activity: dict[tuple[str, str], float] | None = None,
    **kw: Any,
) -> ClusterView:
    """Index a one-node cluster."""
    the_node = n or node()
    kube = KubeSnapshot(CLUSTER, "cid", T0, (the_node,), pods, frozenset({"apps"}))
    thanos = ThanosSnapshot(
        CLUSTER,
        T0,
        gpu_uuids_by_node={the_node.name: frozenset(f"GPU-{i}" for i in range(uuids))},
        activity_by_pod=activity or {},
        counters=counters or {},
    )
    return build_cluster_view(CLUSTER, kube, thanos, **kw)


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


def test_row13_not_ready_with_critical_hardware_is_dc(policy: Policy) -> None:
    decision = decide(policy, view(n=node(ready=False)), health_overall="CRITICAL")
    bucket = only(decision, Reason.HARDWARE_CRITICAL)
    assert (bucket.stage_owner, bucket.gpus) == ("DC", 8)


def test_c3_ready_node_with_critical_hardware_keeps_gpu_rows_and_flags(policy: Policy) -> None:
    decision = decide(policy, view(), health_overall="CRITICAL")
    bucket = only(decision, Reason.NO_WORKLOAD)
    assert Flag.HARDWARE_CRITICAL in bucket.flags and bucket.stage_owner == "Serving"


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


def test_c6_interactive_runai_work_is_idle(policy: Policy) -> None:
    workspace = pod("ws-0", 1, OwnerRef("run.ai", "InteractiveWorkload", "notebook"))
    build_job = pod(
        "job-0", 1, OwnerRef("run.ai", "RunaiJob", "debug"), labels={"priorityClassName": "build"}
    )
    decision = decide(policy, view(workspace, build_job))
    assert rows(decision) == [
        ("NO_WORKLOAD", None, 6),
        ("OTHER_WORKLOAD", "team-a/InteractiveWorkload/notebook", 1),
        ("OTHER_WORKLOAD", "team-a/RunaiJob/debug", 1),
    ]
    assert {b.holder_ref for b in decision.buckets if b.reason == Reason.OTHER_WORKLOAD} == {
        "team-a/InteractiveWorkload/notebook",
        "team-a/RunaiJob/debug",
    }


def test_row17_serving_with_genuine_requests_before_i2_is_flagged_unverified(
    policy: Policy,
) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    counters = {EPP: {("team-a", "qwen-kserve-router-scheduler-65bdbffc9-jch5b"): 480.0}}
    decision = decide(policy, view(decode, counters=counters))
    bucket = only(decision, Reason.SERVING_IN_USE)
    assert bucket.category == Category.PRODUCTIVE and bucket.gpus == 4
    assert Flag.LOW_USE_UNVERIFIED in bucket.flags
    assert Flag.USER_ATTRIBUTION_UNVERIFIED in bucket.flags
    assert bucket.evidence["requests_per_gpu_hour"] == pytest.approx(480 / (4 * 24))


def test_d8_counters_of_record_are_never_added(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    epp_pod = ("team-a", "qwen-kserve-router-scheduler-65bdbffc9-jch5b")
    counters = {
        EPP: {epp_pod: 5.0},
        "inference_objective_request_total": {epp_pod: 5.0},
        VLLM: {("team-a", "qwen-kserve-84fd6f5c76-abcde"): 5.0},
    }
    bucket = only(decide(policy, view(decode, counters=counters)), Reason.SERVING_IN_USE)
    assert (bucket.evidence["counter"], bucket.evidence["requests"]) == (EPP, 5.0)


def test_d8_falls_back_to_decode_vllm_counter_and_ignores_prefill(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    counters = {
        VLLM: {
            ("team-a", "qwen-kserve-84fd6f5c76-abcde"): 7.0,
            ("team-a", "qwen-kserve-856fc8d7db-zzzzz"): 3.0,
            ("team-a", "qwen-kserve-prefill-7d9f8b-xxxxx"): 10.0,
        }
    }
    bucket = only(decide(policy, view(decode, counters=counters)), Reason.SERVING_IN_USE)
    assert (bucket.evidence["counter"], bucket.evidence["requests"]) == (VLLM, 10.0)


def test_unknown_is_never_zero_serving_without_telemetry_is_unknown(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    other = {EPP: {("team-a", "other-kserve-router-scheduler-1-a"): 0.0}}
    decision = decide(policy, view(decode, counters=other))
    assert summary(decision) == {
        ("UNKNOWN", "TRAFFIC_UNOBSERVED", 4),
        ("IDLE", "NO_WORKLOAD", 4),
    }


def test_row19_ready_serving_with_zero_requests_is_no_real_demand(policy: Policy) -> None:
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 8, *llmisvc("qwen"))
    counters = {EPP: {("team-a", "qwen-kserve-router-scheduler-65bdbffc9-jch5b"): 0.0}}
    decision = decide(policy, view(decode, counters=counters))
    bucket = only(decision, Reason.NO_REAL_DEMAND)
    assert (bucket.category, bucket.stage_owner, bucket.gpus) == (Category.IDLE, "Serving", 8)


def test_row18_low_use_applies_only_once_i2_is_approved(policy: Policy) -> None:
    approved = policy.model_copy(
        update={"serving": ServingPolicy(useful_requests_per_gpu_hour=10.0)}
    )
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    counters = {EPP: {("team-a", "qwen-kserve-router-scheduler-65bdbffc9-jch5b"): 480.0}}
    low = only(decide(approved, view(decode, counters=counters)), Reason.LOW_USE)
    assert low.category == Category.IDLE and low.evidence["threshold"] == 10.0
    busy = {EPP: {("team-a", "qwen-kserve-router-scheduler-65bdbffc9-jch5b"): 960.0}}
    assert only(decide(approved, view(decode, counters=busy)), Reason.SERVING_IN_USE).flags == (
        Flag.USER_ATTRIBUTION_UNVERIFIED,
    )


def test_d9_counter_with_synthetic_filter_drops_the_attribution_flag(policy: Policy) -> None:
    filtered = CounterOfRecord(
        metric=EPP,
        pod_regex="^{name}-kserve-router-scheduler-[a-z0-9]+-[a-z0-9]+$",
        matchers='fairness_id!="canary"',
    )
    kinds = [policy.serving.kinds[0].model_copy(update={"counters": [filtered]})]
    custom = policy.model_copy(update={"serving": ServingPolicy(kinds=kinds)})
    decode = pod("qwen-kserve-84fd6f5c76-abcde", 4, *llmisvc("qwen"))
    counters = {filtered.selector: {("team-a", "qwen-kserve-router-scheduler-1-a"): 3.0}}
    bucket = only(decide(custom, view(decode, counters=counters)), Reason.SERVING_IN_USE)
    assert bucket.flags == (Flag.LOW_USE_UNVERIFIED,)


def test_row20_crash_looping_pod_beyond_warmup_is_not_ready(policy: Policy) -> None:
    crash = pod("dbg-0", 2, ready=False, waiting_reasons=("CrashLoopBackOff",))
    bucket = only(decide(policy, view(crash)), Reason.WORKLOAD_NOT_READY)
    assert bucket.evidence["waiting"] == ["CrashLoopBackOff"]


def test_row20_serving_pod_loading_within_warmup_is_judged_by_demand(policy: Policy) -> None:
    loading = pod(
        "qwen-kserve-84fd6f5c76-abcde",
        4,
        *llmisvc("qwen"),
        ready=False,
        started_at=T0 - timedelta(minutes=5),
    )
    counters = {EPP: {("team-a", "qwen-kserve-router-scheduler-1-a"): 0.0}}
    assert only(decide(policy, view(loading, counters=counters)), Reason.NO_REAL_DEMAND).gpus == 4


def test_row21_other_workload_names_its_holder(policy: Policy) -> None:
    bench = pod("bench-x", 1, OwnerRef("batch", "Job", "aiperf-1"), images=("aiperf:1",))
    bucket = only(decide(policy, view(bench)), Reason.OTHER_WORKLOAD)
    assert bucket.holder_ref == "team-a/Job/aiperf-1"
    assert bucket.evidence["pods"] == ["team-a/bench-x"]


def test_plain_vllm_deployment_is_recognised_by_image(policy: Policy) -> None:
    owners = (OwnerRef("apps", "ReplicaSet", "chat-7d4b9c"), OwnerRef("apps", "Deployment", "chat"))
    plain = pod("chat-7d4b9c-abcde", 2, *owners)
    counters = {VLLM: {("team-a", "chat-7d4b9c-abcde"): 12.0}}
    assert only(decide(policy, view(plain, counters=counters)), Reason.SERVING_IN_USE).gpus == 2
    not_vllm = pod("chat-7d4b9c-abcde", 2, *owners, images=("registry.example/triton:24",))
    assert only(decide(policy, view(not_vllm)), Reason.OTHER_WORKLOAD).gpus == 2


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
    v = view(reservation, fraction, reservation_namespaces=frozenset({"runai-reservation"}))
    bucket = only(decide(policy, v), Reason.RUNAI_WORKLOAD)
    assert bucket.gpus == 1 and bucket.holder_ref == "team-a/InferenceWorkload/small-model"


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
        ("IDLE", "OTHER_WORKLOAD", 1),
    }


def test_allocated_idle_flag_needs_low_activity_for_the_whole_window(policy: Policy) -> None:
    trainer = pod("train-0", 8, OwnerRef("run.ai", "TrainingWorkload", "t"))
    idle = view(trainer, activity={("team-a", "train-0"): 0.01})
    assert Flag.ALLOCATED_IDLE in only(decide(policy, idle), Reason.RUNAI_WORKLOAD).flags
    no_data = view(trainer)
    assert Flag.ALLOCATED_IDLE not in only(decide(policy, no_data), Reason.RUNAI_WORKLOAD).flags
    young = pod("train-0", 8, OwnerRef("run.ai", "TrainingWorkload", "t"), started_at=T0)
    fresh = view(young, activity={("team-a", "train-0"): 0.0})
    assert Flag.ALLOCATED_IDLE not in only(decide(policy, fresh), Reason.RUNAI_WORKLOAD).flags


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
    counters = {EPP: {("team-a", "qwen-kserve-router-scheduler-1-a"): 3.0}}
    decision = decide(policy, view(decode, counters=counters))
    names = {m.name for m in decision.milestones}
    assert names == {
        "assigned_cluster",
        "node_ready",
        "gpu_ready",
        "workload_running",
        "first_genuine_request",
    }
