"""GPU-level rows 15-22 of ADR-0002 (as changed by ADR-0009) for one Ready node. Pure: no I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from ttv.cluster_view import ClusterView, clean_hostname, holder_ref
from ttv.config import Policy, ServingKind
from ttv.models import (
    Category,
    ClusterNode,
    ClusterPod,
    Flag,
    MilestoneFact,
    Reason,
    Stage,
)

# Allocation precedence when pods claim more GPUs than report healthy (first match wins),
# and each row's ADR-0002 label for the trace.
_RANK: Mapping[Reason, float] = {
    Reason.HOLDER_UNRESOLVED: 15.5,
    Reason.RUNAI_WORKLOAD: 16,
    Reason.ML_WORKLOAD: 16.1,
    Reason.ACTIVITY_UNOBSERVED: 16.2,
    Reason.TRAFFIC_UNOBSERVED: 16.5,
    Reason.SERVING_IN_USE: 17,
    Reason.LOW_USE: 18,
    Reason.NO_REAL_DEMAND: 19,
    Reason.MODEL_NOT_READY: 20,
    Reason.WORKLOAD_NOT_READY: 20.5,
    Reason.LOW_GPU_UTILIZATION: 21,
    Reason.POWER_WITHOUT_POD: 21.5,
    Reason.NO_WORKLOAD: 22,
}
_ROW: Mapping[Reason, str] = {
    Reason.HOLDER_UNRESOLVED: "15b",
    Reason.RUNAI_WORKLOAD: "16",
    Reason.ML_WORKLOAD: "16c",
    Reason.ACTIVITY_UNOBSERVED: "16d",
    Reason.TRAFFIC_UNOBSERVED: "16b",
    Reason.SERVING_IN_USE: "17",
    Reason.LOW_USE: "18",
    Reason.NO_REAL_DEMAND: "19",
    Reason.MODEL_NOT_READY: "20",
    Reason.WORKLOAD_NOT_READY: "20b",
    Reason.LOW_GPU_UTILIZATION: "21",
    Reason.POWER_WITHOUT_POD: "22b",
    Reason.NO_WORKLOAD: "22",
}
_PRODUCTIVE = frozenset({Reason.RUNAI_WORKLOAD, Reason.ML_WORKLOAD, Reason.SERVING_IN_USE})
_UNKNOWN = frozenset(
    {
        Reason.HOLDER_UNRESOLVED,
        Reason.TRAFFIC_UNOBSERVED,
        Reason.ACTIVITY_UNOBSERVED,
        Reason.POWER_WITHOUT_POD,
    }
)
_STAGE: Mapping[Reason, Stage] = {
    Reason.LOW_USE: Stage.SERVING,
    Reason.NO_REAL_DEMAND: Stage.SERVING,
    Reason.MODEL_NOT_READY: Stage.SERVING,
    Reason.WORKLOAD_NOT_READY: Stage.MACHINE_LEARNING,
    Reason.LOW_GPU_UTILIZATION: Stage.MACHINE_LEARNING,
    Reason.NO_WORKLOAD: Stage.EMPTY,
}
_WORKLOAD_PRESENT = frozenset(
    {
        Reason.RUNAI_WORKLOAD,
        Reason.ML_WORKLOAD,
        Reason.SERVING_IN_USE,
        Reason.LOW_USE,
        Reason.NO_REAL_DEMAND,
        Reason.LOW_GPU_UTILIZATION,
    }
)


@dataclass(frozen=True)
class Slice:
    """
    GPUs of one node sharing a classification, before they are split by GPU model.

    Attributes:
        category (Category): Category.
        reason (Reason): Matching row.
        stage (Stage | None): Stage for IDLE slices.
        gpus (int): GPUs.
        holder_ref (str | None): Holding logical workload.
        team (str | None): Consuming team (productive slices only).
        flags (tuple[str, ...]): Flags.
        evidence (Mapping[str, Any]): Facts the row matched on.
    """

    category: Category
    reason: Reason
    stage: Stage | None
    gpus: int
    holder_ref: str | None = None
    team: str | None = None
    flags: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GpuRows:
    """
    The GPU-level outcome for one node.

    Attributes:
        slices (tuple[Slice, ...]): Slices in row order; their GPUs sum to `E`.
        trace (tuple[str, ...]): Row outcomes for `/explain`.
        milestones (tuple[MilestoneFact, ...]): Lifecycle facts seen.
    """

    slices: tuple[Slice, ...]
    trace: tuple[str, ...]
    milestones: tuple[MilestoneFact, ...]


@dataclass(frozen=True)
class _PodClass:
    """One holder pod's classification (internal)."""

    reason: Reason
    holder: str
    gpus: int
    pod: str
    idle_activity: bool = False
    flags: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    ready: bool = False


