"""Pure classification: one asset snapshot in, GPU buckets out (ADR-0002). No I/O here."""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ttv.cluster_view import ClusterView, clean_hostname
from ttv.config import Policy
from ttv.gpu_rows import Slice, gpu_level
from ttv.models import (
    AssetInput,
    Bucket,
    Category,
    ClusterNode,
    Decision,
    Flag,
    ManagerServer,
    MilestoneFact,
    Reason,
    ServerScanServer,
    SourceStatus,
    Stage,
    StageClock,
)

SERVERSCAN_SOURCE = "serverscan"
COLLECTOR_SOURCE_PREFIX = "collector:"
_NO_CLOCK = StageClock(since=None, lower_bound=True, source="none")


@dataclass(frozen=True)
class ClassifyContext:
    """
    Everything outside the asset that a rule may consult.

    Attributes:
        policy (Policy): Owners, objectives, target clusters, exclusions.
        sources (Mapping[str, SourceStatus]): Health of each source by name.
        unknown_grace_scans (int): Consecutive failed scans before a source's assets turn UNKNOWN.
        clusters (Mapping[str, ClusterView]): Observed target clusters by name.
        now (datetime | None): Scan time; None uses each cluster's kube read time.
    """

    policy: Policy
    sources: Mapping[str, SourceStatus] = field(default_factory=dict)
    unknown_grace_scans: int = 2
    clusters: Mapping[str, ClusterView] = field(default_factory=dict)
    now: datetime | None = None


def gpu_count(server: ServerScanServer) -> int | None:
    """
    Return the number of GPUs server-scan reports, or None when GPU inventory is unread.

    Args:
        server (ServerScanServer): The record.

    Returns:
        int | None: GPU count; None means unknown, never zero.
    """
    return None if server.gpu_models is None else len(server.gpu_models)


def classify(asset: AssetInput, ctx: ClassifyContext) -> Decision:
    """
    Classify every GPU of one asset; the first matching rule wins.

    Args:
        asset (AssetInput): The asset; it must carry a server-scan record with at least one GPU.
        ctx (ClassifyContext): Policy and source health.

    Returns:
        Decision: Buckets whose GPU counts sum to the asset's GPU count, with the rule trace.

    Raises:
        ValueError: If the asset has no GPUs, or neither a server-scan nor a collector record.
    """
    server = asset.serverscan
    if server is None and asset.collector is not None and asset.collector.gpu_models:
        return _CollectorRules(asset, asset.collector, ctx).decide()
    if server is None or not server.gpu_models:
        raise ValueError(f"{asset.asset_id}: classify() needs a server-scan record with GPUs")
    return _Rules(asset, server, ctx).decide()


