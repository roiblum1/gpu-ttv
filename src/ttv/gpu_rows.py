"""GPU-level rows 15-22 of ADR-0002 for one Ready, GPU-ready node. Pure: no I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from ttv.cluster_view import ClusterView, clean_hostname, holder_ref
from ttv.config import CounterOfRecord, Policy, ServingKind
from ttv.models import (
    Category,
    ClusterNode,
    ClusterPod,
    Flag,
    MilestoneFact,
    Reason,
    Stage,
)

# Allocation precedence when pods claim more GPUs than report healthy (first match wins).
_RANK: Mapping[Reason, float] = {
    Reason.HOLDER_UNRESOLVED: 15.5,
    Reason.RUNAI_WORKLOAD: 16,
    Reason.TRAFFIC_UNOBSERVED: 16.5,
    Reason.SERVING_IN_USE: 17,
    Reason.LOW_USE: 18,
    Reason.NO_REAL_DEMAND: 19,
    Reason.WORKLOAD_NOT_READY: 20,
    Reason.OTHER_WORKLOAD: 21,
    Reason.NO_WORKLOAD: 22,
}
_WORKLOAD_PRESENT = frozenset(
    {Reason.RUNAI_WORKLOAD, Reason.SERVING_IN_USE, Reason.LOW_USE, Reason.NO_REAL_DEMAND}
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
            f"{int(_RANK[group.reason])} {group.reason}: "
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
    trace.append(f"22 NO_WORKLOAD: {remaining}")
    if remaining:
        slices.append(
            Slice(
                Category.IDLE,
                Reason.NO_WORKLOAD,
                Stage.SERVING,
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
            (_classify_pod(m, view, policy, now) for m in members), key=lambda c: _RANK[c.reason]
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


def _classify_pod(pod: ClusterPod, view: ClusterView, policy: Policy, now: datetime) -> _PodClass:
    """
    Rows 16-21 for one workload pod, first match wins.

    Args:
        pod (ClusterPod): The pod.
        view (ClusterView): The cluster view.
        policy (Policy): Policy.
        now (datetime): Scan time.

    Returns:
        _PodClass: The classification.
    """
    ref = holder_ref(pod)
    started = pod.started_at
    warming = not pod.ready and (started is None or now - started < policy.serving.warmup_grace)
    usable = pod.ready or warming
    base: dict[str, Any] = {
        "pod": f"{pod.namespace}/{pod.name}",
        "ready": pod.ready,
        "phase": pod.phase,
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
        idle = reason in (Reason.RUNAI_WORKLOAD, Reason.SERVING_IN_USE) and _allocated_idle(
            pod, view, policy, now
        )
        return _PodClass(
            reason, ref, pod.gpus, pod.name, idle, tuple(flags), base | extra, started, pod.ready
        )

    if not pod.owners_complete:
        return make(Reason.HOLDER_UNRESOLVED)
    top = pod.owners[-1] if pod.owners else None
    runai = policy.runai
    if top is not None and top.group == runai.group and top.kind in runai.productive_kinds:
        interactive = (
            top.kind == "RunaiJob"
            and pod.labels.get(runai.priority_label) in runai.interactive_priority_classes
        )
        if not interactive and (pod.phase == "Running" or warming):
            return make(Reason.RUNAI_WORKLOAD, runai_kind=top.kind)

    kind = _serving_kind(pod, policy) if top is not None else None
    if kind is not None and top is not None:
        counter, requests, counted = _demand(view, kind, pod.namespace, top.name)
        if counter is None or requests is None:
            return make(
                Reason.TRAFFIC_UNOBSERVED,
                serving_kind=kind.kind,
                tried=[c.selector for c in kind.counters],
            )
        held = view.gpus_by_holder.get(ref, 0)
        hours = policy.serving.demand_window.total_seconds() / 3600
        per_gpu_hour = requests / (held * hours) if held and hours else None
        demand: dict[str, Any] = {
            "counter": counter.selector,
            "requests": requests,
            "window_hours": hours,
            "deployment_gpus": held,
            "requests_per_gpu_hour": per_gpu_hour,
            "counted_pods": counted,
        }
        attribution = () if counter.excludes_synthetic else (Flag.USER_ATTRIBUTION_UNVERIFIED,)
        if requests > 0:
            threshold = policy.serving.useful_requests_per_gpu_hour
            if threshold is None:
                return make(
                    Reason.SERVING_IN_USE, flags=(Flag.LOW_USE_UNVERIFIED, *attribution), **demand
                )
            if per_gpu_hour is not None and per_gpu_hour >= threshold:
                return make(Reason.SERVING_IN_USE, flags=attribution, **demand)
            return make(Reason.LOW_USE, flags=attribution, threshold=threshold, **demand)
        if usable:
            return make(Reason.NO_REAL_DEMAND, flags=attribution, **demand)
        return make(Reason.WORKLOAD_NOT_READY, **demand)

    if not usable:
        return make(Reason.WORKLOAD_NOT_READY)
    return make(Reason.OTHER_WORKLOAD)


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


def _demand(
    view: ClusterView, kind: ServingKind, namespace: str, name: str
) -> tuple[CounterOfRecord | None, float | None, list[str]]:
    """
    Genuine requests of one logical deployment from its counter of record (D8).

    The first counter candidate with series for this deployment's pods wins; candidates are
    never added together (the same requests appear under several names).

    Args:
        view (ClusterView): The cluster view.
        kind (ServingKind): The deployment's serving kind.
        namespace (str): Namespace.
        name (str): Logical deployment (top owner) name.

    Returns:
        tuple[CounterOfRecord | None, float | None, list[str]]: The counter, `increase()` over
        the demand window, and the pods counted; `(None, None, [])` when no candidate has series.
    """
    counters = view.thanos.counters if view.thanos else {}
    for counter in kind.counters:
        values = counters.get(counter.selector)
        if values is None:
            continue
        pattern = _regex(counter.pod_regex.replace("{name}", re.escape(name)))
        matched = {
            pod: v for (ns, pod), v in values.items() if ns == namespace and pattern.match(pod)
        }
        if matched:
            return counter, sum(matched.values()), sorted(matched)
    return None, None, []


def _allocated_idle(pod: ClusterPod, view: ClusterView, policy: Policy, now: datetime) -> bool:
    """
    Whether a productive pod stayed below the activity threshold for the whole window.

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
    if reason in (Reason.RUNAI_WORKLOAD, Reason.SERVING_IN_USE):
        return Category.PRODUCTIVE
    if reason in (Reason.HOLDER_UNRESOLVED, Reason.TRAFFIC_UNOBSERVED):
        return Category.UNKNOWN
    return Category.IDLE


def _stage(reason: Reason) -> Stage | None:
    """
    The stage of a GPU-level row: Serving for idle holders, none otherwise.

    Args:
        reason (Reason): The row.

    Returns:
        Stage | None: The stage.
    """
    return Stage.SERVING if _category(reason) is Category.IDLE else None


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
