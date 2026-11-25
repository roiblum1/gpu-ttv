"""One engine scan: fetch → identity → classify → ledger plan → apply (ADR-0001)."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from ttv.classify import SERVERSCAN_SOURCE, ClassifyContext, classify, gpu_count
from ttv.cluster_view import ClusterView, build_cluster_view, clean_hostname, holder_ref
from ttv.config import ClusterConnection, Policy
from ttv.demand import CounterHistory, PodKey, holder_demand
from ttv.gpu_rows import is_serving_pod
from ttv.identity import IdentityConflict, build_assets
from ttv.ledger import LedgerPlan, plan
from ttv.metrics import EngineMetrics
from ttv.models import (
    AssetInput,
    ClusterPod,
    CounterSample,
    Decision,
    Demand,
    KubeSnapshot,
    Reason,
    ServerScanServer,
    SourceStatus,
    ThanosSnapshot,
)
from ttv.pricing.catalog import canonical_gpu_model
from ttv.pricing.importer import PresentPricing
from ttv.sources.http import SourceError
from ttv.sources.pod_metrics import MetricsTarget, PodMetricsRead
from ttv.sources.serverscan import ServerScanError, ServerScanFetch
from ttv.store import AssetRow, LedgerState, Milestone, PostgresStore, WorkloadRow

log = structlog.get_logger(__name__)


class ServerScanFetcher(Protocol):
    """What the engine needs from the server-scan source."""

    async def fetch(self, now: datetime) -> ServerScanFetch:
        """
        Fetch the current server-scan view.

        Args:
            now (datetime): Scan time.

        Returns:
            ServerScanFetch: The fetch result.
        """
        ...


class KubeFetcher(Protocol):
    """What the engine needs from a cluster's kube API source."""

    async def fetch(self, now: datetime) -> KubeSnapshot:
        """
        Read the cluster.

        Args:
            now (datetime): Scan time.

        Returns:
            KubeSnapshot: A complete read.
        """
        ...

    async def aclose(self) -> None:
        """Release connections."""
        ...


class ThanosFetcher(Protocol):
    """What the engine needs from a cluster's Thanos source."""

    async def fetch(self, now: datetime) -> ThanosSnapshot:
        """
        Query the cluster's Thanos querier.

        Args:
            now (datetime): Scan time.

        Returns:
            ThanosSnapshot: A complete read.
        """
        ...

    async def aclose(self) -> None:
        """Release connections."""
        ...


class PodMetricsFetcher(Protocol):
    """What the engine needs from a cluster's vLLM pod-metrics source (ADR-0009)."""

    async def fetch(self, targets: Sequence[MetricsTarget]) -> PodMetricsRead:
        """
        Read each target pod's request counter through the kube API pod proxy.

        Args:
            targets (Sequence[MetricsTarget]): Pods and ports to read.

        Returns:
            PodMetricsRead: Values, per-pod failures, and whether access was denied.
        """
        ...

    async def aclose(self) -> None:
        """Release connections."""
        ...


@dataclass
class ClusterSources:
    """
    The read-only sources of one observed target cluster.

    Attributes:
        conn (ClusterConnection): Its configuration.
        kube (KubeFetcher): Kube API source.
        thanos (ThanosFetcher): Thanos source.
        pod_metrics (PodMetricsFetcher | None): vLLM pod-metrics source; None reads no counter.
        last_kube (KubeSnapshot | None): Last good kube read (kept through short outages).
        last_thanos (ThanosSnapshot | None): Last good Thanos read.
    """

    conn: ClusterConnection
    kube: KubeFetcher
    thanos: ThanosFetcher
    pod_metrics: PodMetricsFetcher | None = None
    last_kube: KubeSnapshot | None = None
    last_thanos: ThanosSnapshot | None = None