def gpu_level(
    *,
    expected: int,
    node: ClusterNode,
    view: ClusterView,
    policy: Policy,
    now: datetime,
    base_flags: Sequence[str],
) -> GpuRows:
    """
    Split a node's `E` GPUs over rows 15-22 (ADR-0002); the caller has passed rows 1-14.

    Args:
        expected (int): `E`, GPUs in inventory.
        node (ClusterNode): The asset's node (Ready, schedulable, `A > 0`, `R > 0`).
        view (ClusterView): The cluster's indexed snapshots.
        policy (Policy): Business policy.
        now (datetime): Scan time (warm-up and the `allocated_idle` window).
        base_flags (Sequence[str]): Flags every slice carries (hardware health, C3).

    Returns:
        GpuRows: Slices summing to `expected`, the trace and milestones.
    """
    uuids = view.gpu_uuids(node)
    usable_hw = min(node.allocatable_gpus, len(uuids))
    trace: list[str] = []
    slices: list[Slice] = []
    milestones = [MilestoneFact("gpu_ready", None, True, {"A": node.allocatable_gpus})]

    missing = max(expected - usable_hw, 0)
    trace.append(
        f"15 GPU_MISSING: {missing} (E={expected}, A={node.allocatable_gpus}, R={len(uuids)})"
    )
    if missing:
        evidence = {
            "E": expected,
            "A": node.allocatable_gpus,
            "R": len(uuids),
            "last_xid": dict(view.xid_by_clean.get(clean_hostname(node.name) or node.name, {})),
        }
        slices.append(
            Slice(
                Category.IDLE,
                Reason.GPU_MISSING,
                Stage.GPU_PLATFORM,
                missing,
                None,
                None,
                tuple(base_flags),
                evidence,
            )
        )

    pods = view.pods_by_node.get(node.name, ())
    classes = [_classify_holder(p, pods, view, policy, now) for p in pods if p.gpus > 0]
    remaining = min(usable_hw, expected)
    for group in _group(classes):
        take = min(group.gpus, remaining)
        trace.append(
            f"{_ROW[group.reason]} {group.reason}: "
            f"{take} held by {group.holder_ref}"
            + (f" (claims {group.gpus}, only {take} healthy left)" if take < group.gpus else "")
        )
        remaining -= take
        if take:
            slices.append(
                Slice(
                    _category(group.reason),
                    group.reason,
                    _stage(group.reason),
                    take,
                    group.holder_ref,
                    _team(group, policy)
                    if _category(group.reason) is Category.PRODUCTIVE
                    else None,
                    tuple(dict.fromkeys([*base_flags, *group.flags])),
                    group.evidence,
                )
            )
    powered = _powered_free_gpus(node, pods, view, policy)
    unseen = min(powered, remaining)
    trace.append(f"22b POWER_WITHOUT_POD: {unseen} (no pod, drawing power)")
    if unseen:
        slices.append(
            Slice(
                Category.UNKNOWN,
                Reason.POWER_WITHOUT_POD,
                None,
                unseen,
                None,
                None,
                tuple(base_flags),
                {"powered_gpus_without_pod": powered, "threshold": policy.power.threshold},
            )
        )
        remaining -= unseen
    trace.append(f"22 NO_WORKLOAD: {remaining}")
    if remaining:
        slices.append(
            Slice(
                Category.IDLE,
                Reason.NO_WORKLOAD,
                Stage.EMPTY,
                remaining,
                None,
                None,
                tuple(base_flags),
                {"free_gpus": remaining},
            )
        )

    present = [c for c in classes if c.reason in _WORKLOAD_PRESENT and c.ready]
    if present:
        starts = [c.started_at for c in present if c.started_at is not None]
        milestones.append(
            MilestoneFact(
                "workload_running",
                min(starts) if starts else None,
                True,
                {"holder": present[0].holder},
            )
        )
    if any(c.reason in (Reason.SERVING_IN_USE, Reason.LOW_USE) for c in classes):
        milestones.append(MilestoneFact("first_genuine_request", None, True, {}))
    return GpuRows(tuple(slices), tuple(trace), tuple(milestones))


