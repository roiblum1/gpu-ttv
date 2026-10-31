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

from ttv.classify import (
    COLLECTOR_SOURCE_PREFIX,
    SERVERSCAN_SOURCE,
    ClassifyContext,
    classify,
    gpu_count,
)
from ttv.cluster_view import ClusterView, build_cluster_view, clean_hostname
from ttv.config import ClusterConnection, Policy
from ttv.identity import IdentityConflict, build_assets
from ttv.ledger import LedgerPlan, plan
from ttv.metrics import EngineMetrics
from ttv.models import (
    AssetInput,
    Decision,
    KubeSnapshot,
    ManagerServer,
    ServerScanServer,
    SourceStatus,
    ThanosSnapshot,
)
from ttv.pricing.catalog import canonical_gpu_model
from ttv.pricing.importer import PresentPricing
from ttv.sources.http import SourceError
from ttv.sources.serverscan import ServerScanError, ServerScanFetch
from ttv.store import AssetRow, CollectorState, LedgerState, Milestone, PostgresStore

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


@dataclass
class ClusterSources:
    """
    The two read-only sources of one observed target cluster.

    Attributes:
        conn (ClusterConnection): Its configuration.
        kube (KubeFetcher): Kube API source.
        thanos (ThanosFetcher): Thanos source.
        last_kube (KubeSnapshot | None): Last good kube read (kept through short outages).
        last_thanos (ThanosSnapshot | None): Last good Thanos read.
    """

    conn: ClusterConnection
    kube: KubeFetcher
    thanos: ThanosFetcher
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
        collector (dict[str, ManagerServer]): Collector record of collector-only assets.
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
    collector: dict[str, ManagerServer] = field(default_factory=dict)
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
        collector_stale_after: timedelta = timedelta(hours=1),
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
            collector_stale_after (timedelta): Age after which a manager's latest complete
                collector read no longer counts, so its collector-only assets turn UNKNOWN.
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
        self.collector_stale_after = collector_stale_after
        self.pricing = pricing
        self.last_fetch: ServerScanFetch | None = None
        self.last_report: ScanReport | None = None
        self._failed: dict[str, int] = defaultdict(int)

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

        (fetch, complete), views = await asyncio.gather(
            self._fetch(now, errors), self._read_clusters(now, errors)
        )
        status = await self._record_source(
            SERVERSCAN_SOURCE, complete, now, errors[-1] if errors else None
        )
        assets, conflicts = build_assets(fetch.servers if fetch else ())
        assets, node_conflicts = _node_claims(assets, set(views))
        collector = await store.load_collector_state() if store else None
        collector_sources, collector_ok = self._collector_status(collector, now)
        from_collector, collector_conflicts = _collector_assets(
            collector, {a.asset_id for a in assets}
        )
        held_no_listing = 0
        if fetch is None:
            # Without any server-scan listing, "not in server-scan" is unknowable (ADR-0004).
            held_no_listing, from_collector, collector_conflicts = len(from_collector), [], []
        conflicts = conflicts + node_conflicts + collector_conflicts
        ctx = ClassifyContext(
            policy=self.policy,
            sources={SERVERSCAN_SOURCE: status, **collector_sources},
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
        collected: dict[str, ManagerServer] = {}
        for asset in from_collector:
            record = asset.collector
            assert record is not None
            if record.gpu_models is None:
                coverage["collector_gpu_inventory_unread"] += 1
                carry.add(asset.asset_id)
            elif not record.gpu_models:
                decisions[asset.asset_id] = _empty(asset.asset_id, self.policy)
            else:
                coverage["collector_gpu_assets"] += 1
                collected[asset.asset_id] = record
                decisions[asset.asset_id] = classify(asset, ctx)
        if held_no_listing:
            coverage["collector_held_no_serverscan"] = held_no_listing
        pending = len(fetch.pending_detail_ids) if fetch else 0
        coverage["pending_detail"] = pending

        state = await store.load_state() if store else LedgerState([], {}, {})
        fresh_complete = complete and fetch is not None and fetch is self.last_fetch
        vanished = (
            self._vanished(state, set(decisions) | carry, now)
            if fresh_complete and pending == 0 and collector_ok
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
                assets=[
                    _asset_row(a, servers[a]) if a in servers else _collector_row(a, collected[a])
                    for a in sorted(gpu_ids)
                ],
                milestones=[
                    m
                    for a in sorted(gpu_ids)
                    for m in _milestones(a, servers.get(a), decisions[a], now)
                ],
                conflicts=conflicts,
                resolve_missing_conflicts=fresh_complete,
                now=now,
                gpu_aliases=_aliases(decisions),
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
            collector=collected,
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

    def _collector_status(
        self, collector: CollectorState | None, now: datetime
    ) -> tuple[dict[str, SourceStatus], bool]:
        """
        Health of each collector manager, from the age of its latest complete read.

        Args:
            collector (CollectorState | None): The collector's latest view (None: no database).
            now (datetime): Scan time.

        Returns:
            tuple[dict[str, SourceStatus], bool]: Status by source name, and whether every
            manager is fresh and its latest attempt complete (only then may assets vanish).
        """
        if collector is None:
            return {}, True
        complete_at = {r.manager: r.finished_at for r in collector.reads}
        statuses: dict[str, SourceStatus] = {}
        all_ok = True
        for manager, (_, attempt_complete) in collector.last_attempt.items():
            last_ok = complete_at.get(manager)
            fresh = last_ok is not None and now - last_ok <= self.collector_stale_after
            name = COLLECTOR_SOURCE_PREFIX + manager
            statuses[name] = SourceStatus(name, 0 if fresh else self.grace, last_ok)
            all_ok = all_ok and fresh and attempt_complete
        return statuses, all_ok

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

        views: dict[str, ClusterView] = {}
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
            views[c.conn.name] = build_cluster_view(
                c.conn.name,
                c.last_kube,
                c.last_thanos,
                unreachable=failed >= self.grace,
                reservation_namespaces=c.conn.reservation_namespaces,
            )
        return views

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


def _collector_assets(
    collector: CollectorState | None, serverscan_ids: set[str]
) -> tuple[list[AssetInput], list[IdentityConflict]]:
    """
    Assets for servers only the collector knows; server-scan wins for the same vendor + serial.

    Args:
        collector (CollectorState | None): The collector's latest view.
        serverscan_ids (set[str]): Asset ids server-scan already provides.

    Returns:
        tuple[list[AssetInput], list[IdentityConflict]]: Collector-only assets; a
        `COLLECTOR_DUPLICATE` conflict for each serial seen by two managers or twice in one, and
        a `COLLECTOR_PROFILE_AMBIGUOUS` one for each server whose profile binding is unresolved.
    """
    if collector is None:
        return [], []
    groups: dict[str, list[ManagerServer]] = defaultdict(list)
    for read in collector.reads:
        for m in read.servers:
            serial = m.serial_normalized
            fallback = f"{m.vendor}:collector-id:{m.manager}:{m.source_id}"
            asset_id = f"{m.vendor}:{serial}" if serial else fallback
            if asset_id not in serverscan_ids:
                groups[asset_id].append(m)
    assets: list[AssetInput] = []
    conflicts: list[IdentityConflict] = []
    for asset_id in sorted(groups):
        members = groups[asset_id]
        m = members[0]
        conflict = None
        if len(members) > 1:
            ids = tuple(sorted(f"{x.manager}/{x.source_id}" for x in members))
            conflicts.append(IdentityConflict("COLLECTOR_DUPLICATE", asset_id, ids))
            conflict = f"{len(members)} manager records share this serial: {', '.join(ids)}"
        elif m.profile_unresolved:
            ref = (f"{m.manager}/{m.source_id}",)
            conflicts.append(IdentityConflict("COLLECTOR_PROFILE_AMBIGUOUS", asset_id, ref))
        key = (m.vendor, m.serial_normalized)
        assets.append(
            AssetInput(
                asset_id=asset_id,
                serverscan=None,
                conflict=conflict,
                collector=m,
                collector_first_seen=collector.first_seen.get(key),
                collector_profile_since=collector.profile_since.get((*key, m.profile_name or "")),
            )
        )
    return assets, conflicts


def _collector_row(asset_id: str, m: ManagerServer) -> AssetRow:
    """
    Describe a collector-only asset.

    Args:
        asset_id (str): Asset id.
        m (ManagerServer): Its collector record.

    Returns:
        AssetRow: The row to upsert.
    """
    models = Counter(m.gpu_models or ())
    return AssetRow(
        asset_id=asset_id,
        vendor=m.vendor,
        serial=m.serial_normalized,
        system_uuid=None,
        name=m.name or m.source_id,
        model=m.model,
        site=None,
        gpu_model=models.most_common(1)[0][0] if models else None,
        gpu_count=None if m.gpu_models is None else len(m.gpu_models),
        source=m.manager,
        aliases={"collector_source_id": m.source_id, "manager": m.manager},
    )


def _milestones(
    asset_id: str, s: ServerScanServer | None, decision: Decision, now: datetime
) -> list[Milestone]:
    """
    First-time facts from server-scan and from this scan's classification.

    Args:
        asset_id (str): Asset id.
        s (ServerScanServer | None): Its server-scan record (None for collector-only assets).
        decision (Decision): Its decision (carries cluster milestones).
        now (datetime): Scan time.

    Returns:
        list[Milestone]: `first_seen`, `in_serverscan` when known, and the decision's facts.
    """
    result = [
        Milestone(asset_id, "first_seen", now, lower_bound=True, evidence={"source": "ttv-engine"})
    ]
    if s is not None and s.created_at is not None:
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
