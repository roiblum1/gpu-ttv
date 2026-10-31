"""Domain types shared by the sources, the classifier and the ledger (ADR-0002)."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class Category(enum.StrEnum):
    """Mutually exclusive capacity categories; only IDLE and PRODUCTIVE are ever valued."""

    PRODUCTIVE = "PRODUCTIVE"
    IDLE = "IDLE"
    UNKNOWN = "UNKNOWN"
    NOT_CHARGED = "NOT_CHARGED"


class Stage(enum.StrEnum):
    """Pipeline stages; each maps to a configurable owning team."""

    PRE_PROVISIONING = "PRE_PROVISIONING"
    PROVISIONING = "PROVISIONING"
    GPU_PLATFORM = "GPU_PLATFORM"
    SERVING = "SERVING"


class Reason(enum.StrEnum):
    """Reason codes from ADR-0002, in rule order."""

    EXCLUDED = "EXCLUDED"
    SOURCE_STALE = "SOURCE_STALE"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    PROFILE_NONE = "PROFILE_NONE"
    OTHER_INTENDED_USE = "OTHER_INTENDED_USE"
    HANDOVER_PENDING = "HANDOVER_PENDING"
    MAINTENANCE = "MAINTENANCE"
    HARDWARE_CRITICAL = "HARDWARE_CRITICAL"
    BMC_UNREACHABLE = "BMC_UNREACHABLE"
    CONTESTED = "CONTESTED"
    NOT_IN_CLUSTER = "NOT_IN_CLUSTER"
    IN_MCE_INVENTORY = "IN_MCE_INVENTORY"
    OUTSIDE_TARGET_CLUSTERS = "OUTSIDE_TARGET_CLUSTERS"
    MEMBERSHIP_UNKNOWN = "MEMBERSHIP_UNKNOWN"
    CLUSTER_NOT_OBSERVED = "CLUSTER_NOT_OBSERVED"
    CLUSTER_UNREACHABLE = "CLUSTER_UNREACHABLE"
    NODE_NOT_FOUND = "NODE_NOT_FOUND"
    NODE_NOT_READY = "NODE_NOT_READY"
    NODE_UNSCHEDULABLE = "NODE_UNSCHEDULABLE"
    GPU_PLATFORM_NOT_READY = "GPU_PLATFORM_NOT_READY"
    GPU_MISSING = "GPU_MISSING"
    HOLDER_UNRESOLVED = "HOLDER_UNRESOLVED"
    RUNAI_WORKLOAD = "RUNAI_WORKLOAD"
    TRAFFIC_UNOBSERVED = "TRAFFIC_UNOBSERVED"
    SERVING_IN_USE = "SERVING_IN_USE"
    LOW_USE = "LOW_USE"
    NO_REAL_DEMAND = "NO_REAL_DEMAND"
    WORKLOAD_NOT_READY = "WORKLOAD_NOT_READY"
    OTHER_WORKLOAD = "OTHER_WORKLOAD"
    NO_WORKLOAD = "NO_WORKLOAD"


class Flag(enum.StrEnum):
    """Annotations that never change a bucket's category."""

    HARDWARE_CRITICAL = "hardware_critical"
    BMC_UNREACHABLE = "bmc_unreachable"
    ALLOCATED_IDLE = "allocated_idle"
    LOW_USE_UNVERIFIED = "low_use_unverified"
    USER_ATTRIBUTION_UNVERIFIED = "user_attribution_unverified"
    HANDOVER_OVERDUE = "handover_overdue"