def _powered_free_gpus(
    node: ClusterNode, pods: Sequence[ClusterPod], view: ClusterView, policy: Policy
) -> int:
    """
    GPUs no pod holds that drew working power in the activity window (row 22b, ADR-0009).

    DCGM series without a pod also come from GPUs whose holder DCGM could not attribute, so
    those holders' GPUs are subtracted first.

    Args:
        node (ClusterNode): The node.
        pods (Sequence[ClusterPod]): Its GPU-relevant pods.
        view (ClusterView): The cluster view.
        policy (Policy): Policy (`power.threshold`).

    Returns:
        int: GPUs in use by something unseen.
    """
    threshold = policy.power.threshold
    if threshold is None:
        return 0
    shares = view.free_power_by_clean.get(clean_hostname(node.name) or node.name, ())
    powered = sum(1 for share in shares if share >= threshold)
    attributed = view.thanos.power_by_pod if view.thanos else {}
    unattributed = sum(p.gpus for p in pods if p.gpus and (p.namespace, p.name) not in attributed)
    return max(powered - unattributed, 0)


def _classify_holder(
    pod: ClusterPod,
    node_pods: Sequence[ClusterPod],
    view: ClusterView,
    policy: Policy,
    now: datetime,
) -> _PodClass:
    """
    Rows 16-21 for one pod holding whole GPUs; Run:ai reservation pods take their group's class.

    Args:
        pod (ClusterPod): The holder.
        node_pods (Sequence[ClusterPod]): Every GPU-relevant pod on the node.
        view (ClusterView): The cluster view.
        policy (Policy): Policy.
        now (datetime): Scan time.

    Returns:
        _PodClass: The classification, carrying `pod.gpus`.
    """
    if pod.namespace in view.reservation_namespaces:
        members = [
            p
            for p in node_pods
            if p.fractional
            and pod.gpu_group is not None
            and p.gpu_group == pod.gpu_group
            and p.namespace not in view.reservation_namespaces
        ]
        evidence = {"reservation_pod": f"{pod.namespace}/{pod.name}", "gpu_group": pod.gpu_group}
        if not members:
            return _PodClass(
                Reason.NO_WORKLOAD, holder_ref(pod), pod.gpus, pod.name, evidence=evidence
            )
        best = min(
            (_classify_pod(m, view, policy, now, activity_of=pod) for m in members),
            key=lambda c: _RANK[c.reason],
        )
        return _PodClass(
            best.reason,
            best.holder,
            pod.gpus,
            best.pod,
            best.idle_activity,
            best.flags,
            {**best.evidence, **evidence},
            best.started_at,
            best.ready,
        )
    return _classify_pod(pod, view, policy, now)