class _Rules:
    """One evaluation of ADR-0002's rule table for a single asset."""

    def __init__(self, asset: AssetInput, server: ServerScanServer, ctx: ClassifyContext) -> None:
        """
        Prepare the evaluation.

        Args:
            asset (AssetInput): The asset.
            server (ServerScanServer): Its server-scan record.
            ctx (ClassifyContext): Policy and source health.
        """
        self.asset = asset
        self.s = server
        self.ctx = ctx
        self.policy = ctx.policy
        self.trace: list[str] = []
        self.flags: list[str] = []
        self.critical = False
        self.bmc_down = False
        self.milestones: list[MilestoneFact] = []
        self.cluster: str | None = None
        self.node_uid: str | None = None
        self.pool: str | None = None
        self.models: tuple[str, ...] = server.gpu_models or ()

    def decide(self) -> Decision:
        """
        Walk the server-level rows in order and return the first match.

        Returns:
            Decision: The classification.
        """
        for rule in (
            self._excluded,
            self._source_stale,
            self._identity_conflict,
            self._maintenance,
            self._hardware,
            self._contested,
            self._membership,
        ):
            decision = rule()
            if decision is not None:
                return decision
        raise AssertionError("_membership always matches")  # pragma: no cover

    def _excluded(self) -> Decision | None:
        """
        Row 1: deliberately excluded assets are not charged.

        Returns:
            Decision | None: A match, or None.
        """
        excl = self.policy.exclusions
        hit = self.asset.asset_id in excl.asset_ids or self.s.name in excl.serverscan_names
        if self._note("1 EXCLUDED", hit):
            return self._server_level(Category.NOT_CHARGED, Reason.EXCLUDED)
        return None

    def _source_stale(self) -> Decision | None:
        """
        Row 2: a failing source or a record server-scan itself marks stale is UNKNOWN.

        Returns:
            Decision | None: A match, or None.
        """
        status = self.ctx.sources.get(SERVERSCAN_SOURCE)
        failing = status is not None and status.failed_scans >= self.ctx.unknown_grace_scans
        if self._note("2 SOURCE_STALE", failing or self.s.stale):
            evidence = {
                "record_stale": self.s.stale,
                "source_failed_scans": status.failed_scans if status else 0,
            }
            return self._server_level(Category.UNKNOWN, Reason.SOURCE_STALE, evidence=evidence)
        return None

    def _identity_conflict(self) -> Decision | None:
        """
        Row 2b: an ambiguous identity is UNKNOWN until resolved.

        Returns:
            Decision | None: A match, or None.
        """
        if self._note("2b IDENTITY_CONFLICT", self.asset.conflict is not None):
            evidence = {"conflict": self.asset.conflict}
            return self._server_level(Category.UNKNOWN, Reason.IDENTITY_CONFLICT, evidence=evidence)
        return None

    def _maintenance(self) -> Decision | None:
        """
        Row 6a: maintenance in server-scan is DC's time.

        Returns:
            Decision | None: A match, or None.
        """
        if self._note("6a MAINTENANCE", self.s.maintenance_enabled):
            clock = _clock(self.s.maintenance_since, "maintenance.created_at")
            evidence = {"maintenance_reason": self.s.maintenance_reason}
            return self._server_level(
                Category.IDLE, Reason.MAINTENANCE, Stage.PRE_PROVISIONING, clock, evidence
            )
        return None

    def _hardware(self) -> Decision | None:
        """
        Row 6b: critical health or an unreachable BMC is DC's time unless the server is INSTALLED.

        Returns:
            Decision | None: A match, or None.
        """
        critical = self.s.health_overall in self.policy.critical_health_values
        bmc_down = self.s.reachable is False
        self.critical, self.bmc_down = critical, bmc_down
        if critical:
            self.flags.append(Flag.HARDWARE_CRITICAL)
        if bmc_down:
            self.flags.append(Flag.BMC_UNREACHABLE)
        installed = self.s.lifecycle_state == "INSTALLED"
        if not self._note(
            "6b HARDWARE_CRITICAL/BMC_UNREACHABLE", (critical or bmc_down) and not installed
        ):
            return None
        evidence = {"health_overall": self.s.health_overall, "reachable": self.s.reachable}
        if critical:
            return self._server_level(
                Category.IDLE, Reason.HARDWARE_CRITICAL, Stage.PRE_PROVISIONING, _NO_CLOCK, evidence
            )
        clock = _clock(self.s.unreachable_since, "unreachable_since")
        return self._server_level(
            Category.IDLE, Reason.BMC_UNREACHABLE, Stage.PRE_PROVISIONING, clock, evidence
        )

    def _contested(self) -> Decision | None:
        """
        Row 7: a contested cluster claim is UNKNOWN.

        Returns:
            Decision | None: A match, or None.
        """
        if self._note("7 CONTESTED", bool(self.s.contested_with)):
            evidence = {"contested_with": self.s.contested_with}
            return self._server_level(Category.UNKNOWN, Reason.CONTESTED, evidence=evidence)
        return None

    def _membership(self) -> Decision:
        """
        Rows 8-11: OpenShift membership decides Redbull's stage, exclusions and observation.

        Returns:
            Decision: Always a match (unknown membership is UNKNOWN).
        """
        state, cluster = self.s.lifecycle_state, self.s.cluster_name
        evidence = {"lifecycle_state": state, "cluster_name": cluster, "mce_name": self.s.mce_name}
        if self._note("8 NOT_IN_CLUSTER", state == "AVAILABLE"):
            return self._server_level(
                Category.IDLE,
                Reason.NOT_IN_CLUSTER,
                Stage.PROVISIONING,
                self._redbull_clock(),
                evidence,
            )
        if self._note("9 IN_MCE_INVENTORY", state == "INSTALLED_TO_INVENTORY"):
            return self._server_level(
                Category.IDLE,
                Reason.IN_MCE_INVENTORY,
                Stage.PROVISIONING,
                self._redbull_clock(),
                evidence,
            )
        if self._note(
            "10 OUTSIDE_TARGET_CLUSTERS",
            state == "INSTALLED" and cluster not in self.policy.target_clusters,
        ):
            return self._server_level(
                Category.NOT_CHARGED, Reason.OUTSIDE_TARGET_CLUSTERS, evidence=evidence
            )
        if self._note("10b MEMBERSHIP_UNKNOWN", state != "INSTALLED"):
            return self._server_level(
                Category.UNKNOWN, Reason.MEMBERSHIP_UNKNOWN, evidence=evidence
            )
        self.milestones.append(
            MilestoneFact(
                "assigned_cluster",
                self.s.claim_changed_at,
                self.s.claim_changed_at is None,
                {"cluster": cluster},
            )
        )
        view = self.ctx.clusters.get(cluster or "")
        if self._note("11 CLUSTER_NOT_OBSERVED", view is None):
            return self._server_level(
                Category.UNKNOWN, Reason.CLUSTER_NOT_OBSERVED, evidence=evidence
            )
        assert view is not None
        down = view.unreachable or view.kube is None or view.thanos is None
        if self._note("11 CLUSTER_UNREACHABLE", down):
            return self._server_level(
                Category.UNKNOWN, Reason.CLUSTER_UNREACHABLE, evidence=evidence
            )
        return self._cluster(view)

    def _cluster(self, view: ClusterView) -> Decision:
        """
        Rows 12-14 on the asset's node, then the GPU-level rows 15-22.

        Args:
            view (ClusterView): The asset's target cluster (kube and Thanos read).

        Returns:
            Decision: Always a match.
        """
        wanted = clean_hostname(self.s.reported_name or self.s.name)
        nodes = view.nodes_by_clean.get(wanted or "", ())
        lookup = {"cluster": view.name, "node_name": wanted}
        if self._note("12 NODE_NOT_FOUND", not nodes):
            return self._server_level(Category.UNKNOWN, Reason.NODE_NOT_FOUND, evidence=lookup)
        if self._note("12 node name ambiguous (IDENTITY_CONFLICT)", len(nodes) > 1):
            lookup["candidates"] = sorted(n.uid for n in nodes)
            return self._server_level(Category.UNKNOWN, Reason.IDENTITY_CONFLICT, evidence=lookup)
        node = nodes[0]
        self.cluster, self.node_uid = view.name, node.uid
        self.pool = _pool(node, self.policy)
        evidence: dict[str, Any] = {
            "cluster": view.name,
            "node": node.name,
            "ready": node.ready,
            "unschedulable": node.unschedulable,
        }
        if node.ready:
            self.milestones.append(
                MilestoneFact("node_ready", node.ready_changed_at, True, {"node": node.name})
            )
        else:
            return self._node_not_ready(node, evidence)
        if self._note("13 NODE_UNSCHEDULABLE", node.unschedulable):
            return self._server_level(
                Category.IDLE, Reason.NODE_UNSCHEDULABLE, Stage.PROVISIONING, evidence=evidence
            )
        reporting = len(view.gpu_uuids(node))
        evidence |= {"A": node.allocatable_gpus, "R": reporting}
        if self._note("14 GPU_PLATFORM_NOT_READY", node.allocatable_gpus == 0 or reporting == 0):
            return self._server_level(
                Category.IDLE, Reason.GPU_PLATFORM_NOT_READY, Stage.GPU_PLATFORM, evidence=evidence
            )
        assert view.kube is not None
        rows = gpu_level(
            expected=len(self.models),
            node=node,
            view=view,
            policy=self.policy,
            now=self.ctx.now or view.kube.fetched_at,
            base_flags=self.flags,
        )
        self.trace.extend(rows.trace)
        self.milestones.extend(rows.milestones)
        return self._from_slices(rows.slices)

    def _node_not_ready(self, node: ClusterNode, evidence: dict[str, Any]) -> Decision:
        """
        Row 13 NODE_NOT_READY, or row 6b when the hardware is also critical (C3).

        Args:
            node (ClusterNode): The not-Ready node.
            evidence (dict[str, Any]): Node evidence.

        Returns:
            Decision: DC's time when the hardware explains it, else Redbull's.
        """
        hardware = self.critical or self.bmc_down
        evidence |= {"health_overall": self.s.health_overall, "reachable": self.s.reachable}
        if self._note("13 NODE_NOT_READY + 6b hardware", hardware):
            reason = Reason.HARDWARE_CRITICAL if self.critical else Reason.BMC_UNREACHABLE
            return self._server_level(
                Category.IDLE, reason, Stage.PRE_PROVISIONING, _NO_CLOCK, evidence
            )
        self._note("13 NODE_NOT_READY", True)
        clock = StageClock(node.ready_changed_at, lower_bound=True, source="node.ready")
        if node.ready_changed_at is None:
            clock = _NO_CLOCK
        return self._server_level(
            Category.IDLE, Reason.NODE_NOT_READY, Stage.PROVISIONING, clock, evidence
        )

    def _redbull_clock(self) -> StageClock:
        """
        Evidence for when Redbull's stage began: the last OpenShift claim change, else first sight.

        Returns:
            StageClock: `claim_changed_at` as reported, else `created_at` as a lower bound.
        """
        if self.s.claim_changed_at is not None:
            return StageClock(
                self.s.claim_changed_at, lower_bound=False, source="openshift.claim_changed_at"
            )
        return _clock(self.s.created_at, "created_at")

    def _note(self, row: str, matched: bool) -> bool:
        """
        Record a rule's outcome in the trace.

        Args:
            row (str): Row label from ADR-0002.
            matched (bool): Whether the rule matched.

        Returns:
            bool: `matched`, unchanged.
        """
        self.trace.append(f"{row}: {'MATCH' if matched else 'no'}")
        return matched

    def _server_level(
        self,
        category: Category,
        reason: Reason,
        stage: Stage | None = None,
        clock: StageClock = _NO_CLOCK,
        evidence: Mapping[str, Any] | None = None,
    ) -> Decision:
        """
        Assign all of the asset's GPUs to one category, one bucket per GPU model.

        Args:
            category (Category): Category for every GPU.
            reason (Reason): Matching rule.
            stage (Stage | None): Stage, for IDLE buckets.
            clock (StageClock): Evidence for the stage start.
            evidence (Mapping[str, Any] | None): Facts the rule matched on.

        Returns:
            Decision: The decision.
        """
        models = self.models
        slice_ = Slice(
            category,
            reason,
            stage,
            len(models),
            None,
            None,
            tuple(self.flags),
            dict(evidence or {}),
        )
        clocks = {str(stage): clock} if stage is not None and clock.since is not None else {}
        return self._from_slices((slice_,), clocks)

    def _from_slices(
        self, slices: Sequence[Slice], clocks: Mapping[str, StageClock] | None = None
    ) -> Decision:
        """
        Turn slices into per-model buckets: GPU models are dealt to slices in order.

        Args:
            slices (Sequence[Slice]): Slices in row order; their GPUs sum to `E`.
            clocks (Mapping[str, StageClock] | None): Stage-start evidence by stage.

        Returns:
            Decision: The decision.
        """
        models = sorted(m or "" for m in self.models)
        buckets: list[Bucket] = []
        cursor = 0
        for item in slices:
            dealt = Counter(models[cursor : cursor + item.gpus])
            cursor += item.gpus
            for model, count in sorted(dealt.items()):
                buckets.append(
                    Bucket(
                        category=item.category,
                        reason=item.reason,
                        gpus=count,
                        gpu_model=model or None,
                        stage=item.stage,
                        stage_owner=self.policy.teams[item.stage] if item.stage else None,
                        flags=item.flags,
                        holder_ref=item.holder_ref,
                        pool=self.pool,
                        team=item.team,
                        evidence=dict(item.evidence),
                        cluster=self.cluster,
                        node_uid=self.node_uid,
                    )
                )
        return Decision(
            asset_id=self.asset.asset_id,
            expected_gpus=len(models),
            buckets=_merge(buckets),
            trace=tuple(self.trace),
            stage_clocks=dict(clocks or {}),
            policy_version=self.policy.policy_version,
            milestones=tuple(self.milestones),
        )