@dataclass(frozen=True)
class ServerScanServer:
    """
    The fields of one server-scan `GET /api/v1/servers/{id}` record that the engine uses.

    Attributes:
        id (str): server-scan's `id`.
        name (str): server-scan's `name` (the profile name for profiled servers).
        vendor (str): `identity.vendor` (`dell`, `cisco`, `hp`, `standalone`).
        serial_normalized (str): `identity.serial_normalized`; may be empty.
        system_uuid (str | None): `identity.system_uuid`.
        model (str | None): Server model.
        site_id (str | None): server-scan site.
        source_provider (str): Collector that owns the record, e.g. `UCS_CENTRAL`.
        gpu_models (tuple[str, ...] | None): One model per GPU ("" if unnamed); None when
            server-scan lists `hardware.gpus` in `unread_fields`.
        profile_template_name (str | None): `profile_template.name`.
        lifecycle_state (str | None): `openshift.lifecycle_state`.
        cluster_name (str | None): `openshift.cluster_name`.
        mce_name (str | None): `openshift.mce_name`.
        reported_name (str | None): `openshift.reported_name`.
        contested_with (str | None): `openshift.contested_with`.
        claim_changed_at (datetime | None): `openshift.claim_changed_at`.
        maintenance_enabled (bool): `maintenance.enabled`.
        maintenance_reason (str | None): `maintenance.reason`.
        maintenance_since (datetime | None): `maintenance.created_at`, when present.
        health_overall (str | None): `health.overall`.
        reachable (bool | None): `reachable`.
        unreachable_since (datetime | None): `unreachable_since`.
        last_seen_at (datetime | None): `last_seen_at`.
        created_at (datetime | None): `created_at` (server-scan's first sight).
        stale (bool): server-scan's own staleness flag.
    """

    id: str
    name: str
    vendor: str
    serial_normalized: str
    system_uuid: str | None
    model: str | None
    site_id: str | None
    source_provider: str
    gpu_models: tuple[str, ...] | None
    profile_template_name: str | None
    lifecycle_state: str | None
    cluster_name: str | None
    mce_name: str | None
    reported_name: str | None
    contested_with: str | None
    claim_changed_at: datetime | None
    maintenance_enabled: bool
    maintenance_reason: str | None
    maintenance_since: datetime | None
    health_overall: str | None
    reachable: bool | None
    unreachable_since: datetime | None
    last_seen_at: datetime | None
    created_at: datetime | None
    stale: bool


@dataclass(frozen=True)
class SourceStatus:
    """
    Health of one source as seen by the engine.

    Attributes:
        name (str): Source name, e.g. `serverscan` or a cluster name.
        failed_scans (int): Consecutive scans whose fetch failed or was partial.
        last_success_at (datetime | None): End of the last complete fetch.
    """

    name: str
    failed_scans: int
    last_success_at: datetime | None


@dataclass(frozen=True)
class ManagerServer:
    """
    One server as the profile-less collector saw it in UCS Central or OpenManage (ADR-0002 3-5).

    Attributes:
        vendor (str): `cisco` or `dell` (server-scan's `identity.vendor` values).
        serial_normalized (str): `identity.normalize_serial` of the hardware serial (Cisco
            `serial`, Dell `DeviceServiceTag`); "" when the manager has none.
        manager (str): Manager it came from, e.g. `ucs-central:<name>`, `openmanage:<name>`.
        source_id (str): The manager's id for it: the compute DN (Cisco), the device Id (Dell).
        name (str | None): Hardware name (DN, `DeviceName`).
        model (str | None): Server model.
        profile_name (str | None): Name of the profile bound to it in any state; None when no
            profile is bound (the simplified rule of 2026-10-30).
        gpu_models (tuple[str, ...] | None): One entry per GPU ("" if unnamed); () when the
            manager reports none; None when GPU inventory could not be read.
        domain (str | None): UCS domain name or id; OME group, when known.
        evidence (Mapping[str, Any]): Raw facts kept for `/explain` (profile DN or id, states,
            GPU source, domain freshness).
    """

    vendor: str
    serial_normalized: str
    manager: str
    source_id: str
    name: str | None
    model: str | None
    profile_name: str | None
    gpu_models: tuple[str, ...] | None
    domain: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectorRead:
    """
    One profile-less collector read of one manager.

    Attributes:
        manager (str): Manager name, as in `ManagerServer.manager`.
        kind (str): `ucs_central` or `openmanage`.
        started_at (datetime): Read start.
        finished_at (datetime): Read end.
        complete (bool): Every domain or device listing was read in full. An incomplete read
            never makes a server disappear (§4).
        servers (tuple[ManagerServer, ...]): Every server read, with or without GPUs.
        errors (tuple[str, ...]): Error summaries without secrets.
        coverage (Mapping[str, Any]): Counts: domains read/skipped, devices, GPU inventory unread.
    """

    manager: str
    kind: str
    started_at: datetime
    finished_at: datetime
    complete: bool
    servers: tuple[ManagerServer, ...]
    errors: tuple[str, ...] = ()
    coverage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetInput:
    """
    Everything the classifier knows about one asset in one snapshot.

    Attributes:
        asset_id (str): `<vendor>:<serial_normalized>`, or a fallback key (see identity.py).
        serverscan (ServerScanServer | None): The server-scan record, if any.
        conflict (str | None): Why the identity is ambiguous, if it is.
        collector (ManagerServer | None): The collector's record, for an asset server-scan does
            not have (rows 3-5).
        collector_first_seen (datetime | None): When the collector first saw it (a lower bound).
        collector_profile_since (datetime | None): When the collector first saw its current
            profile name bound (the handover clock of row 5).
    """

    asset_id: str
    serverscan: ServerScanServer | None
    conflict: str | None = None
    collector: ManagerServer | None = None
    collector_first_seen: datetime | None = None
    collector_profile_since: datetime | None = None