def _classify_pod(
    pod: ClusterPod,
    view: ClusterView,
    policy: Policy,
    now: datetime,
    *,
    activity_of: ClusterPod | None = None,
) -> _PodClass:
    """
    Rows 16-21 for one workload pod, first match wins (ADR-0009).

    Run:ai and every other non-serving workload are Machine Learning, judged by GPU activity;
    recognised serving deployments are judged by their requests.

    Args:
        pod (ClusterPod): The pod.
        view (ClusterView): The cluster view.
        policy (Policy): Policy.
        now (datetime): Scan time.
        activity_of (ClusterPod | None): Pod whose GPU activity to use when this pod has none
            (the Run:ai reservation pod that holds a fractional pod's GPU).

    Returns:
        _PodClass: The classification.
    """
    ref = holder_ref(pod)
    started = pod.started_at
    warming = not pod.ready and (started is None or now - started < policy.serving.warmup_grace)
    base: dict[str, Any] = {
        "pod": f"{pod.namespace}/{pod.name}",
        "ready": pod.ready,
        "phase": pod.phase,
        "started_at": started.isoformat() if started else None,
        "owners": [f"{o.kind}/{o.name}" for o in pod.owners],
    }
    if pod.waiting_reasons:
        base["waiting"] = list(pod.waiting_reasons)

    def make(reason: Reason, *, flags: Sequence[str] = (), **extra: Any) -> _PodClass:
        """
        Build the class for this pod.

        Args:
            reason (Reason): Matching row.
            flags (Sequence[str]): Flags.
            **extra (Any): Extra evidence.

        Returns:
            _PodClass: The class.
        """
        idle = reason is Reason.SERVING_IN_USE and _allocated_idle(pod, view, policy, now)
        return _PodClass(
            reason, ref, pod.gpus, pod.name, idle, tuple(flags), base | extra, started, pod.ready
        )

    if not pod.owners_complete:
        return make(Reason.HOLDER_UNRESOLVED)
    top = pod.owners[-1] if pod.owners else None
    runai = top is not None and top.group == policy.runai.group
    kind = None if runai or top is None else _serving_kind(pod, policy)
    if kind is not None:
        return _serving(pod, ref, view, policy, warming, kind, make)
    if not (pod.ready or warming):
        return make(Reason.WORKLOAD_NOT_READY, kind="ml", runai=runai)
    return _machine_learning(pod, view, policy, now, runai, activity_of, make)


def _serving(
    pod: ClusterPod,
    ref: str,
    view: ClusterView,
    policy: Policy,
    warming: bool,
    kind: ServingKind,
    make: Any,
) -> _PodClass:
    """
    Rows 16b-20 for a pod of a recognised serving deployment, from its requests (ADR-0009).

    Args:
        pod (ClusterPod): The pod.
        ref (str): Its `holder_ref`.
        view (ClusterView): The cluster view (with `demand`).
        policy (Policy): Policy.
        warming (bool): Not Ready but within `warmup_grace`.
        kind (ServingKind): The matched serving kind.
        make (Any): The caller's class builder.

    Returns:
        _PodClass: The classification.
    """
    rules = policy.serving
    demand = view.demand.get(ref)
    held = view.gpus_by_holder.get(ref, 0)
    power_avg = view.power_avg_by_holder.get(ref)
    window_hours = rules.demand_window.total_seconds() / 3600
    evidence: dict[str, Any] = {
        "kind": "serving",
        "serving_kind": kind.kind,
        "metric": rules.metric,
        "deployment_gpus": held,
        "window_hours": window_hours,
        "recent_hours": rules.recent_window.total_seconds() / 3600,
        "gpu_power_avg": power_avg,
    }
    if demand is not None:
        hours = demand.covered_hours if demand.covered_hours > 0 else None
        evidence |= {
            "requests": demand.requests,
            "complete": demand.complete,
            "covered_hours": demand.covered_hours,
            "recent_requests": demand.recent_requests,
            "recent_complete": demand.recent_complete,
            "requests_per_gpu_hour": demand.requests / (held * hours) if held and hours else None,
            "counted_pods": list(demand.pods),
            "unreadable_pods": list(demand.unreadable),
        }
    flags = (Flag.USER_ATTRIBUTION_UNVERIFIED,)
    usable = pod.ready or warming
    if demand is not None and demand.requests > 0:
        threshold = rules.useful_requests_per_gpu_hour
        per_gpu_hour = evidence["requests_per_gpu_hour"]
        young = demand.covered_hours < evidence["recent_hours"]
        if threshold is None or not demand.complete or young:
            if threshold is None or per_gpu_hour is None or per_gpu_hour < threshold:
                flags = (Flag.LOW_USE_UNVERIFIED, *flags)
            return make(Reason.SERVING_IN_USE, flags=flags, **evidence)
        if per_gpu_hour is None or per_gpu_hour < threshold:
            return make(
                Reason.LOW_USE, flags=flags, threshold=threshold, basis="requests", **evidence
            )
        line = policy.power.threshold
        if line is not None and power_avg is not None and power_avg < line:
            return make(Reason.LOW_USE, flags=flags, threshold=line, basis="power", **evidence)
        return make(Reason.SERVING_IN_USE, flags=flags, **evidence)
    if not usable:
        return make(Reason.MODEL_NOT_READY, **evidence)
    if demand is None or not demand.complete:
        return make(Reason.TRAFFIC_UNOBSERVED, **evidence)
    return make(Reason.NO_REAL_DEMAND, flags=flags, **evidence)