class _CollectorRules(_Rules):
    """Rows 1-5 for a GPU server only the profile-less collector knows (ADR-0002, ADR-0004)."""

    def __init__(self, asset: AssetInput, record: ManagerServer, ctx: ClassifyContext) -> None:
        """
        Prepare the evaluation.

        Args:
            asset (AssetInput): The asset.
            record (ManagerServer): Its collector record.
            ctx (ClassifyContext): Policy and source health.
        """
        self.asset = asset
        self.m = record
        self.ctx = ctx
        self.policy = ctx.policy
        self.trace = []
        self.flags = []
        self.critical = False
        self.bmc_down = False
        self.milestones = []
        self.cluster = None
        self.node_uid = None
        self.pool = None
        self.models = record.gpu_models or ()

    def decide(self) -> Decision:
        """
        Walk rows 1, 2, 2b, 3, 4 and 5 in order; row 5 always matches what is left.

        Returns:
            Decision: The classification.
        """
        m, rules = self.m, self.policy.collector
        evidence: dict[str, Any] = {
            "manager": m.manager,
            "source_id": m.source_id,
            "name": m.name,
            "profile_name": m.profile_name,
            "domain": m.domain,
        }
        if self._note("1 EXCLUDED", self.asset.asset_id in self.policy.exclusions.asset_ids):
            return self._server_level(Category.NOT_CHARGED, Reason.EXCLUDED, evidence=evidence)
        status = self.ctx.sources.get(COLLECTOR_SOURCE_PREFIX + m.manager)
        failing = status is None or status.failed_scans >= self.ctx.unknown_grace_scans
        if self._note("2 SOURCE_STALE (collector)", failing):
            evidence["source_failed_scans"] = status.failed_scans if status else None
            return self._server_level(Category.UNKNOWN, Reason.SOURCE_STALE, evidence=evidence)
        if self._note("2b IDENTITY_CONFLICT", self.asset.conflict is not None):
            evidence["conflict"] = self.asset.conflict
            return self._server_level(Category.UNKNOWN, Reason.IDENTITY_CONFLICT, evidence=evidence)
        seen = self.asset.collector_first_seen
        if self._note("3 PROFILE_NONE", m.profile_name is None):
            clock = _NO_CLOCK if seen is None else StageClock(seen, True, "collector.first_seen")
            return self._server_level(
                Category.IDLE, Reason.PROFILE_NONE, Stage.PRE_PROVISIONING, clock, evidence
            )
        ocp = re.search(rules.ocp_profile_pattern, m.profile_name or "") is not None
        if self._note("4 OTHER_INTENDED_USE", not ocp):
            return self._server_level(
                Category.NOT_CHARGED, Reason.OTHER_INTENDED_USE, evidence=evidence
            )
        self._note("5 HANDOVER_PENDING", True)
        since = self.asset.collector_profile_since or seen
        now = self.ctx.now
        overdue = since is not None and now is not None and now - since > rules.handover_grace
        if overdue:
            self.flags.append(Flag.HANDOVER_OVERDUE)
        evidence |= {"ocp_profile_since": since.isoformat() if since else None}
        self.milestones.append(
            MilestoneFact("ocp_profile_seen", since, True, {"profile": m.profile_name})
        )
        return self._server_level(Category.UNKNOWN, Reason.HANDOVER_PENDING, evidence=evidence)