@dataclass(frozen=True)
class Bucket:
    """
    A group of an asset's GPUs that share one classification.

    Attributes:
        category (Category): Capacity category.
        reason (Reason): Matching rule.
        gpus (int): Physical GPUs in this bucket.
        gpu_model (str | None): GPU model; buckets never mix models.
        stage (Stage | None): Pipeline stage for IDLE buckets.
        stage_owner (str | None): Team owning the stage (from policy).
        flags (tuple[str, ...]): Flags; they never change the category.
        holder_ref (str | None): Holding logical workload, `namespace/Kind/name` of the pod's top
            owner (pod names go in evidence, so a pod restart keeps the interval open).
        pool (str | None): Node pool, when known.
        team (str | None): Consuming team for productive buckets.
        evidence (Mapping[str, Any]): Facts the rule matched on.
        cluster (str | None): Target cluster, once the node is known (not part of the key).
        node_uid (str | None): The node's UID in that cluster (not part of the key).
    """

    category: Category
    reason: Reason
    gpus: int
    gpu_model: str | None
    stage: Stage | None = None
    stage_owner: str | None = None
    flags: tuple[str, ...] = ()
    holder_ref: str | None = None
    pool: str | None = None
    team: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    cluster: str | None = None
    node_uid: str | None = None


@dataclass(frozen=True)
class StageClock:
    """
    Evidence for when the asset's current stage episode began.

    Attributes:
        since (datetime | None): Start time, or None to use the first observation.
        lower_bound (bool): True when the real start may be earlier.
        source (str): Which field the time came from.
    """

    since: datetime | None
    lower_bound: bool
    source: str


@dataclass(frozen=True)
class MilestoneFact:
    """
    A first-time lifecycle fact the classifier saw evidence for (§20.5 milestones).

    Attributes:
        name (str): `assigned_cluster`, `node_ready`, `gpu_ready`, `workload_running`,
            `first_genuine_request`.
        at (datetime | None): When, by evidence; None means "this scan".
        lower_bound (bool): True when it may have happened earlier.
        evidence (Mapping[str, Any]): Where the time came from.
    """

    name: str
    at: datetime | None
    lower_bound: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """
    The classifier's result for one asset.

    Attributes:
        asset_id (str): The asset.
        expected_gpus (int): `E`; equals the sum of bucket GPUs.
        buckets (tuple[Bucket, ...]): Non-empty unless the asset has no GPUs.
        trace (tuple[str, ...]): Each rule evaluated, in order, with its outcome.
        stage_clocks (Mapping[str, StageClock]): When each stage in the buckets began, by
            evidence, keyed by stage value; a stage without an entry has no evidence.
        policy_version (str): Policy the decision was made under.
        milestones (tuple[MilestoneFact, ...]): Lifecycle facts seen in this snapshot.
    """

    asset_id: str
    expected_gpus: int
    buckets: tuple[Bucket, ...]
    trace: tuple[str, ...]
    stage_clocks: Mapping[str, StageClock]
    policy_version: str
    milestones: tuple[MilestoneFact, ...] = ()


@dataclass(frozen=True)
class OwnerRef:
    """
    One link in a pod's ownership chain.

    Attributes:
        group (str): API group from the owner's `apiVersion` ("" for core, `apps`, `run.ai`).
        kind (str): Owner kind, e.g. `ReplicaSet`, `LLMInferenceService`.
        name (str): Owner name (in the pod's namespace, or cluster-scoped).
    """

    group: str
    kind: str
    name: str