def _machine_learning(
    pod: ClusterPod,
    view: ClusterView,
    policy: Policy,
    now: datetime,
    runai: bool,
    activity_of: ClusterPod | None,
    make: Any,
) -> _PodClass:
    """
    Rows 16, 16c, 16d and 21: a running Machine Learning pod, judged by its GPU activity.

    Args:
        pod (ClusterPod): The pod.
        view (ClusterView): The cluster view.
        policy (Policy): Policy (`allocated_idle` is the utilization rule).
        now (datetime): Scan time.
        runai (bool): The top owner is a Run:ai workload.
        activity_of (ClusterPod | None): Fallback pod for the activity lookup.
        make (Any): The caller's class builder.

    Returns:
        _PodClass: The classification.
    """
    rule, line = policy.allocated_idle, policy.power.threshold
    snapshot = view.thanos
    keys = [(pod.namespace, pod.name)]
    if activity_of is not None:
        keys.append((activity_of.namespace, activity_of.name))
    activity = next(
        (snapshot.activity_by_pod[k] for k in keys if snapshot and k in snapshot.activity_by_pod),
        None,
    )
    reading = next(
        (snapshot.power_by_pod[k] for k in keys if snapshot and k in snapshot.power_by_pod), None
    )
    power = reading.peak if reading is not None and line is not None else None
    evidence: dict[str, Any] = {
        "kind": "ml",
        "runai": runai,
        "gpu_busy_peak": activity,
        "gpu_power_peak": power,
        "busy_window_hours": rule.window.total_seconds() / 3600,
        "activity_metric": snapshot.activity_metric if snapshot else None,
    }
    in_use = Reason.RUNAI_WORKLOAD if runai else Reason.ML_WORKLOAD
    age = now - pod.started_at if pod.started_at is not None else None
    if activity is None and power is None:
        if age is not None and age < policy.serving.warmup_grace:
            return make(in_use, **evidence)
        return make(Reason.ACTIVITY_UNOBSERVED, **evidence)
    busy = activity is not None and activity >= rule.threshold
    powered = power is not None and line is not None and power >= line
    if age is not None and age >= rule.window and not busy and not powered:
        return make(
            Reason.LOW_GPU_UTILIZATION, threshold=rule.threshold, power_threshold=line, **evidence
        )
    return make(in_use, **evidence)


def is_serving_pod(pod: ClusterPod, policy: Policy) -> bool:
    """
    Whether rows 16b-20 apply: a pod of a recognised serving deployment that is not Run:ai.

    Args:
        pod (ClusterPod): The pod.
        policy (Policy): Policy.

    Returns:
        bool: True when its counter decides whether the GPUs are used (ADR-0009).
    """
    if not pod.owners_complete or not pod.owners:
        return False
    return pod.owners[-1].group != policy.runai.group and _serving_kind(pod, policy) is not None