@dataclass
class ScanReport:
    """
    What one scan did, for logs, `/readyz` and `/explain`.

    Attributes:
        scan_id (int): Scan id (0 without a database).
        status (str): `ok`, `partial`, `failed` or `dry_run`.
        started_at (datetime): Scan time.
        finished_at (datetime): End time.
        decisions (dict[str, Decision]): Decisions by asset id.
        servers (dict[str, ServerScanServer]): server-scan record by asset id.
        coverage (dict[str, Any]): Counts of what was and was not classified.
        plan_counts (dict[str, int]): Opens, closes and refreshes planned.
        errors (list[str]): Error summaries.
    """

    scan_id: int
    status: str
    started_at: datetime
    finished_at: datetime
    decisions: dict[str, Decision] = field(default_factory=dict)
    servers: dict[str, ServerScanServer] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    plan_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Engine:
    """Runs scans; holds the last good server-scan fetch so a short outage changes nothing."""

    def __init__(
        self,
        *,
        policy: Policy,
        source: ServerScanFetcher,
        store: PostgresStore | None,
        metrics: EngineMetrics,
        dry_run: bool,
        unknown_grace_scans: int,
        tombstone_after: timedelta,
        clusters: Sequence[ClusterSources] = (),
        cluster_timeout: float = 120.0,
        pricing: PresentPricing | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """
        Configure the engine.

        Args:
            policy (Policy): Business policy.
            source (ServerScanFetcher): server-scan source.
            store (PostgresStore | None): Ledger; None classifies only (no database).
            metrics (EngineMetrics): Gauges to update.
            dry_run (bool): Classify and plan but never write the ledger.
            unknown_grace_scans (int): Failed scans before a source's assets turn UNKNOWN.
            tombstone_after (timedelta): Absence from complete listings before an asset is retired.
            clusters (Sequence[ClusterSources]): Observed target clusters.
            cluster_timeout (float): Deadline for each cluster source read, in seconds.
            pricing (PresentPricing | None): Present-day snapshot; None prices nothing.
            clock (Callable[[], datetime]): Time source (tests inject one).
        """
        self.policy = policy
        self.source = source
        self.store = store
        self.metrics = metrics
        self.dry_run = dry_run or store is None
        self.grace = unknown_grace_scans
        self.tombstone_after = tombstone_after
        self.clock = clock
        self.clusters = list(clusters)
        self.cluster_timeout = cluster_timeout
        self.pricing = pricing
        self.last_fetch: ServerScanFetch | None = None
        self.last_report: ScanReport | None = None
        self._failed: dict[str, int] = defaultdict(int)
        self.history = CounterHistory(policy.serving.demand_window + timedelta(hours=1))
        self._history_loaded = False

    async def run_once(self) -> ScanReport:
        """
        Run one complete scan.

        Returns:
            ScanReport: What the scan saw and did.
        """
        started = time.monotonic()
        now = self.clock()
        errors: list[str] = []
        store = self.store
        scan_id = await store.begin_scan("engine", now, dry_run=self.dry_run) if store else 0
        if store and not self._history_loaded:
            self.history.add(await store.load_counter_samples(now - self.history.keep))
            self._history_loaded = True

        (fetch, complete), views = await asyncio.gather(
            self._fetch(now, errors), self._read_clusters(now, errors)
        )
        status = await self._record_source(
            SERVERSCAN_SOURCE, complete, now, errors[-1] if errors else None
        )
        assets, conflicts = build_assets(fetch.servers if fetch else ())
        assets, node_conflicts = _node_claims(assets, set(views))
        conflicts = conflicts + node_conflicts
        ctx = ClassifyContext(
            policy=self.policy,
            sources={SERVERSCAN_SOURCE: status},
            unknown_grace_scans=self.grace,
            clusters=views,
            now=now,
        )
        pending_clusters = {
            name
            for name, v in views.items()
            if not v.unreachable and (v.kube is None or v.thanos is None)
        }

        decisions: dict[str, Decision] = {}
        servers: dict[str, ServerScanServer] = {}
        carry: set[str] = set()
        coverage: Counter[str] = Counter()
        for asset in assets:
            server = asset.serverscan
            assert server is not None
            servers[asset.asset_id] = server
            n = gpu_count(server)
            if (
                server.lifecycle_state == "INSTALLED"
                and server.cluster_name in pending_clusters
                and n
            ):
                coverage["carried_cluster_pending"] += 1
                carry.add(asset.asset_id)
            elif n is None:
                coverage["gpu_inventory_unread"] += 1
                carry.add(asset.asset_id)
            elif n == 0:
                coverage["no_gpus"] += 1
                decisions[asset.asset_id] = _empty(asset.asset_id, self.policy)
            else:
                coverage["gpu_assets"] += 1
                decisions[asset.asset_id] = classify(asset, ctx)
        pending = len(fetch.pending_detail_ids) if fetch else 0
        coverage["pending_detail"] = pending

        state = await store.load_state() if store else LedgerState([], {}, {})
        fresh_complete = complete and fetch is not None and fetch is self.last_fetch
        vanished = (
            self._vanished(state, set(decisions) | carry, now)
            if fresh_complete and pending == 0
            else {}
        )
        ledger_plan = plan(
            now=now,
            decisions=decisions,
            open_intervals=state.open_intervals,
            last_stage=state.last_stage,
            known_assets=set(state.last_observed),
            vanished=vanished,
        )
        if store and not self.dry_run:
            gpu_ids = {a for a, d in decisions.items() if d.buckets}
            await store.apply(
                scan_id,
                ledger_plan,
                assets=[_asset_row(a, servers[a]) for a in sorted(gpu_ids)],
                milestones=[
                    m
                    for a in sorted(gpu_ids)
                    for m in _milestones(a, servers[a], decisions[a], now)
                ],
                conflicts=conflicts,
                resolve_missing_conflicts=fresh_complete,
                now=now,
                gpu_aliases=_aliases(decisions),
                workloads=_workload_rows(decisions),
            )

        coverage["cluster_sources_failed"] = sum(
            1 for e in errors if e.startswith(("kube:", "thanos:"))
        )
        status_text = (
            "dry_run"
            if self.dry_run
            else ("ok" if complete and not errors and pending == 0 else "partial")
        )
        if fetch is None:
            status_text = "failed"
        finished = self.clock()
        report = ScanReport(
            scan_id=scan_id,
            status=status_text,
            started_at=now,
            finished_at=finished,
            decisions=decisions,
            servers=servers,
            coverage=dict(coverage) | {"conflicts": len(conflicts)},
            plan_counts=_counts(ledger_plan),
            errors=errors,
        )
        if store:
            await store.finish_scan(
                scan_id, status=status_text, now=finished, coverage=report.coverage, errors=errors
            )
        self._update_metrics(report, conflicts, complete, time.monotonic() - started, errors)
        self.last_report = report
        log.info(
            "scan.done",
            scan_id=scan_id,
            status=status_text,
            **report.plan_counts,
            **report.coverage,
        )
        return report

    async def _fetch(self, now: datetime, errors: list[str]) -> tuple[ServerScanFetch | None, bool]:
        """
        Fetch server-scan, falling back to the last good fetch on failure.

        Args:
            now (datetime): Scan time.
            errors (list[str]): Collects error summaries.

        Returns:
            tuple[ServerScanFetch | None, bool]: Fetch to classify; whether it was complete.
        """
        try:
            fetch = await self.source.fetch(now)
        except ServerScanError as exc:
            errors.append(f"serverscan: {exc}")
            log.warning("serverscan.fetch_failed", error=str(exc))
            return self.last_fetch, False
        self.last_fetch = fetch
        return fetch, fetch.listing_complete

    async def _record_source(
        self, name: str, ok: bool, now: datetime, error: str | None
    ) -> SourceStatus:
        """
        Track consecutive failures of one source (in Postgres, or in memory without one).

        Args:
            name (str): Source name, e.g. `serverscan` or `kube:<cluster>`.
            ok (bool): Whether this read was complete.
            now (datetime): Scan time.
            error (str | None): Error summary for a failed read.

        Returns:
            SourceStatus: Health after this scan.
        """
        if self.store:
            return await self.store.record_source(name, ok=ok, now=now, error=error)
        self._failed[name] = 0 if ok else self._failed[name] + 1
        return SourceStatus(name, self._failed[name], now if ok else None)

    async def _read_clusters(self, now: datetime, errors: list[str]) -> dict[str, ClusterView]:
        """
        Read every observed cluster's kube API and Thanos concurrently, each with a deadline.

        A failed read keeps the last good snapshot; the cluster turns unreachable only after
        `unknown_grace_scans` consecutive failures of either source (§20.6).

        Args:
            now (datetime): Scan time.
            errors (list[str]): Collects error summaries.

        Returns:
            dict[str, ClusterView]: One view per observed cluster.
        """
        jobs = [self._read(f"kube:{c.conn.name}", c.kube.fetch(now)) for c in self.clusters]
        jobs += [self._read(f"thanos:{c.conn.name}", c.thanos.fetch(now)) for c in self.clusters]
        results = await asyncio.gather(*jobs)
        kube_results, thanos_results = results[: len(self.clusters)], results[len(self.clusters) :]

        failed_by: dict[str, int] = {}
        fresh: dict[str, bool] = {}
        for c, (kube, kube_err), (thanos, thanos_err) in zip(
            self.clusters, kube_results, thanos_results, strict=True
        ):
            if isinstance(kube, KubeSnapshot):
                c.last_kube = kube
            if isinstance(thanos, ThanosSnapshot):
                c.last_thanos = thanos
            failed = 0
            for name, err in (
                (f"kube:{c.conn.name}", kube_err),
                (f"thanos:{c.conn.name}", thanos_err),
            ):
                if err:
                    errors.append(err)
                health = await self._record_source(name, err is None, now, err)
                failed = max(failed, health.failed_scans)
            failed_by[c.conn.name] = failed
            fresh[c.conn.name] = isinstance(kube, KubeSnapshot)

        pods = {c.conn.name: self._serving_pods(c) for c in self.clusters}
        reads = await asyncio.gather(
            *(
                self._read_pod_metrics(c, pods[c.conn.name][1] if fresh[c.conn.name] else [])
                for c in self.clusters
            )
        )
        views: dict[str, ClusterView] = {}
        for c, (values, error, attempted) in zip(self.clusters, reads, strict=True):
            if attempted:
                await self._record_source(f"vllm:{c.conn.name}", error is None, now, error)
            serving, counted = pods[c.conn.name]
            demand = await self._serving_demand(c, serving, counted, values, now)
            views[c.conn.name] = build_cluster_view(
                c.conn.name,
                c.last_kube,
                c.last_thanos,
                unreachable=failed_by[c.conn.name] >= self.grace,
                reservation_namespaces=c.conn.reservation_namespaces,
                demand=demand,
            )
        return views

    def _serving_pods(self, c: ClusterSources) -> tuple[list[ClusterPod], list[ClusterPod]]:
        """
        A cluster's GPU-holding pods of served models, and those whose counters are read.

        Args:
            c (ClusterSources): The cluster.

        Returns:
            tuple[list[ClusterPod], list[ClusterPod]]: All serving pods, and the Running ones
            outside `skip_roles` (prefill pods count each request again).
        """
        if c.last_kube is None:
            return [], []
        rules = self.policy.serving
        serving = [
            p
            for p in c.last_kube.pods
            if p.gpus > 0
            and p.namespace not in c.conn.reservation_namespaces
            and is_serving_pod(p, self.policy)
        ]
        counted = [
            p
            for p in serving
            if p.phase == "Running" and p.labels.get(rules.role_label) not in rules.skip_roles
        ]
        return serving, counted

    async def _read_pod_metrics(
        self, c: ClusterSources, pods: Sequence[ClusterPod]
    ) -> tuple[dict[PodKey, float], str | None, bool]:
        """
        Read the pods' counters within the cluster deadline; network only, no database.

        Args:
            c (ClusterSources): The cluster.
            pods (Sequence[ClusterPod]): Running serving pods to read.

        Returns:
            tuple[dict[PodKey, float], str | None, bool]: Counter values by `(namespace, pod)`
            (failed pods absent), an error for the `vllm:<cluster>` source, and whether a read
            was attempted.
        """
        if c.pod_metrics is None or not pods:
            return {}, None, False
        port = self.policy.serving.metrics_port
        targets = [MetricsTarget(p.namespace, p.name, port or p.metrics_port or 8000) for p in pods]
        name = f"vllm:{c.conn.name}"
        read, error = await self._read(name, c.pod_metrics.fetch(targets))
        values: dict[PodKey, float] = {}
        if isinstance(read, PodMetricsRead):
            values = dict(read.values)
            if read.permission_denied:
                error = f"{name}: permission denied; grant get on pods/proxy (gpu-cluster-rbac)"
            elif not values:
                error = f"{name}: no pod's counter could be read ({len(read.failed)} failed)"
        return values, error, True

    async def _serving_demand(
        self,
        c: ClusterSources,
        serving: Sequence[ClusterPod],
        counted: Sequence[ClusterPod],
        values: dict[PodKey, float],
        now: datetime,
    ) -> dict[str, Demand]:
        """
        Keep this scan's counter readings and work out each served model's demand (ADR-0009).

        Readings go to the in-memory history and, also in dry run, to Postgres. A counted pod
        with no reading this scan makes its deployment's windows incomplete.

        Args:
            c (ClusterSources): The cluster.
            serving (Sequence[ClusterPod]): Its serving pods.
            counted (Sequence[ClusterPod]): The ones whose counters were to be read.
            values (dict[PodKey, float]): Counter values read this scan.
            now (datetime): Scan time.

        Returns:
            dict[str, Demand]: Demand by `holder_ref`; a deployment with no reading is absent.
        """
        rules = self.policy.serving
        samples = [
            CounterSample(
                cluster=c.conn.name,
                namespace=p.namespace,
                pod=p.name,
                holder_ref=holder_ref(p),
                observed_at=now,
                value=values[(p.namespace, p.name)],
                pod_started_at=p.started_at,
                restarts=p.restarts,
            )
            for p in counted
            if (p.namespace, p.name) in values
        ]
        self.history.add(samples)
        self.history.prune(now)
        if self.store:
            await self.store.record_counter_samples(samples, prune_before=now - self.history.keep)

        pods_of: dict[str, list[PodKey]] = defaultdict(list)
        for p in counted:
            pods_of[holder_ref(p)].append((p.namespace, p.name))
        out: dict[str, Demand] = {}
        for ref in sorted({holder_ref(p) for p in serving}):
            keys = pods_of.get(ref, [])
            demand = holder_demand(
                self.history.for_holder(c.conn.name, ref),
                now=now,
                window=rules.demand_window,
                recent=rules.recent_window,
                read=[k for k in keys if k in values],
                unreadable=[k for k in keys if k not in values],
            )
            if demand is not None:
                out[ref] = demand
        return out

    async def _read(self, name: str, job: Any) -> tuple[Any, str | None]:
        """
        Await one cluster source read with the cluster deadline; never raises.

        Args:
            name (str): Source name.
            job (Any): The awaitable fetch.

        Returns:
            tuple[Any, str | None]: The snapshot, or None with an error summary.
        """
        try:
            return await asyncio.wait_for(job, timeout=self.cluster_timeout), None
        except TimeoutError:
            message = f"{name}: deadline of {self.cluster_timeout:.0f}s exceeded"
        except SourceError as exc:
            message = str(exc)
        except Exception as exc:  # one broken cluster source must not stop the scan
            log.exception("cluster.read_crashed", source=name)
            message = f"{name}: {type(exc).__name__}: {exc}"
        log.warning("cluster.read_failed", source=name, error=message)
        return None, message

    def _vanished(
        self, state: LedgerState, present: set[str], now: datetime
    ) -> dict[str, datetime]:
        """
        Assets missing from a complete listing for longer than the tombstone period.

        Args:
            state (LedgerState): Ledger state with last observations.
            present (set[str]): Assets in this listing.
            now (datetime): Scan time.

        Returns:
            dict[str, datetime]: Assets to retire, with their last observation time.
        """
        return {
            asset_id: last
            for asset_id, last in state.last_observed.items()
            if asset_id not in present and now - last > self.tombstone_after
        }

    def _update_metrics(
        self,
        report: ScanReport,
        conflicts: list[IdentityConflict],
        complete: bool,
        duration: float,
        errors: list[str],
    ) -> None:
        """
        Publish this scan's gauges.

        Args:
            report (ScanReport): The scan.
            conflicts (list[IdentityConflict]): Conflicts seen.
            complete (bool): Whether the server-scan fetch was complete.
            duration (float): Seconds taken.
            errors (list[str]): Error summaries, each prefixed with its source name.
        """
        failed = [] if complete else [(SERVERSCAN_SOURCE, "api")]
        for error in errors:
            source, _, rest = error.partition(":")
            if source in ("kube", "thanos"):
                failed.append((source, rest.split(":", 1)[0]))
        self.metrics.observe_decisions(report.decisions, report.servers)
        priced = self.pricing.priced if self.pricing else frozenset()
        self.metrics.observe_unpriced(
            report.decisions, lambda raw: canonical_gpu_model(raw) in priced
        )
        self.metrics.observe_scan(
            kind="engine",
            complete=complete and report.status in ("ok", "dry_run"),
            finished_at=report.finished_at.timestamp(),
            duration=duration,
            conflicts=len(conflicts),
            unread=report.coverage.get("gpu_inventory_unread", 0),
            pending=report.coverage.get("pending_detail", 0),
            dry_run=self.dry_run,
            failed_sources=failed,
        )


def _empty(asset_id: str, policy: Policy) -> Decision:
    """
    A decision with no GPUs, which closes any intervals the asset still has open.

    Args:
        asset_id (str): Asset id.
        policy (Policy): Active policy.

    Returns:
        Decision: An empty decision.
    """
    return Decision(
        asset_id,
        0,
        (),
        ("no GPUs in inventory",),
        {},
        policy.policy_version,
    )


def _aliases(decisions: dict[str, Decision]) -> dict[str, str | None]:
    """
    Canonical names of the raw GPU models in this scan's buckets, for `gpu_model_aliases`.

    Args:
        decisions (dict[str, Decision]): Decisions by asset id.

    Returns:
        dict[str, str | None]: Raw model → canonical name (None: unidentified).
    """
    raw = {b.gpu_model for d in decisions.values() for b in d.buckets if b.gpu_model}
    return {model: canonical_gpu_model(model) for model in raw}


def _asset_row(asset_id: str, s: ServerScanServer) -> AssetRow:
    """
    Describe an asset from its server-scan record.

    Args:
        asset_id (str): Asset id.
        s (ServerScanServer): Its record.

    Returns:
        AssetRow: The row to upsert.
    """
    models = Counter(s.gpu_models or ())
    return AssetRow(
        asset_id=asset_id,
        vendor=s.vendor,
        serial=s.serial_normalized,
        system_uuid=s.system_uuid,
        name=s.name,
        model=s.model,
        site=s.site_id,
        gpu_model=models.most_common(1)[0][0] if models else None,
        gpu_count=None if s.gpu_models is None else len(s.gpu_models),
        source=s.source_provider,
        aliases={"serverscan_id": s.id, "serverscan_name": s.name},
    )


def _node_claims(
    assets: list[AssetInput], observed: set[str]
) -> tuple[list[AssetInput], list[IdentityConflict]]:
    """
    Mark assets that claim the same node of an observed cluster as conflicting (§5).

    Args:
        assets (list[AssetInput]): Assets from server-scan.
        observed (set[str]): Observed cluster names.

    Returns:
        tuple[list[AssetInput], list[IdentityConflict]]: Assets (claimants marked) and the
        `NODE_CLAIM` conflicts.
    """
    claims: dict[str, list[str]] = defaultdict(list)
    for asset in assets:
        s = asset.serverscan
        if s and s.lifecycle_state == "INSTALLED" and s.cluster_name in observed:
            name = clean_hostname(s.reported_name or s.name)
            if name:
                claims[f"{s.cluster_name}/{name}"].append(asset.asset_id)
    shared = {key: ids for key, ids in claims.items() if len(ids) > 1}
    if not shared:
        return assets, []
    by_asset = {a: key for key, ids in shared.items() for a in ids}
    marked = [
        dataclasses.replace(
            a,
            conflict=a.conflict
            or f"node {by_asset[a.asset_id]} also claimed by "
            + ", ".join(i for i in shared[by_asset[a.asset_id]] if i != a.asset_id),
        )
        if a.asset_id in by_asset
        else a
        for a in assets
    ]
    conflicts = [IdentityConflict("NODE_CLAIM", k, tuple(sorted(v))) for k, v in shared.items()]
    return marked, conflicts


def _milestones(
    asset_id: str, s: ServerScanServer, decision: Decision, now: datetime
) -> list[Milestone]:
    """
    First-time facts from server-scan and from this scan's classification.

    Args:
        asset_id (str): Asset id.
        s (ServerScanServer): Its server-scan record.
        decision (Decision): Its decision (carries cluster milestones).
        now (datetime): Scan time.

    Returns:
        list[Milestone]: `first_seen`, `in_serverscan` when known, and the decision's facts.
    """
    result = [
        Milestone(asset_id, "first_seen", now, lower_bound=True, evidence={"source": "ttv-engine"})
    ]
    if s.created_at is not None:
        result.append(
            Milestone(
                asset_id,
                "in_serverscan",
                s.created_at,
                lower_bound=False,
                evidence={"source": "serverscan.created_at"},
            )
        )
    for fact in decision.milestones:
        at = fact.at if fact.at is not None and fact.at <= now else now
        result.append(Milestone(asset_id, fact.name, at, fact.lower_bound, dict(fact.evidence)))
    return result


def _counts(ledger_plan: LedgerPlan) -> dict[str, int]:
    """
    Summarise a plan for logs.

    Args:
        ledger_plan (LedgerPlan): The plan.

    Returns:
        dict[str, int]: Opens, closes and refreshes.
    """
    return {
        "opens": len(ledger_plan.opens),
        "closes": len(ledger_plan.closes),
        "refreshes": len(ledger_plan.refreshes),
    }


def _workload_rows(decisions: dict[str, Decision]) -> list[WorkloadRow]:
    """
    Every GPU-holding workload in this scan's buckets, for `workloads_now` (ADR-0009).

    Args:
        decisions (dict[str, Decision]): Decisions by asset id.

    Returns:
        list[WorkloadRow]: One row per (cluster, workload, reason), GPUs summed over nodes.
    """
    rows: dict[tuple[str, str, str], WorkloadRow] = {}
    for d in decisions.values():
        for b in d.buckets:
            if b.holder_ref is None or b.cluster is None or b.reason is Reason.NO_WORKLOAD:
                continue
            e = b.evidence
            key = (b.cluster, b.holder_ref, str(b.reason))
            since = _parse_time(e.get("started_at"))
            busy = e.get("gpu_busy_peak")
            row = rows.get(key)
            if row is None:
                kind = "unresolved" if b.reason is Reason.HOLDER_UNRESOLVED else e.get("kind")
                rows[key] = WorkloadRow(
                    cluster=b.cluster,
                    holder_ref=b.holder_ref,
                    reason=str(b.reason),
                    category=str(b.category),
                    kind=kind if kind in ("serving", "ml") else "unresolved",
                    runai=bool(e.get("runai")),
                    stage=str(b.stage) if b.stage else None,
                    stage_owner=b.stage_owner,
                    gpus=b.gpus,
                    gpu_models=(b.gpu_model,) if b.gpu_model else (),
                    requests_window=e.get("requests"),
                    window_hours=e.get("covered_hours", e.get("window_hours")),
                    window_complete=e.get("complete"),
                    requests_recent=e.get("recent_requests"),
                    recent_hours=e.get("recent_hours"),
                    recent_complete=e.get("recent_complete"),
                    requests_per_gpu_hour=e.get("requests_per_gpu_hour"),
                    gpu_busy_peak=busy,
                    busy_window_hours=e.get("busy_window_hours"),
                    gpu_power_peak=e.get("gpu_power_peak"),
                    gpu_power_avg=e.get("gpu_power_avg"),
                    since=since,
                )
                continue
            models = row.gpu_models
            if b.gpu_model and b.gpu_model not in models:
                models = (*models, b.gpu_model)
            peaks = [x for x in (row.gpu_busy_peak, busy) if x is not None]
            powers = [x for x in (row.gpu_power_peak, e.get("gpu_power_peak")) if x is not None]
            starts = [x for x in (row.since, since) if x is not None]
            rows[key] = dataclasses.replace(
                row,
                gpus=row.gpus + b.gpus,
                gpu_models=models,
                gpu_busy_peak=max(peaks) if peaks else None,
                gpu_power_peak=max(powers) if powers else None,
                since=min(starts) if starts else None,
            )
    return [rows[k] for k in sorted(rows)]


def _parse_time(value: Any) -> datetime | None:
    """
    An ISO timestamp from evidence, or None.

    Args:
        value (Any): The evidence value.

    Returns:
        datetime | None: The time.
    """
    return datetime.fromisoformat(value) if isinstance(value, str) else None