@dataclass(frozen=True)
class ClusterNode:
    """
    One node of a target cluster, as the kube API reports it (§20.3).

    Attributes:
        name (str): `metadata.name`.
        uid (str): `metadata.uid`.
        ready (bool | None): `Ready` condition is True; None when the condition is absent.
        ready_changed_at (datetime | None): `Ready` condition `lastTransitionTime`.
        unschedulable (bool): `spec.unschedulable`.
        allocatable_gpus (int): `status.allocatable[<gpu resource>]`, 0 when absent.
        labels (Mapping[str, str]): Node labels (for pools).
    """

    name: str
    uid: str
    ready: bool | None
    ready_changed_at: datetime | None
    unschedulable: bool
    allocatable_gpus: int
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ClusterPod:
    """
    A pod bound to a node that holds whole GPUs or is a Run:ai fractional pod.

    Attributes:
        namespace (str): Namespace.
        name (str): Name.
        uid (str): UID.
        node_name (str): `spec.nodeName`.
        phase (str): `Pending` or `Running` (finished pods are not listed).
        ready (bool): `Ready` condition is True.
        started_at (datetime | None): `status.startTime`.
        gpus (int): Whole GPUs of the GPU resource as Kubernetes counts them: max(app containers
            + sidecar init containers, each regular init container + sidecars declared before
            it), from requests, else limits.
        fractional (bool): Carries a Run:ai fraction annotation (`gpu-fraction`, `gpu-memory`).
        gpu_group (str | None): Run:ai GPU group (label `runai-gpu-group`, or the id after the
            `runai-gpu-group/` label prefix for multi-device fractions).
        owners (tuple[OwnerRef, ...]): Controller chain, direct owner first, top owner last;
            empty for a bare pod.
        owners_complete (bool): False when a lookup failed before the top was reached.
        labels (Mapping[str, str]): Pod labels.
        images (tuple[str, ...]): Container images.
        waiting_reasons (tuple[str, ...]): Container waiting reasons, e.g. `CrashLoopBackOff`.
        restarts (int): Sum of container restart counts.
    """

    namespace: str
    name: str
    uid: str
    node_name: str
    phase: str
    ready: bool
    started_at: datetime | None
    gpus: int
    fractional: bool = False
    gpu_group: str | None = None
    owners: tuple[OwnerRef, ...] = ()
    owners_complete: bool = True
    labels: Mapping[str, str] = field(default_factory=dict)
    images: tuple[str, ...] = ()
    waiting_reasons: tuple[str, ...] = ()
    restarts: int = 0


@dataclass(frozen=True)
class KubeSnapshot:
    """
    One complete read of a target cluster's kube API.

    Attributes:
        cluster (str): Configured cluster name (equals server-scan `cluster_name`).
        cluster_id (str | None): `clusterversion/version` `spec.clusterID`.
        fetched_at (datetime): When the read finished.
        nodes (tuple[ClusterNode, ...]): Every node.
        pods (tuple[ClusterPod, ...]): Pods holding whole GPUs, and Run:ai fractional pods.
        api_groups (frozenset[str]): API groups the cluster serves (Run:ai presence, D5).
    """

    cluster: str
    cluster_id: str | None
    fetched_at: datetime
    nodes: tuple[ClusterNode, ...]
    pods: tuple[ClusterPod, ...]
    api_groups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ThanosSnapshot:
    """
    One complete read of a target cluster's Thanos querier.

    Attributes:
        cluster (str): Configured cluster name.
        fetched_at (datetime): When the read finished.
        gpu_uuids_by_node (Mapping[str, frozenset[str]]): Distinct DCGM GPU UUIDs per value of
            the DCGM node label; `R` is the size of the set (D1, D2).
        last_xid_by_node (Mapping[str, Mapping[str, float]]): Non-zero last XID per GPU UUID.
        activity_by_pod (Mapping[tuple[str, str], float]): `(namespace, pod)` → highest GPU
            activity over the `allocated_idle` window, as a 0-1 ratio.
        activity_metric (str | None): DCGM field the activity came from.
        counters (Mapping[str, Mapping[tuple[str, str], float]]): Serving counter selector →
            `(namespace, pod)` → `increase()` over the demand window. A selector with no
            series is absent; a selector whose series did not move maps pods to 0.0.
    """

    cluster: str
    fetched_at: datetime
    gpu_uuids_by_node: Mapping[str, frozenset[str]] = field(default_factory=dict)
    last_xid_by_node: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    activity_by_pod: Mapping[tuple[str, str], float] = field(default_factory=dict)
    activity_metric: str | None = None
    counters: Mapping[str, Mapping[tuple[str, str], float]] = field(default_factory=dict)