def _serving_kind(pod: ClusterPod, policy: Policy) -> ServingKind | None:
    """
    The serving kind whose top-owner group/kind (and image pattern) match the pod.

    Args:
        pod (ClusterPod): The pod (with at least one owner).
        policy (Policy): Policy.

    Returns:
        ServingKind | None: The first match.
    """
    top = pod.owners[-1]
    for kind in policy.serving.kinds:
        if (top.group, top.kind) != (kind.group, kind.kind):
            continue
        pattern = kind.image_pattern
        if pattern is None or any(_regex(pattern).search(i) for i in pod.images):
            return kind
    return None


def _allocated_idle(pod: ClusterPod, view: ClusterView, policy: Policy, now: datetime) -> bool:
    """
    Whether a serving pod stayed below the activity threshold for the whole window (a flag).

    Args:
        pod (ClusterPod): The pod.
        view (ClusterView): The cluster view.
        policy (Policy): Policy.
        now (datetime): Scan time.

    Returns:
        bool: True only with activity evidence covering the window; missing data is not idle.
    """
    rule = policy.allocated_idle
    activity = (view.thanos.activity_by_pod if view.thanos else {}).get((pod.namespace, pod.name))
    old_enough = pod.started_at is not None and now - pod.started_at >= rule.window
    return activity is not None and old_enough and activity < rule.threshold


@dataclass(frozen=True)
class _Group:
    """Holders merged by (reason, holder) (internal)."""

    reason: Reason
    holder_ref: str
    gpus: int
    flags: tuple[str, ...]
    evidence: Mapping[str, Any]
    namespace: str


def _group(classes: Sequence[_PodClass]) -> list[_Group]:
    """
    Merge pods of one workload with one reason; order by row precedence, then holder.

    `allocated_idle` survives only when every merged pod is idle.

    Args:
        classes (Sequence[_PodClass]): Per-pod classes.

    Returns:
        list[_Group]: Groups in allocation order.
    """
    merged: dict[tuple[Reason, str], list[_PodClass]] = {}
    for c in classes:
        merged.setdefault((c.reason, c.holder), []).append(c)
    groups: list[_Group] = []
    for (reason, holder), members in merged.items():
        flags = list(dict.fromkeys(f for m in members for f in m.flags))
        if all(m.idle_activity for m in members):
            flags.append(Flag.ALLOCATED_IDLE)
        evidence = dict(members[0].evidence)
        evidence["pods"] = sorted(str(m.evidence.get("pod", m.pod)) for m in members)
        evidence.pop("pod", None)
        groups.append(
            _Group(
                reason,
                holder,
                sum(m.gpus for m in members),
                tuple(flags),
                evidence,
                holder.split("/", 1)[0],
            )
        )
    groups.sort(key=lambda g: (_RANK[g.reason], g.holder_ref))
    return groups


def _category(reason: Reason) -> Category:
    """
    The category of a GPU-level row.

    Args:
        reason (Reason): The row.

    Returns:
        Category: PRODUCTIVE, UNKNOWN or IDLE.
    """
    if reason in _PRODUCTIVE:
        return Category.PRODUCTIVE
    if reason in _UNKNOWN:
        return Category.UNKNOWN
    return Category.IDLE


def _stage(reason: Reason) -> Stage | None:
    """
    The stage of a GPU-level row: Apply's, Machine Learning's or Samurai's for idle rows.

    Args:
        reason (Reason): The row.

    Returns:
        Stage | None: The stage; None for rows that are not idle.
    """
    return _STAGE.get(reason)


def _team(group: _Group, policy: Policy) -> str:
    """
    The consuming team of a productive group (§20.2 "Pools and teams").

    Args:
        group (_Group): The group.
        policy (Policy): Policy.

    Returns:
        str: Mapped team, else `unassigned`.
    """
    teams = policy.team_map
    return teams.workloads.get(group.holder_ref) or teams.namespaces.get(
        group.namespace, "unassigned"
    )


@lru_cache(maxsize=512)
def _regex(pattern: str) -> re.Pattern[str]:
    """
    Compile and cache a policy regex.

    Args:
        pattern (str): The regex.

    Returns:
        re.Pattern[str]: Compiled pattern.
    """
    return re.compile(pattern)