def _pool(node: ClusterNode, policy: Policy) -> str | None:
    """
    The first configured pool whose node selector the node satisfies.

    Args:
        node (ClusterNode): The node.
        policy (Policy): Policy with `pools`.

    Returns:
        str | None: Pool name, or None.
    """
    for pool in policy.pools:
        if all(node.labels.get(k) == v for k, v in pool.node_selector.items()):
            return pool.name
    return None


def _merge(buckets: Sequence[Bucket]) -> tuple[Bucket, ...]:
    """
    Merge buckets that share an interval key, so no key appears twice for one asset.

    Args:
        buckets (Sequence[Bucket]): Buckets in order.

    Returns:
        tuple[Bucket, ...]: Buckets with unique keys, GPUs summed, first evidence kept.
    """
    merged: dict[tuple[Any, ...], Bucket] = {}
    for b in buckets:
        key = (b.category, b.reason, b.stage_owner, b.holder_ref, b.pool, b.team, b.gpu_model)
        if key in merged:
            first = merged[key]
            merged[key] = dataclasses.replace(first, gpus=first.gpus + b.gpus)
        else:
            merged[key] = b
    return tuple(merged.values())


def _clock(since: Any, source: str) -> StageClock:
    """
    Build a stage clock from a server-scan timestamp that may only bound the real start.

    Args:
        since (Any): The timestamp, or None.
        source (str): Field it came from.

    Returns:
        StageClock: Exact for state-change timestamps, a lower bound for `created_at`.
    """
    if since is None:
        return _NO_CLOCK
    return StageClock(since=since, lower_bound=source == "created_at", source=source)
